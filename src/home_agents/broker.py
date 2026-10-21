"""Credential broker (E1.7). It relays requests and ciphertext; it never sees a password.

1. A workspace makes a one-time X25519 key pair and asks for a login: site, job, reason, route.
2. The broker checks the site is one agents may ever use and sends an approval to your phone,
   showing the job, site, exit IP and planned action.
3. On Face ID approval, the app decrypts that one vault item on the phone, seals it to the
   workspace's public key and signs the decision over the ciphertext's hash.
4. The broker verifies the signature and hands the ciphertext to that workspace once.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass

from nacl.public import PublicKey

from .approvals import ApprovalError, Approvals


class BrokerError(Exception):
    pass


@dataclass
class CredentialRequest:
    approval_id: str
    workspace: str
    site: str
    job_id: str
    workspace_public_key: str   # base64 X25519
    ciphertext: bytes | None = None
    delivered: bool = False


class CredentialBroker:
    def __init__(self, approvals: Approvals, allowed_sites: set[str], audit=None, deliver_ttl: int = 120):
        self.approvals = approvals
        self.allowed_sites = {s.lower() for s in allowed_sites}
        self.audit = audit
        self.deliver_ttl = deliver_ttl
        self._requests: dict[str, CredentialRequest] = {}

    def request_login(self, *, workspace: str, job_id: str, site: str, reason: str, route: str, exit_ip: str,
                      workspace_public_key: str) -> str:
        site = site.lower().strip()
        if site not in self.allowed_sites:
            raise BrokerError(f"{site} is not in the agents collection; add it on your phone first")
        try:
            PublicKey(base64.b64decode(workspace_public_key))
        except Exception as exc:
            raise BrokerError("workspace public key is not a valid X25519 key") from exc
        fingerprint = hashlib.sha256(base64.b64decode(workspace_public_key)).hexdigest()[:16]
        req = self.approvals.request(
            kind="login",
            summary=f"Log in to {site} for job {job_id}: {reason}",
            payload={"site": site, "job_id": job_id, "route": route, "exit_ip": exit_ip,
                     "workspace": workspace, "workspace_key_fingerprint": fingerprint, "reason": reason},
            job_id=job_id,
            extra={"site": site, "route": route, "exit_ip": exit_ip, "workspace_public_key": workspace_public_key},
        )
        self._requests[req.id] = CredentialRequest(req.id, workspace, site, job_id, workspace_public_key)
        if self.audit:
            self.audit.record("login_requested", workspace, {"site": site, "route": route, "approval": req.id}, job_id)
        return req.id

    def phone_response(self, approval_id: str, decision: str, signature_b64: str, ciphertext_b64: str = "") -> None:
        cred = self._requests.get(approval_id)
        if cred is None:
            raise BrokerError("unknown request")
        ciphertext = base64.b64decode(ciphertext_b64) if ciphertext_b64 else b""
        attachment = hashlib.sha256(ciphertext).hexdigest() if ciphertext else ""
        try:
            req = self.approvals.decide(approval_id, decision, signature_b64, attachment)
        except ApprovalError as exc:
            raise BrokerError(str(exc)) from exc
        if req.state == "approved":
            if not ciphertext:
                raise BrokerError("approved without ciphertext")
            cred.ciphertext = ciphertext
        if self.audit:
            self.audit.record(f"login_{req.state}", "owner", {"site": cred.site, "approval": approval_id}, cred.job_id)

    def fetch(self, approval_id: str, workspace: str) -> bytes:
        cred = self._requests.get(approval_id)
        if cred is None or cred.workspace != workspace:
            raise BrokerError("no such request for this workspace")
        if cred.delivered:
            raise BrokerError("already delivered")
        req = self.approvals.get(approval_id)
        if req is None or req.state != "approved" or cred.ciphertext is None:
            raise BrokerError(f"not approved ({req.state if req else 'unknown'})")
        if time.time() > req.expires_at + self.deliver_ttl:
            raise BrokerError("approval too old")
        cred.delivered = True
        ct, cred.ciphertext = cred.ciphertext, None   # hand over once, keep nothing
        if self.audit:
            self.audit.record("login_delivered", workspace, {"site": cred.site, "approval": approval_id}, cred.job_id)
        return ct

    def cancel_workspace(self, workspace: str) -> int:
        n = 0
        for cred in self._requests.values():
            if cred.workspace == workspace and not cred.delivered:
                req = self.approvals.get(cred.approval_id)
                if req and req.state == "pending":
                    req.state = "denied"
                cred.ciphertext = None
                n += 1
        return n
