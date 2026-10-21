"""Approvals signed on your phone (S1.6.2).

The app keeps a P-256 key in the iPhone's Secure Enclave, unlocked with Face ID. Servers
hold only the public key, so nothing on a server can approve anything. Each request has a
nonce and an expiry and can be decided once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


class ApprovalError(Exception):
    pass


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


@dataclass
class ApprovalRequest:
    id: str
    kind: str                 # tool, purchase, login, send, delete, code_change, takeover
    summary: str              # what the phone shows
    payload: dict
    nonce: str
    created_at: float
    expires_at: float
    job_id: str | None = None
    conversation_id: str | None = None
    state: str = "pending"    # pending, approved, denied, expired
    extra: dict = field(default_factory=dict)

    def signing_message(self, decision: str, attachment_sha256: str = "") -> bytes:
        return canonical({
            "id": self.id, "nonce": self.nonce, "decision": decision,
            "payload_sha256": digest(self.payload), "attachment_sha256": attachment_sha256,
        })

    def public_view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "summary": self.summary, "nonce": self.nonce,
                "payload_sha256": digest(self.payload), "expires_at": self.expires_at,
                "job_id": self.job_id, "state": self.state, **self.extra}


class Approvals:
    def __init__(self, phone_public_keys_pem: list[bytes], notify: Callable[[ApprovalRequest], None] | None = None,
                 audit=None, ttl_seconds: int = 300, clock: Callable[[], float] = time.time):
        self.keys = [serialization.load_pem_public_key(k) for k in phone_public_keys_pem]
        for k in self.keys:
            if not isinstance(k, ec.EllipticCurvePublicKey) or k.curve.name != "secp256r1":
                raise ValueError("phone keys must be P-256 (Secure Enclave)")
        self.notify = notify or (lambda _r: None)
        self.audit = audit
        self.ttl = ttl_seconds
        self.clock = clock
        self._pending: dict[str, ApprovalRequest] = {}
        self._on_approve: dict[str, Callable[[ApprovalRequest], None]] = {}

    def request(self, kind: str, summary: str, payload: dict, job_id: str | None = None,
                conversation_id: str | None = None, on_approve: Callable[[ApprovalRequest], None] | None = None,
                extra: dict | None = None) -> ApprovalRequest:
        now = self.clock()
        req = ApprovalRequest(f"apr-{secrets.token_hex(8)}", kind, summary, payload, secrets.token_urlsafe(16),
                              now, now + self.ttl, job_id, conversation_id, extra=extra or {})
        self._pending[req.id] = req
        if on_approve:
            self._on_approve[req.id] = on_approve
        if self.audit:
            self.audit.record("approval_requested", "system", {"id": req.id, "kind": kind, "summary": summary}, job_id)
        self.notify(req)
        return req

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._pending.get(request_id)

    def pending(self) -> list[ApprovalRequest]:
        self._expire()
        return [r for r in self._pending.values() if r.state == "pending"]

    def cancel_for_job(self, job_id: str) -> int:
        n = 0
        for r in self._pending.values():
            if r.job_id == job_id and r.state == "pending":
                r.state = "denied"
                n += 1
        return n

    def decide(self, request_id: str, decision: str, signature_b64: str, attachment_sha256: str = "") -> ApprovalRequest:
        self._expire()
        req = self._pending.get(request_id)
        if req is None:
            raise ApprovalError("unknown request")
        if req.state != "pending":
            raise ApprovalError(f"request already {req.state}")
        if decision not in ("approve", "deny"):
            raise ApprovalError("decision must be approve or deny")
        sig = base64.b64decode(signature_b64)
        msg = req.signing_message(decision, attachment_sha256)
        if not any(_verify(k, sig, msg) for k in self.keys):
            if self.audit:
                self.audit.record("approval_bad_signature", "unknown", {"id": request_id}, req.job_id)
            raise ApprovalError("signature does not verify")
        req.state = "approved" if decision == "approve" else "denied"
        if self.audit:
            self.audit.record(f"approval_{req.state}", "owner", {"id": req.id, "kind": req.kind}, req.job_id)
        callback = self._on_approve.pop(req.id, None)
        if req.state == "approved" and callback:
            callback(req)
        return req

    def _expire(self) -> None:
        now = self.clock()
        for r in self._pending.values():
            if r.state == "pending" and now > r.expires_at:
                r.state = "expired"


def _verify(key: ec.EllipticCurvePublicKey, sig: bytes, msg: bytes) -> bool:
    try:
        key.verify(sig, msg, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
