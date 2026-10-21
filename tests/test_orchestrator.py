import json

import pytest
from conftest import FakeClient, block, response

from home_agents.orchestrator import Orchestrator, SpinOffBudget, State
from home_agents.workspaces import DockerWorkspaces

ROUTES = {"home": {"country": "CA"}, "nord-de": {"country": "DE"}}


class FakeContainers:
    def __init__(self):
        self.runs, self.removed = [], []

    def run(self, image, **kw):
        self.runs.append((image, kw))

    def get(self, name):
        parent = self

        class C:
            def remove(self, force):
                parent.removed.append(name)
        return C()

    def list(self, filters):
        return []


class FakeDocker:
    def __init__(self):
        self.containers = FakeContainers()


def plan_response(subtasks):
    return response([block(type="text", text=json.dumps({"subtasks": subtasks}))])


def test_plan_start_and_kill(conn):
    docker = FakeDocker()
    ws = DockerWorkspaces({"home": 18080, "nord-de": 18082}, docker)
    lookup = {"home": ("192.0.2.10", "CA"), "nord-de": ("198.51.100.7", "NL")}.get
    client = FakeClient([plan_response([
        {"title": "Card site", "instructions": "download", "workspace": "desktop", "route": "home", "peers": ["Report"]},
        {"title": "Report", "instructions": "build", "workspace": "cli", "route": "nord-de", "peers": []},
    ])])
    o = Orchestrator(conn, client, ROUTES, ws, lookup)
    subtasks = o.plan("make my TTC reimbursement")
    assert ["agent1", "agent2"] in o.peer_pairs(subtasks)
    job = o.start("make my TTC reimbursement", subtasks)
    board = {r["id"]: r for r in o.board()}
    # nord-de exits from the wrong country, so its subtask never gets a workspace
    assert board[f"{job}-agent2"]["state"] == State.FAILED.value
    assert board[f"{job}-agent1"]["state"] == State.RUNNING.value
    image, kw = docker.containers.runs[0]
    assert kw["network"] == "ws-internal" and kw["environment"]["HTTPS_PROXY"].endswith(":18080")
    assert "ANTHROPIC_API_KEY" not in kw["environment"]
    o.kill(job)
    assert docker.containers.removed and all(r["state"] == "killed" for r in o.board())


def test_budget_caps_spin_off(conn):
    o = Orchestrator(conn, None, ROUTES, None, None, budget=SpinOffBudget(max_subagents=1))
    from home_agents.orchestrator import Subtask
    with pytest.raises(ValueError, match="budget"):
        o.enforce_budget([Subtask("a", "x"), Subtask("b", "y")])
    with pytest.raises(ValueError, match="macOS"):
        o.enforce_budget([Subtask("a", "x", workspace="macos")])
