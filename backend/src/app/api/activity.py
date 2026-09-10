"""Activity API (SPEC §7): live SSE stream + recent history."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.core.db import get_session
from app.models import ActivityEvent, Article, Feed, User
from app.services import activity, llmtrace
from app.services.process import queue_depth

router = APIRouter(prefix="/activity", tags=["activity"])

# Pipeline states in execution order (invariant 7)
_PIPELINE_STATES = ["fetched", "fulltext", "summarized", "embedded", "clustered", "filtered"]


class EventOut(BaseModel):
    ts: datetime
    level: str
    component: str
    action: str
    detail: dict[str, object]


@router.get("/recent")
async def recent(
    limit: int = 200,
    component: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    limit = min(limit, 500)
    q = select(ActivityEvent).order_by(desc(ActivityEvent.id)).limit(limit)
    if component:
        q = q.where(ActivityEvent.component == component)
    rows = (await session.scalars(q)).all()
    return {
        "events": [
            EventOut(
                ts=e.ts,
                level=e.level,
                component=e.component,
                action=e.action,
                detail=json.loads(e.detail),
            )
            for e in reversed(rows)
        ],
        "llm_queue_depth": queue_depth(),
    }


class PipelineRow(BaseModel):
    id: int
    title: str
    feed_title: str
    processing_state: str
    fetched_at: datetime
    content_status: str
    story_id: int | None


# Hard cap on in-flight rows returned (safety valve; a sane backlog is far smaller).
# Finished articles are always limited to the most recent ones.
_IN_FLIGHT_CAP = 500
_FINISHED_ROWS = 20


@router.get("/pipeline")
async def pipeline(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Snapshot of the pipeline: every article still in flight + the last finished."""
    base = select(Article, Feed.title).join(Feed, Article.feed_id == Feed.id)
    in_flight = (
        await session.execute(
            base.where(Article.processing_state.not_in(["clustered", "filtered"]))
            .order_by(desc(Article.id))
            .limit(_IN_FLIGHT_CAP + 1)  # one extra to detect truncation
        )
    ).all()
    truncated = len(in_flight) > _IN_FLIGHT_CAP
    in_flight = in_flight[:_IN_FLIGHT_CAP]
    finished = (
        await session.execute(
            base.where(Article.processing_state == "clustered")
            .order_by(desc(Article.id))
            .limit(_FINISHED_ROWS)
        )
    ).all()

    def _row(article: Article, feed_title: str) -> PipelineRow:
        return PipelineRow(
            id=article.id,
            title=article.title,
            feed_title=feed_title,
            processing_state=article.processing_state,
            fetched_at=article.fetched_at,
            content_status=article.content_status,
            story_id=article.story_id,
        )

    out = [_row(a, t) for a, t in in_flight] + [_row(a, t) for a, t in finished]
    return {
        "states": _PIPELINE_STATES,
        "rows": [r.model_dump(mode="json") for r in out],
        "in_flight": len(in_flight),
        "truncated": truncated,
        "llm_queue_depth": queue_depth(),
    }


@router.get("/llm")
async def llm_interactions(
    limit: int = 50,
    user: User = Depends(current_user),
) -> dict[str, object]:
    """Snapshot of the in-memory LLM interaction trace (SPEC §7) for the
    Activity page's initial load; updates then arrive live over the SSE stream
    as `llm_interaction` actions."""
    return {
        "enabled": settings.llm_trace_enabled,
        "interactions": llmtrace.recent(min(limit, 100)),
    }


@router.get("/stream")
async def stream(user: User = Depends(current_user)) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        q = activity.subscribe()
        try:
            yield f"event: hello\ndata: {json.dumps({'llm_queue_depth': queue_depth()})}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=25)
                    payload["ts"] = datetime.now(UTC).isoformat()
                except TimeoutError:
                    payload = {"action": "ping"}  # keep-alive
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            activity.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
