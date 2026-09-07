"""Live LLM interaction trace (SPEC §7).

An in-memory, bounded ring of the most recent LLM calls — what was asked
(system/user prompts, truncated) and what came back (raw reply, latency, token
usage) — broadcast live over the activity SSE stream so the Activity page can
show the ongoing conversation with the LLM server while the pipeline runs.

Deliberately NOT persisted to ACTIVITY_LOG: prompts carry full article text and
would blow up the ring-buffer table. The trace is observability, not history —
it resets on restart.

Every function here must never raise: tracing is best-effort and must not break
the pipeline. Call sites annotate the current call with `context(...)` (a
task-local ContextVar, like llm_client.last_usage) so chat_json/embed can tag
records without signature changes (tests monkeypatch those signatures).
"""

import itertools
import json
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.services import activity

# Bounded in-memory ring (most recent calls first when read via recent()).
_records: deque[dict[str, Any]] = deque(maxlen=100)
_ids = itertools.count(1)

# Task-local annotation set by call sites: kind (usage-style), label (human
# hint like the article title), article_id. None = unannotated call.
_current: ContextVar[dict[str, Any] | None] = ContextVar("llm_trace_context", default=None)


@contextmanager
def context(
    kind: str, *, label: str | None = None, article_id: int | None = None
) -> Iterator[None]:
    """Annotate LLM calls made inside the block with kind/label/article_id."""
    token = _current.set({"kind": kind, "label": label, "article_id": article_id})
    try:
        yield
    finally:
        _current.reset(token)


def _truncate(text: str) -> tuple[str, int, bool]:
    """Clamp a text field to llm_trace_max_chars. Returns (text, full_len, cut)."""
    full = len(text)
    limit = max(settings.llm_trace_max_chars, 100)
    if full <= limit:
        return text, full, False
    return text[:limit], full, True


def _broadcast(record: dict[str, Any]) -> None:
    activity.broadcast({"action": "llm_interaction", "interaction": record})


def begin_chat(model: str, system: str, user: str) -> dict[str, Any] | None:
    """Open a trace record for a chat completion (None when tracing is off)."""
    if not settings.llm_trace_enabled:
        return None
    try:
        sys_text, sys_len, sys_cut = _truncate(system)
        user_text, user_len, user_cut = _truncate(user)
        ctx = _current.get() or {}
        record: dict[str, Any] = {
            "id": next(_ids),
            "ts": datetime.now(UTC).isoformat(),
            "kind": ctx.get("kind") or "other",
            "label": ctx.get("label"),
            "article_id": ctx.get("article_id"),
            "endpoint": "chat",
            "model": model,
            "status": "running",
            "request": {
                "system": sys_text,
                "user": user_text,
                "system_chars": sys_len,
                "user_chars": user_len,
                "truncated": sys_cut or user_cut,
            },
            "response": None,
            "error": None,
            "latency_ms": None,
            "usage": None,
            "attempts": 1,
        }
        _records.append(record)
        _broadcast(record)
        return record
    except Exception:
        return None  # never break the pipeline for tracing


def begin_embed(model: str, texts: list[str]) -> dict[str, Any] | None:
    """Open a trace record for an embeddings call (None when tracing is off)."""
    if not settings.llm_trace_enabled:
        return None
    try:
        total_chars = sum(len(t) for t in texts)
        sample, _, _ = _truncate(texts[0] if texts else "")
        ctx = _current.get() or {}
        record = {
            "id": next(_ids),
            "ts": datetime.now(UTC).isoformat(),
            "kind": ctx.get("kind") or "other",
            "label": ctx.get("label"),
            "article_id": ctx.get("article_id"),
            "endpoint": "embed",
            "model": model,
            "status": "running",
            "request": {
                "texts": len(texts),
                "chars": total_chars,
                "sample": sample,
            },
            "response": None,
            "error": None,
            "latency_ms": None,
            "usage": None,
            "attempts": 1,
        }
        _records.append(record)
        _broadcast(record)
        return record
    except Exception:
        return None


def complete(
    record: dict[str, Any] | None,
    response: Any,
    *,
    latency_ms: int,
    usage: dict[str, Any] | None,
    attempts: int = 1,
) -> None:
    """Close a record as done. `response` is shown raw (string) or JSON-dumped."""
    if record is None:
        return
    try:
        text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        shown, full, _cut = _truncate(text)
        record.update(
            status="done",
            response=shown,
            response_chars=full,
            latency_ms=latency_ms,
            usage=usage,
            attempts=attempts,
        )
        _broadcast(record)
    except Exception:
        pass


def fail(
    record: dict[str, Any] | None,
    error: str,
    *,
    latency_ms: int | None = None,
    attempts: int = 1,
) -> None:
    """Close a record as failed."""
    if record is None:
        return
    try:
        record.update(
            status="error",
            error=error[:500],
            latency_ms=latency_ms,
            attempts=attempts,
        )
        _broadcast(record)
    except Exception:
        pass


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent records, newest first."""
    return list(reversed(_records))[:limit]


def clear() -> None:
    """Test hook: empty the ring."""
    _records.clear()
