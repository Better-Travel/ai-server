"""FastAPI application entrypoint for Betty, the trip-planning guide."""

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.logging_config import configure_logging
from app.schemas import (
    ChatRequest,
    ChatResponse,
    SummarizeRequest,
    SummarizeResponse,
)
from app.services.betty_service import BettyError, BettyService

configure_logging(get_settings().log_level)
logger = logging.getLogger(__name__)

app = FastAPI(title="Betty Trip Planner", version="0.1.0")


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
    if request.messages[-1].role != "user":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Last message must be from the user."
        )
    try:
        logger.info(
            "POST /chat: %d messages, last user message: %r",
            len(request.messages),
            request.messages[-1].content,
        )
        result = betty.chat(request.messages)
        logger.info(
            "chat done: ready=%s reply=%r trip_context=%s",
            result.trip_context is not None,
            result.reply,
            result.trip_context.model_dump_json() if result.trip_context else None,
        )
        return ChatResponse(
            reply=result.reply,
            ready=result.trip_context is not None,
            trip_context=result.trip_context,
        )
    except BettyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@app.post("/api/v1/summarize")
def summarize(request: SummarizeRequest, betty: BettyDep) -> SummarizeResponse:
    """Condense the conversation into a single-paragraph trip summary."""
    try:
        logger.info(
            "POST /summarize: %d messages, trip_context=%s",
            len(request.messages),
            request.trip_context is not None,
        )
        summary = betty.summarize(request.messages, request.trip_context)
        logger.info("summarize done: %r", summary)
        return SummarizeResponse(summary=summary)
    except BettyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
