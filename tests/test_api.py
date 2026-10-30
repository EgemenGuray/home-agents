from conftest import FakeClient, block, response
from fastapi.testclient import TestClient

from home_agents.api import build_app
from home_agents.approvals import Approvals
from home_agents.settings import Settings


def test_converse_and_signed_approval(tmp_path, monkeypatch, phone):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")
    approvals = Approvals([phone.public_pem])
    client = FakeClient([response([block(type="text", text="Hello.")])])
    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml"), client, approvals)
    http = TestClient(app)
    assert http.post("/v1/converse", json={"text": "hi"}).status_code == 401
    r = http.post("/v1/converse", json={"text": "hi"}, headers={"Authorization": "Bearer test-token"})
    assert r.json()["text"] == "Hello."

    req = approvals.request("send", "Send draft", {"draft": 1})
    bad = http.post(f"/v1/approvals/{req.id}", json={"decision": "approve", "signature": phone.sign(req, "deny")})
    assert bad.status_code == 400
    ok = http.post(f"/v1/approvals/{req.id}", json={"decision": "approve", "signature": phone.sign(req, "approve")})
    assert ok.json()["state"] == "approved"


def test_metrics_counts_pending_approvals(tmp_path, monkeypatch, phone):
    monkeypatch.setenv("HOME_AGENTS_SERVICE_TOKEN", "test-token")
    approvals = Approvals([phone.public_pem])
    app = build_app(Settings(data_dir=tmp_path, policy_file=tmp_path / "none.yaml"), FakeClient([]), approvals)
    approvals.request("send", "Send draft", {"draft": 1})
    body = TestClient(app).get("/metrics", headers={"Authorization": "Bearer test-token"}).text
    assert "home_agents_approvals_pending 1" in body
    assert 'home_agents_spend_cad{period="month"} 0' in body
