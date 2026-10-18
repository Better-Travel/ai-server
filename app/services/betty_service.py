"""Betty: the trip-planning assistant backed by NVIDIA's OpenAI-compatible API."""

import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel

from app.config import Settings
from app.prompts import (
    EVENT_PICK_SYSTEM_PROMPT,
    PREFERENCES_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    build_chat_prompt,
)
from app.schemas import (
    ChatResponse,
    Event,
    EventSearch,
    Message,
    PlaceSearch,
    StreamDelta,
    StreamStatus,
    Trip,
)
from app.services.event_finder import (
    MAX_EVENTS,
    EventFinder,
    EventSearcher,
    describe_candidates,
    parse_picks,
    trip_dates,
)
from app.services.llm_loop import (
    BettyError,
    Finished,
    TextDelta,
    ToolFinished,
    ToolLoop,
    ToolStarted,
)
from app.services.place_finder import (
    DEFAULT_TEMPERATURE,
    FoundPlaces,
    PlaceFinder,
    PlaceSearcher,
)
from app.services.route_planner import RouteBuilder, RoutePlanner
from app.services.search_places_tool import TOOL_NAME as SEARCH_TOOL_NAME
from app.services.search_places_tool import TOOL_SPEC as SEARCH_TOOL_SPEC
from app.services.search_places_tool import parse_trip, rejected_result, search_result

MAX_PREFERENCES_CHARS = 800
NO_PREFERENCES = "NONE"


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    """Outcome of one chat turn."""

    reply: str
    search: PlaceSearch | None = None
    events: EventSearch | None = None


@dataclass(frozen=True)
class ChatEvent:
    """One event of a streamed chat turn: its SSE event name and its JSON body."""

    name: Literal["status", "delta", "search", "events", "done"]
    data: BaseModel


class BettyService:
    """Wraps the OpenAI SDK client to provide chat, summarisation and place search."""

    def __init__(
        self,
        settings: Settings,
        client: OpenAI | None = None,
        place_finder: PlaceSearcher | None = None,
        event_finder: EventSearcher | None = None,
        route_planner: RoutePlanner | None = None,
    ) -> None:
        self._settings = settings
        self._find_events: EventSearcher | None = (
            (
                event_finder
                if event_finder is not None
                else EventFinder(settings.backend_url, settings.request_timeout_seconds)
            )
            if settings.betty_events_tool
            else None
        )
        self._find_places: PlaceSearcher | None = (
            (place_finder if place_finder is not None else PlaceFinder())
            if settings.betty_places_tool
            else None
        )
        self._plan_route: RoutePlanner = (
            route_planner if route_planner is not None else RouteBuilder()
        )
        if client is None:
            if not settings.nvidia_api_key_2:
                raise BettyError("NVIDIA_API_KEY_2 is not set.")
            client = OpenAI(
                api_key=settings.nvidia_api_key_2,
                base_url=settings.nvidia_base_url,
                timeout=settings.request_timeout_seconds,
            )
        self._client = client
        self._loop = ToolLoop(
            client,
            settings.betty_model,
            max_rounds=settings.betty_max_tool_rounds,
            temperature=settings.betty_temperature,
            max_tokens=settings.betty_max_tokens,
        )

    def chat(
        self, messages: Sequence[Message], places_temperature: float | None = None
    ) -> ChatResult:
        """Return Betty's next reply; `search` is set when she had enough info and searched.

        `places_temperature` (0-1) overrides PLACES_TEMPERATURE for this turn's search.
        """
        searches: list[PlaceSearch] = []
        event_searches: list[EventSearch] = []
        handler = self._search_handler(messages, searches, event_searches, places_temperature)
        reply = self._complete(
            build_chat_prompt(date.today()),
            messages,
            tools=[SEARCH_TOOL_SPEC],
            handlers={SEARCH_TOOL_NAME: handler},
        )
        return ChatResult(
            reply=reply,
            search=searches[-1] if searches else None,
            events=event_searches[-1] if event_searches else None,
        )

    def chat_stream(
        self, messages: Sequence[Message], places_temperature: float | None = None
    ) -> Generator[ChatEvent]:
        """Betty's next reply as it is written, for clients that show or speak it right away.

        Yields `delta` events with the reply text, `status` while she searches, `search` and
        `events` as soon as they exist, and finally `done` with the same body `chat()` gives.
        Raises BettyError when the model fails, possibly after some deltas. Closing the
        generator closes the model stream (see ToolLoop.stream).
        """
        searches: list[PlaceSearch] = []
        event_searches: list[EventSearch] = []
        found_before = events_before = 0
        handler = self._search_handler(messages, searches, event_searches, places_temperature)
        loop = self._loop.stream(
            build_chat_prompt(date.today()),
            messages,
            tools=[SEARCH_TOOL_SPEC],
            handlers={SEARCH_TOOL_NAME: handler},
        )
        try:
            for event in loop:
                if isinstance(event, TextDelta):
                    yield ChatEvent("delta", StreamDelta(text=event.text))
                elif isinstance(event, ToolStarted) and event.name == SEARCH_TOOL_NAME:
                    found_before, events_before = len(searches), len(event_searches)
                    yield ChatEvent("status", StreamStatus(stage="searching_places"))
                elif isinstance(event, ToolFinished) and event.name == SEARCH_TOOL_NAME:
                    if len(searches) > found_before:
                        yield ChatEvent("search", searches[-1])
                    if len(event_searches) > events_before:
                        yield ChatEvent("events", event_searches[-1])
                elif isinstance(event, Finished):
                    yield ChatEvent(
                        "done",
                        ChatResponse(
                            reply=event.reply,
                            search=searches[-1] if searches else None,
                            events=event_searches[-1] if event_searches else None,
                        ),
                    )
        finally:
            loop.close()

    def _search_handler(
        self,
        messages: Sequence[Message],
        searches: list[PlaceSearch],
        event_searches: list[EventSearch],
        places_temperature: float | None = None,
    ) -> Callable[[str], str]:
        """The search_places tool for one turn; each search is appended to the given lists."""

        def search_places(arguments: str) -> str:
            trip = parse_trip(arguments)
            if not isinstance(trip, Trip):
                return rejected_result(trip)
            summary = self._summary(messages, trip)
            events = self._events_for(trip, summary)
            if events is not None:
                event_searches.append(events)
            temperature = (
                self._settings.places_temperature
                if places_temperature is None
                else places_temperature
            )
            search, tool_result = self._search(trip, summary, events, temperature)
            searches.append(search)
            return tool_result

        return search_places

    def summarize(self, messages: Sequence[Message], trip: Trip | None = None) -> str:
        """Condense the conversation into one plain-text paragraph.

        With a validated `trip`, the paragraph starts with `trip.describe()` (built by code, so
        the destination and dates can't drift) and the model only adds stated preferences.
        """
        transcript = "\n".join(
            f"{'Traveller' if m.role == 'user' else 'Betty'}: {m.content}" for m in messages
        )
        request = Message(role="user", content=f"Conversation:\n{transcript}")
        if trip is None:
            # Collapse any stray line breaks so the output is guaranteed to be one paragraph.
            return " ".join(
                self._complete(SUMMARY_SYSTEM_PROMPT, [request], temperature=0.2).split()
            )
        preferences = " ".join(
            self._complete(PREFERENCES_SYSTEM_PROMPT, [request], temperature=0.2).split()
        )
        if not preferences or preferences.rstrip(".").upper() == NO_PREFERENCES:
            return trip.describe()
        return f"{trip.describe()} {preferences[:MAX_PREFERENCES_CHARS]}"

    def _events_for(self, trip: Trip, summary: str) -> EventSearch | None:
        """Up to MAX_EVENTS relevant events on the trip's specific dates (Warsaw only)."""
        dates = trip_dates(trip)
        if self._find_events is None or dates is None:
            return None
        logger.info("find_events: %s %s..%s", trip.city, *dates)
        found = self._find_events(trip.city, *dates)
        if found.status == "unsupported":
            return None
        return EventSearch(
            city=trip.city,
            from_date=dates[0],
            to_date=dates[1],
            total=found.total,
            events=self._pick_events(summary, found.events),
            error=None if found.status == "ok" else found.message,
        )

    def _pick_events(self, summary: str, candidates: list[Event]) -> list[Event]:
        """The MAX_EVENTS candidates that fit the traveller best (the model chooses).

        If the model fails or answers with something unusable, the first candidates win: they
        are already ranked (dated, traveller-friendly categories first, a mix of kinds).
        """
        if len(candidates) <= 1:
            return candidates
        request = Message(
            role="user",
            content=f"Traveller: {summary}\n\nEvents:\n{describe_candidates(candidates)}",
        )
        try:
            reply = self._complete(EVENT_PICK_SYSTEM_PROMPT, [request], temperature=0.0)
            picks = parse_picks(reply, len(candidates))
        except (BettyError, ValueError) as exc:
            logger.warning("event relevance pick failed, using the ranked order: %s", exc)
            picks = None
        if picks is None:
            return candidates[:MAX_EVENTS]
        logger.info("event picks: %s of %d candidates", [p + 1 for p in picks], len(candidates))
        return [candidates[i] for i in picks]

    def _summary(self, messages: Sequence[Message], trip: Trip) -> str:
        """The conversation summary sent to find_places; the trip facts alone if that fails."""
        try:
            return self.summarize(messages, trip)
        except BettyError as exc:
            # The validated facts alone are still a correct (if less specific) search.
            logger.warning("summary failed, searching with the trip facts only: %s", exc)
            return trip.describe()

    def _search(
        self,
        trip: Trip,
        summary: str,
        events: EventSearch | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> tuple[PlaceSearch, str]:
        """Run find_places on the summary; return the outcome + the text Betty sees."""
        if self._find_places is None:
            found = FoundPlaces(
                status="error", message="Place search is disabled (BETTY_PLACES_TOOL=false)."
            )
        else:
            logger.info("find_places request (temperature %.2f): %r", temperature, summary)
            found = self._find_places(summary, temperature)
        logger.info(
            "find_places result: status=%s total=%d shown=%d message=%r",
            found.status,
            found.total,
            len(found.places),
            found.message,
        )
        routes = self._plan_route(summary, found.places) if found.places else []
        if routes:
            logger.info(
                "route: %s (%.2f km)",
                [stop.name for stop in routes[0].stops],
                routes[0].distance_km,
            )
        search = PlaceSearch(
            trip=trip,
            summary=summary,
            query=found.query,
            query_attempts=found.attempts,
            fetched=found.fetched,
            total=found.total,
            places=found.places,
            routes=routes,
            error=None if found.status == "ok" else found.message,
        )
        return search, search_result(summary, found, events, routes)

    def _complete(
        self,
        system_prompt: str,
        messages: Sequence[Message],
        temperature: float | None = None,
        tools: Sequence[dict[str, Any]] = (),
        handlers: Mapping[str, Callable[[str], str]] | None = None,
    ) -> str:
        return self._loop.complete(system_prompt, messages, temperature, tools, handlers)
