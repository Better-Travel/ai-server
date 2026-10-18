"""FastAPI application entrypoint for Betty, the trip-planning guide."""

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.logging_config import configure_logging
from app.schemas import (
    AskRequest,
    AskResponse,
    ChatRequest,
    ChatResponse,
    NarrationResponse,
    NarrationSegment,
    Resume,
    SummarizeRequest,
    SummarizeResponse,
    WebSource,
)
from app.services.betty_service import BettyService
from app.services.guide_service import GuideService, InvalidCursor, PlaceNotFound
from app.services.llm_loop import BettyError
from app.services.narration import NarrationService
from osm.wikipedia import WikipediaUnavailable, is_valid_language

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


@lru_cache
def get_narration_service() -> NarrationService:
    """The shared narration service (it caches Wikipedia texts)."""
    return NarrationService()


NarrationDep = Annotated[NarrationService, Depends(get_narration_service)]


@lru_cache
def _build_guide(narrations: NarrationService) -> GuideService:
    return GuideService(get_settings(), narrations)


def get_guide_service(narrations: NarrationDep) -> GuideService:
    """Dependency providing the shared GuideService."""
    try:
        return _build_guide(narrations)
    except BettyError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


GuideDep = Annotated[GuideService, Depends(get_guide_service)]


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


@app.get("/api/v1/narration")
def narration(lang: str, title: str, narrations: NarrationDep) -> NarrationResponse:
    """The text to read aloud at a sight (Wikipedia, cleaned for speech), split into segments."""
    if not is_valid_language(lang):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown Wikipedia language.")
    try:
        result = narrations.narration(lang, title)
    except WikipediaUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Wikipedia: {exc}") from exc
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Wikipedia article with that title.")
    logger.info("narration %s:%s -> %d segments", lang, result.title, len(result.segments))
    return NarrationResponse(
        title=result.title,
        lang=result.lang,
        url=result.url,
        attribution=result.attribution,
        segments=[NarrationSegment(id=i, text=text) for i, text in enumerate(result.segments)],
    )


@app.post("/api/v1/narration/ask")
def ask(request: AskRequest, guide: GuideDep) -> AskResponse:
    """Answer a question asked while a segment was spoken; says where the narration resumes."""
    if not is_valid_language(request.place.lang):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown Wikipedia language.")
    logger.info(
        "POST /narration/ask: %s:%s cursor=%d question=%r",
        request.place.lang,
        request.place.title,
        request.cursor,
        request.question,
    )
    try:
        result = guide.ask(
            request.place.lang,
            request.place.title,
            request.cursor,
            request.question,
            request.history,
        )
    except PlaceNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidCursor as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except WikipediaUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"Wikipedia: {exc}") from exc
    except BettyError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    logger.info(
        "guide answer: %r used_web=%s resume=%s", result.answer, result.used_web, result.resume_from
    )
    return AskResponse(
        answer=result.answer,
        used_web=result.used_web,
        sources=[WebSource(title=s.title, url=s.url) for s in result.sources],
        resume=None
        if result.resume_from is None
        else Resume(from_segment=result.resume_from, bridge=result.bridge),
        finished=result.finished,
    )
