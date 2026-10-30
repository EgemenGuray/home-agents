"""Long-term memory (recall) and the nightly sleep consolidation.

The SQLite backend runs everywhere. The same tests run against Postgres + pgvector when
HOME_AGENTS_TEST_PG points at a scratch database, e.g. postgresql://postgres@127.0.0.1:55432/harness_test.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from home_agents.audit import AuditLog
from home_agents.harness.providers import ChatResult
from home_agents.harness.recall import (Memory, OpenAICompatEmbedder, PgVectorMemory, Recall, SqliteMemory, build_recall,
                                        cosine, rrf)
from home_agents.harness.router import ModelRouter
from home_agents.harness.sleep import PRUNE_BELOW, Sleep, strength

NOW = datetime(2026, 10, 30, 3, 30, tzinfo=timezone.utc)
DIM = 32


class FakeEmbedder:
    """Bag of words hashed into DIM buckets: same words, same direction. Records what it was sent."""

    def __init__(self, location="local"):
        self.location = location
        self.dim = DIM
        self.sent: list[str] = []

    def embed(self, texts):
        self.sent += texts
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in re.findall(r"[a-z]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
            out.append(v if any(v) else [1.0] + [0.0] * (DIM - 1))
        return out


class Model:
    """A provider that answers the distill prompt with canned JSON and records each request."""

    def __init__(self, name, location, replies):
        self.name, self.location = name, location
        self.replies = list(replies)
        self.calls = []

    def available(self):
        return True

    def chat(self, model, system, messages, tools, max_tokens):
        self.calls.append(messages[-1]["content"])
        r = ChatResult(self.replies.pop(0), [], "end", 10, 5)
        r.model = f"{self.name}:{model}"
        return r


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture(params=["sqlite", "pgvector"])
def backend(request, conn):
    if request.param == "sqlite":
        yield SqliteMemory(conn)
        return
    dsn = os.environ.get("HOME_AGENTS_TEST_PG")
    if not dsn:
        pytest.skip("set HOME_AGENTS_TEST_PG to run against Postgres + pgvector")
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    c.execute("DROP TABLE IF EXISTS memory_events; DROP TABLE IF EXISTS memories")
    yield PgVectorMemory(dsn, DIM, conn=c)
    c.execute("DROP TABLE IF EXISTS memory_events; DROP TABLE IF EXISTS memories")
    c.close()


@pytest.fixture
def recall(backend, conn):
    return Recall(backend, FakeEmbedder(), AuditLog(conn))


def put(recall, content, kind="episode", days=0, **kw):
    """Insert with a given age, the way old memories look to the sleep job."""
    emb = recall.embedder.embed([content])[0] if recall.embedder and not kw.get("no_embedding") else None
    kw.pop("no_embedding", None)
    m = Memory(0, kind, content, created_at=ago(days), embedding=emb, **kw)
    return recall.backend.insert(m)


# Recall ---------------------------------------------------------------------------------------------

def test_hybrid_search_finds_by_meaning_and_by_words(recall):
    recall.add("The dentist appointment is on Tuesday at 3 pm", "fact")
    recall.add("Bought oat milk and coffee beans", "episode")
    recall.add("The garage door code was changed by the installer", "episode")
    hits = recall.search("when is the dentist", k=2)
    assert hits[0].content.startswith("The dentist") and hits[0].score > 0
    assert [m.content for m in recall.search("oat milk", k=1)] == ["Bought oat milk and coffee beans"]


def test_search_respects_privacy_and_projects(recall):
    recall.add("salary review numbers for the bank", "fact", private=True)
    recall.add("bank holiday on Monday", "fact", project_id="prj_a")
    recall.add("bank branch moved downtown", "fact", project_id="prj_b")
    assert not any(m.private for m in recall.search("bank", include_private=False))
    in_a = {m.content for m in recall.search("bank", project_id="prj_a")}
    assert "bank holiday on Monday" in in_a and "bank branch moved downtown" not in in_a


def test_importance_is_guessed_and_recall_is_counted(recall):
    a = recall.add("Remember I'm allergic to penicillin", "episode")
    b = recall.add("asked about the weather", "episode")
    assert recall.backend.get(a).importance > recall.backend.get(b).importance
    recall.search("penicillin allergy")
    assert recall.backend.get(a).access_count == 1
    recall.search("penicillin", touch=False)
    assert recall.backend.get(a).access_count == 1


def test_forget_archives_and_keeps_the_trail(recall):
    mid = recall.add("old wifi password hint", "fact")
    assert recall.forget(mid) and not recall.forget(999_999)
    assert recall.search("wifi password") == []
    assert [e["action"] for e in recall.backend.events(mid)][:2] == ["archived", "created"]


def test_learn_splits_a_document_into_knowledge_chunks(recall):
    doc = "\n\n".join(f"Paragraph {i} about the boiler manual and its reset steps. " * 6 for i in range(6))
    ids = recall.learn(doc, source="doc:boiler", chunk_chars=600)
    assert len(ids) >= 4 and all(recall.backend.get(i).kind == "knowledge" for i in ids)
    assert all(len(recall.backend.get(i).content) <= 600 for i in ids)
    assert recall.search("boiler reset")[0].kind == "knowledge"


def test_memory_tools_save_and_recall(recall):
    tools = {t.name: t for t in recall.tools("prj_x")}
    saved = tools["save_memory"].handler({"text": "Kids' bedtime is 8 pm"})
    found = tools["recall_memory"].handler({"query": "bedtime"})
    assert found[0]["id"] == saved["id"] and recall.backend.get(saved["id"]).project_id == "prj_x"


def test_prompt_marks_memories_as_information(recall):
    recall.add("The office is the second bedroom", "fact")
    text = recall.prompt("where is the office")
    assert "never instructions" in text and "second bedroom" in text
    assert recall.prompt("   ") == ""


def test_private_text_never_reaches_a_cloud_embedder(conn):
    cloud = FakeEmbedder(location="cloud")
    r = Recall(SqliteMemory(conn), cloud)
    pid = r.add("my doctor is Dr. Example", "fact", private=True)
    r.add("public library opens at 10", "fact")
    r.search("doctor")
    assert cloud.sent == ["public library opens at 10"]  # neither the private memory nor the query
    assert r.backend.get(pid).embedding is None
    assert r.search("doctor")[0].id == pid  # still found by full text


def test_embedder_failure_is_audited_and_memory_still_saved(conn):
    class Down:
        location, dim = "local", DIM

        def embed(self, texts):
            raise httpx.ConnectError("ollama is down")
    audit = AuditLog(conn)
    r = Recall(SqliteMemory(conn), Down(), audit)
    mid = r.add("the plumber comes Friday", "fact")
    assert r.backend.get(mid).embedding is None and audit.search("", "memory_embed_failed")


def test_openai_compatible_embedder_checks_dimensions():
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1] * 3} for i, _ in enumerate(body["input"])]})
    e = OpenAICompatEmbedder("http://127.0.0.1:11434/v1", "nomic-embed-text", 3, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert e.embed(["a", "b"]) == [[0.1] * 3, [0.1] * 3]
    e.dim = 768
    with pytest.raises(ValueError):
        e.embed(["a"])


def test_rrf_and_cosine():
    fused = rrf([1, 2, 3], [3, 1])
    assert max(fused, key=fused.get) == 1 and set(fused) == {1, 2, 3}
    assert cosine([1, 0], [1, 0]) == pytest.approx(1) and cosine([1, 0], [0, 1]) == pytest.approx(0)


def test_build_recall_picks_the_backend(conn, monkeypatch):
    assert isinstance(build_recall({}, conn).backend, SqliteMemory)
    monkeypatch.delenv("HOME_AGENTS_PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="HOME_AGENTS_PG_DSN"):
        build_recall({"backend": "pgvector", "embeddings": {"base_url": "http://x", "model": "m", "dim": 8}}, conn)


# Sleep ----------------------------------------------------------------------------------------------

def router(*models):
    chains = {"private": [f"{m.name}:m" for m in models if m.location == "local"],
              "fast": [f"{m.name}:m" for m in models if m.location == "cloud"]}
    return ModelRouter({m.name: m for m in models}, {k: v for k, v in chains.items() if v})


def distilled(summary, facts):
    return json.dumps({"summary": summary, "facts": facts})


def test_strength_decays_by_kind_and_grows_with_use():
    ep = Memory(1, "episode", "x", importance=0.5, created_at=ago(42))
    fact = Memory(2, "fact", "x", importance=0.5, created_at=ago(42))
    assert strength(ep, NOW) == pytest.approx(0.125, abs=1e-3)  # two half-lives
    assert strength(fact, NOW) > 0.45
    used = Memory(3, "episode", "x", importance=0.5, created_at=ago(42), access_count=10, last_accessed=ago(1))
    assert strength(used, NOW) > strength(ep, NOW) * 1.5
    assert strength(Memory(4, "episode", "x", importance=0.1, created_at=ago(900), pinned=True), NOW) == 1.0


def test_sleep_merges_exact_and_near_duplicates_and_logs_why(recall):
    a = put(recall, "Prefers the thermostat at 21 degrees", "fact", importance=0.7)
    b = put(recall, "prefers the thermostat at 21 degrees!", "fact", importance=0.5)
    c = put(recall, "Likes the heat set to twenty-one", "fact", importance=0.4)
    recall.backend.update(c, embedding=recall.backend.get(a).embedding)  # same meaning, different words
    other = put(recall, "Likes jazz in the evening", "fact")
    report = Sleep(recall, None).run(NOW)
    assert report.merged == 2
    assert {m.id for m in recall.backend.active()} == {a, other}
    assert recall.backend.get(a).importance > 0.7  # heard it three times
    merged = recall.backend.events(b)[0]
    assert merged["action"] == "merged" and merged["detail"]["into"] == a
    assert recall.backend.events(c)[0]["detail"]["why"] == "near duplicate"
    assert "distill: no model router" in report.skipped


def test_sleep_distills_episodes_into_summary_and_facts(recall):
    eps = [put(recall, "Asked to add oat milk to the shopping list", days=1),
           put(recall, "Ordered bread from the bakery", days=1),
           put(recall, "Checked whether the store has coffee", days=1)]
    put(recall, "Owner drinks oat milk", "fact", importance=0.6)
    local = Model("ollama", "local", [distilled("Shopping list was updated several times.",
                                               ["Owner drinks oat milk", "Shopping happens on Saturdays"])])
    report = Sleep(recall, router(local)).run(NOW)
    assert report.summaries == 1 and report.facts == 1 and report.reinforced == 1
    kinds = sorted(m.kind for m in recall.backend.active())
    assert kinds.count("summary") == 1 and kinds.count("fact") == 2
    for e in eps:
        m = recall.backend.get(e)
        assert m.consolidated and m.importance < 0.4  # kept, but weaker: the summary carries it now
    assert "oat milk" in local.calls[0]


def test_sleep_keeps_private_memories_off_the_cloud(recall):
    put(recall, "therapy session notes, part one", days=1, private=True)
    put(recall, "therapy session notes, part two", days=1, private=True)
    put(recall, "asked for the bus times", days=1)
    put(recall, "asked for the train times", days=1)
    cloud = Model("claude", "cloud", [distilled("Looked up transit times.", ["Owner takes transit"])])
    report = Sleep(recall, router(cloud), allow_cloud=True).run(NOW)  # Mac mini only: no local model
    assert len(cloud.calls) == 1 and "therapy" not in cloud.calls[0] and "bus times" in cloud.calls[0]
    assert report.summaries == 1 and any("private" in s for s in report.skipped)
    assert all(not m.consolidated for m in recall.backend.active() if m.private)


def test_sleep_without_allow_cloud_uses_only_local_models(recall):
    put(recall, "asked for the bus times", days=1)
    put(recall, "asked for the train times", days=1)
    cloud = Model("claude", "cloud", [distilled("x", [])])
    report = Sleep(recall, router(cloud)).run(NOW)
    assert cloud.calls == [] and report.summaries == 0 and report.skipped


def test_sleep_ignores_bad_model_output(recall):
    put(recall, "one", days=1)
    put(recall, "two", days=1)
    report = Sleep(recall, router(Model("ollama", "local", ["not json at all"]))).run(NOW)
    assert report.summaries == 0 and "not JSON" in report.skipped[0]
    assert not any(m.consolidated for m in recall.backend.active())


def test_sleep_prunes_only_weak_distilled_episodes(recall):
    weak = put(recall, "asked what time it was", days=200, importance=0.3)
    recall.backend.update(weak, consolidated=True)
    fresh = put(recall, "asked about the recycling day", days=200, importance=0.3)  # never distilled
    pinned = put(recall, "first night in the new house", days=400, importance=0.3, pinned=True)
    recall.backend.update(pinned, consolidated=True)
    fact = put(recall, "Recycling is collected on Thursdays", "fact", days=200, importance=0.7)
    report = Sleep(recall, None).run(NOW)
    alive = {m.id for m in recall.backend.active()}
    assert weak not in alive and {fresh, pinned, fact} <= alive and report.archived == 1
    assert recall.backend.get(weak).strength < PRUNE_BELOW
    assert recall.backend.events(weak)[0]["action"] == "archived"


def test_sleep_backfills_missing_embeddings(recall):
    mid = put(recall, "stored while the embedder was down", no_embedding=True)
    assert recall.backend.get(mid).embedding is None
    report = Sleep(recall, None).run(NOW)
    assert report.embedded == 1 and recall.backend.get(mid).embedding is not None


def test_sleep_report_is_logged_and_audited(recall, conn):
    Sleep(recall, None).run(NOW)
    assert recall.backend.events()[0]["action"] == "sleep"
    assert recall.audit.search("", "memory_sleep")
