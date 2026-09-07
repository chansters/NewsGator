"""Live LLM interaction trace tests (SPEC §7): prompts/replies are captured
in-memory by llmtrace and exposed via GET /api/activity/llm + the SSE stream."""

import pytest
from httpx import AsyncClient
from tests.conftest import setup_admin

from app.core.config import settings
from app.services import llm_client, llmtrace


@pytest.fixture(autouse=True)
def _clean_trace():
    """The trace ring is module-global; isolate per test."""
    llmtrace.clear()
    yield
    llmtrace.clear()


def _fake_chat_client(contents: list[str]):
    """Fake OpenAI chat client replaying the given message contents."""
    state = {"calls": 0}

    class FakeCompletions:
        async def create(self, **kwargs):
            content = contents[min(state["calls"], len(contents) - 1)]
            state["calls"] += 1
            msg = type("M", (), {"content": content})()
            choice = type("C", (), {"message": msg})()
            return type("R", (), {"choices": [choice]})()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    return FakeClient()


def _fake_embed_client():
    class FakeEmbeddings:
        async def create(self, model: str, input: list[str]):
            data = [type("D", (), {"embedding": [0.1, 0.2, 0.3]})() for _ in input]
            return type("R", (), {"data": data})()

    class FakeClient:
        embeddings = FakeEmbeddings()

    return FakeClient()


async def test_chat_records_prompt_and_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_chat_client(['{"summary": "hi"}'])
    monkeypatch.setattr(llm_client, "_chat_client", lambda: fake)
    with llmtrace.context("summarize", label="Some article", article_id=7):
        result, _ = await llm_client.chat_json("sys prompt", "user prompt")
    assert result == {"summary": "hi"}
    records = llmtrace.recent()
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "done"
    assert rec["kind"] == "summarize"
    assert rec["label"] == "Some article"
    assert rec["article_id"] == 7
    assert rec["endpoint"] == "chat"
    assert rec["request"]["system"] == "sys prompt"
    assert rec["request"]["user"] == "user prompt"
    assert rec["response"] == '{"summary": "hi"}'
    assert rec["latency_ms"] is not None
    assert rec["attempts"] == 1


async def test_chat_error_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingCompletions:
        async def create(self, **kwargs):
            raise ConnectionError("server down")

    class FakeClient:
        chat = type("Chat", (), {"completions": FailingCompletions()})()

    monkeypatch.setattr(llm_client, "_chat_client", lambda: FakeClient())
    with pytest.raises(llm_client.LLMError):
        await llm_client.chat_json("sys", "user")
    (rec,) = llmtrace.recent()
    assert rec["status"] == "error"
    assert "server down" in rec["error"]
    assert rec["kind"] == "other"  # no context annotation


async def test_retry_marks_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_chat_client(["not json", '{"ok": true}'])
    monkeypatch.setattr(llm_client, "_chat_client", lambda: fake)
    result, _ = await llm_client.chat_json("sys", "user")
    assert result == {"ok": True}
    (rec,) = llmtrace.recent()
    assert rec["status"] == "done"
    assert rec["attempts"] == 2


async def test_embed_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "_embed_client", _fake_embed_client)
    with llmtrace.context("embed", label="T", article_id=3):
        out = await llm_client.embed(["alpha", "beta"])
    assert len(out) == 2
    (rec,) = llmtrace.recent()
    assert rec["endpoint"] == "embed"
    assert rec["status"] == "done"
    assert rec["request"]["texts"] == 2
    assert rec["request"]["chars"] == len("alpha") + len("beta")
    assert rec["request"]["sample"] == "alpha"
    assert '"vectors": 2' in rec["response"]
    assert '"dimensions": 3' in rec["response"]


async def test_trace_disabled_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_trace_enabled", False)
    fake = _fake_chat_client(['{"ok": true}'])
    monkeypatch.setattr(llm_client, "_chat_client", lambda: fake)
    await llm_client.chat_json("sys", "user")
    assert llmtrace.recent() == []


async def test_long_prompts_are_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_trace_max_chars", 100)
    fake = _fake_chat_client(['{"ok": true}'])
    monkeypatch.setattr(llm_client, "_chat_client", lambda: fake)
    long_user = "x" * 500
    await llm_client.chat_json("sys", long_user)
    (rec,) = llmtrace.recent()
    assert rec["request"]["user"] == "x" * 100
    assert rec["request"]["user_chars"] == 500
    assert rec["request"]["truncated"] is True


async def test_llm_endpoint_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/activity/llm")).status_code == 401


async def test_llm_endpoint_returns_trace(client: AsyncClient) -> None:
    await setup_admin(client)
    rec = llmtrace.begin_chat("test-model", "s", "u")
    llmtrace.complete(rec, '{"ok": true}', latency_ms=12, usage={"total_tokens": 9})
    r = await client.get("/api/activity/llm")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert len(body["interactions"]) == 1
    it = body["interactions"][0]
    assert it["model"] == "test-model"
    assert it["status"] == "done"
    assert it["usage"]["total_tokens"] == 9
