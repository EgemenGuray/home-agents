import base64
import hashlib

import pytest
from nacl.public import PrivateKey, SealedBox

from home_agents.approvals import ApprovalError, Approvals
from home_agents.audit import AuditLog
from home_agents.broker import BrokerError, CredentialBroker


def test_signed_approval_runs_once(phone, conn):
    ran = []
    a = Approvals([phone.public_pem], audit=AuditLog(conn))
    req = a.request("purchase", "Paper towels CA$18", {"amount": 18}, on_approve=lambda r: ran.append(r.id))
    a.decide(req.id, "approve", phone.sign(req, "approve"))
    assert ran == [req.id]
    with pytest.raises(ApprovalError, match="already"):
        a.decide(req.id, "approve", phone.sign(req, "approve"))


def test_server_cannot_forge_or_flip_a_decision(phone):
    from tests_helpers import other_phone_signature
    a = Approvals([phone.public_pem])
    req = a.request("send", "Send email", {"to": "x"})
    with pytest.raises(ApprovalError, match="signature"):
        a.decide(req.id, "approve", other_phone_signature(req, "approve"))
    deny_sig = phone.sign(req, "deny")
    with pytest.raises(ApprovalError, match="signature"):
        a.decide(req.id, "approve", deny_sig)   # a deny signature can't be replayed as approve


def test_approval_expires(phone):
    now = [1000.0]
    a = Approvals([phone.public_pem], ttl_seconds=60, clock=lambda: now[0])
    req = a.request("tool", "Unlock door", {})
    now[0] += 61
    with pytest.raises(ApprovalError, match="expired"):
        a.decide(req.id, "approve", phone.sign(req, "approve"))


def test_broker_relays_only_ciphertext(phone, conn):
    audit = AuditLog(conn)
    approvals = Approvals([phone.public_pem], audit=audit)
    broker = CredentialBroker(approvals, {"card.example.com"}, audit=audit)
    ws_key = PrivateKey.generate()
    ws_pub = base64.b64encode(bytes(ws_key.public_key)).decode()

    with pytest.raises(BrokerError, match="agents collection"):
        broker.request_login(workspace="ws1", job_id="j1", site="bank.example.com", reason="x", route="home",
                             exit_ip="192.0.2.1", workspace_public_key=ws_pub)

    rid = broker.request_login(workspace="ws1", job_id="j1", site="card.example.com", reason="TTC charges",
                               route="home", exit_ip="192.0.2.1", workspace_public_key=ws_pub)
    req = approvals.get(rid)
    assert req.extra["exit_ip"] == "192.0.2.1"

    # On the phone: seal the login to the workspace key, sign over its hash.
    ct = SealedBox(ws_key.public_key).encrypt(b'{"username":"u","password":"hunter2"}')
    sig = phone.sign(req, "approve", hashlib.sha256(ct).hexdigest())
    broker.phone_response(rid, "approve", sig, base64.b64encode(ct).decode())

    with pytest.raises(BrokerError):
        broker.fetch(rid, "ws-other")
    got = broker.fetch(rid, "ws1")
    assert SealedBox(ws_key).decrypt(got) == b'{"username":"u","password":"hunter2"}'
    with pytest.raises(BrokerError, match="already"):
        broker.fetch(rid, "ws1")
    assert "hunter2" not in str(audit.search())


def test_broker_rejects_swapped_ciphertext(phone):
    approvals = Approvals([phone.public_pem])
    broker = CredentialBroker(approvals, {"card.example.com"})
    ws_pub = base64.b64encode(bytes(PrivateKey.generate().public_key)).decode()
    rid = broker.request_login(workspace="ws1", job_id="j1", site="card.example.com", reason="r", route="home",
                               exit_ip="192.0.2.1", workspace_public_key=ws_pub)
    req = approvals.get(rid)
    sig = phone.sign(req, "approve", hashlib.sha256(b"real").hexdigest())
    with pytest.raises(BrokerError, match="signature"):
        broker.phone_response(rid, "approve", sig, base64.b64encode(b"attacker").decode())
