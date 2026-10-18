"""FastAPI application entrypoint for Betty, the trip-planning guide."""

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Sequence
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from app.config import get_settings
from app.logging_config import configure_logging
from app.schemas import (
    ChatRequest,
    ChatResponse,
    Message,
    StreamError,
    StreamStart,
    SummarizeRequest,
    SummarizeResponse,
)
from app.services.betty_service import BettyError, BettyService, ChatEvent

configure_logging(get_settings().log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Betty Trip Planner", version="0.1.0")

# While the server works without sending anything (a place search can take a minute), the stream
# gets an SSE comment this often so clients and proxies don't time the connection out.
PING_SECONDS = 15.0


@lru_cache
def _build_service() -> BettyService:
    return BettyService(get_settings())


def get_betty_service() -> BettyService:
    """Dependency providing the shared BettyService."""
    try:
        return _build_service()
    except BettyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


BettyDep = Annotated[BettyService, Depends(get_betty_service)]


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send browsers hitting the bare URL to the interactive API docs."""
    return RedirectResponse("/docs")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.post("/api/v1/chat")
def chat(request: ChatRequest, betty: BettyDep) -> ChatResponse:
    """Send the conversation so far and receive Betty's next reply."""
    _require_user_last(request)
    try:
        logger.info(
            "POST /chat: %d messages, last user message: %r",
            len(request.messages),
            request.messages[-1].content,
        )
        result = betty.chat(request.messages)
        search = result.search
        logger.info(
            "chat done: searched=%s reply=%r trip=%s places=%s error=%r",
            search is not None,
            result.reply,
            search.trip.model_dump_json() if search else None,
            f"{len(search.places)}/{search.total}" if search else None,
            search.error if search else None,
        )
        if result.events:
            logger.info(
                "chat events: %s %s..%s shown=%d total=%d error=%r",
                result.events.city,
                result.events.from_date,
                result.events.to_date,
                len(result.events.events),
                result.events.total,
                result.events.error,
            )
        return ChatResponse(reply=result.reply, search=search, events=result.events)
    except BettyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@app.post(
    "/api/v1/chat/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-Sent Events."}},
)
async def chat_stream(request: ChatRequest, betty: BettyDep) -> StreamingResponse:
    """Like /api/v1/chat, but Betty's reply streams as Server-Sent Events.

    Events: `start`, then `delta` (text), `status`, `search` and `events` as they happen, then
    one `done` (same body as /api/v1/chat) or one `error`. See docs/mobile-chat-api.md.
    """
    _require_user_last(request)
    turn_id = uuid.uuid4().hex[:12]
    logger.info(
        "POST /chat/stream [%s]: %d messages, last user message: %r",
        turn_id,
        len(request.messages),
        request.messages[-1].content,
    )
    return StreamingResponse(
        _stream_turn(betty, request.messages, turn_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _require_user_last(request: ChatRequest) -> None:
    if request.messages[-1].role != "user":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Last message must be from the user."
        )


def _sse(name: str, data: BaseModel) -> str:
    return f"event: {name}\ndata: {data.model_dump_json()}\n\n"


async def _stream_turn(
    betty: BettyService, messages: Sequence[Message], turn_id: str
) -> AsyncIterator[str]:
    """Run the (blocking) chat_stream in a thread and forward its events, with keep-alives.

    When the client disconnects, the thread is told to stop and closes the model stream at its
    next event; a place search already running finishes first.
    """
    yield _sse("start", StreamStart(turn_id=turn_id))
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[ChatEvent | Exception | None] = asyncio.Queue()
    stop = threading.Event()

    def put(item: ChatEvent | Exception | None) -> None:
        # RuntimeError: the event loop is gone, so nobody is listening any more.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, item)

    def produce() -> None:
        events = betty.chat_stream(messages)
        try:
            for event in events:
                if stop.is_set():
                    break
                put(event)
        except Exception as exc:  # handed to the event loop, which sends it as an error event
            put(exc)
        finally:
            events.close()
            put(None)

    threading.Thread(target=produce, name=f"chat-{turn_id}", daemon=True).start()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                yield _sse("error", _stream_error(item, turn_id))
                break
            if isinstance(item.data, ChatResponse):
                search = item.data.search
                logger.info(
                    "chat stream done [%s]: reply=%r places=%s routes=%s events=%s",
                    turn_id,
                    item.data.reply,
                    f"{len(search.places)}/{search.total}" if search else None,
                    len(search.routes) if search else None,
                    len(item.data.events.events) if item.data.events else None,
                )
            yield _sse(item.name, item.data)
    finally:
        stop.set()


def _stream_error(exc: Exception, turn_id: str) -> StreamError:
    if isinstance(exc, BettyError):
        logger.warning("chat stream failed [%s]: %s", turn_id, exc)
        return StreamError(code="upstream_unavailable", message=str(exc))
    logger.error("chat stream crashed [%s]", turn_id, exc_info=exc)
    return StreamError(code="internal", message="Unexpected server error.")


@app.post("/api/v1/summarize")
def summarize(request: SummarizeRequest, betty: BettyDep) -> SummarizeResponse:
    """Condense the conversation into a single-paragraph trip summary."""
    try:
        logger.info(
            "POST /summarize: %d messages, trip=%s",
            len(request.messages),
            request.trip.model_dump_json() if request.trip else None,
        )
        summary = betty.summarize(request.messages, request.trip)
        logger.info("summarize done: %r", summary)
        return SummarizeResponse(summary=summary)
    except BettyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
