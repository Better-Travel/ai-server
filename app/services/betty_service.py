"""Betty: the trip-planning assistant backed by NVIDIA's OpenAI-compatible API."""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.config import Settings
from app.prompts import (
    PREFERENCES_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    build_chat_prompt,
)
from app.schemas import Message, PlaceSearch, Trip
from app.services.place_finder import FoundPlaces, PlaceFinder, PlaceSearcher
from app.services.route_planner import RouteBuilder, RoutePlanner
from app.services.search_places_tool import TOOL_NAME as SEARCH_TOOL_NAME
from app.services.search_places_tool import TOOL_SPEC as SEARCH_TOOL_SPEC
from app.services.search_places_tool import parse_trip, rejected_result, search_result

MAX_PREFERENCES_CHARS = 800
NO_PREFERENCES = "NONE"


class BettyError(RuntimeError):
    """Raised when Betty cannot produce a response."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    """Outcome of one chat turn."""

    reply: str
    search: PlaceSearch | None = None


class BettyService:
    """Wraps the OpenAI SDK client to provide chat, summarisation and place search."""

    def __init__(
        self,
        settings: Settings,
        client: OpenAI | None = None,
        place_finder: PlaceSearcher | None = None,
        route_planner: RoutePlanner | None = None,
    ) -> None:
        self._settings = settings
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

    def chat(self, messages: Sequence[Message]) -> ChatResult:
        """Return Betty's next reply; `search` is set when she had enough info and searched."""
        searches: list[PlaceSearch] = []

        def search_places(arguments: str) -> str:
            trip = parse_trip(arguments)
            if not isinstance(trip, Trip):
                return rejected_result(trip)
            search, tool_result = self._search(messages, trip)
            searches.append(search)
            return tool_result

        reply = self._complete(
            build_chat_prompt(date.today()),
            messages,
            tools=[SEARCH_TOOL_SPEC],
            handlers={SEARCH_TOOL_NAME: search_places},
        )
        return ChatResult(reply=reply, search=searches[-1] if searches else None)

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

    def _search(self, messages: Sequence[Message], trip: Trip) -> tuple[PlaceSearch, str]:
        """Summarise the conversation, run find_places on it; return the outcome + tool text."""
        try:
            summary = self.summarize(messages, trip)
        except BettyError as exc:
            # The validated facts alone are still a correct (if less specific) search.
            logger.warning("summary failed, searching with the trip facts only: %s", exc)
            summary = trip.describe()
        if self._find_places is None:
            found = FoundPlaces(
                status="error", message="Place search is disabled (BETTY_PLACES_TOOL=false)."
            )
        else:
            logger.info("find_places request: %r", summary)
            found = self._find_places(summary)
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
        return search, search_result(summary, found, routes)

    def _complete(
        self,
        system_prompt: str,
        messages: Sequence[Message],
        temperature: float | None = None,
        tools: Sequence[dict[str, Any]] = (),
        handlers: Mapping[str, Callable[[str], str]] | None = None,
    ) -> str:
        payload: list[ChatCompletionMessageParam] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role == "user":
                payload.append({"role": "user", "content": m.content})
            else:
                payload.append({"role": "assistant", "content": m.content})
        handlers = handlers or {}
        max_rounds = self._settings.betty_max_tool_rounds
        retried_empty = False
        for round_number in range(max_rounds + 1):
            kwargs: dict[str, Any] = {}
            # On the final round stop offering tools so the model must answer in text.
            if tools and round_number < max_rounds:
                kwargs["tools"] = list(tools)
            logger.debug("LLM request round %d: %s", round_number, payload)
            try:
                response = self._client.chat.completions.create(
                    model=self._settings.betty_model,
                    messages=payload,
                    temperature=self._settings.betty_temperature
                    if temperature is None
                    else temperature,
                    **kwargs,
                )
            except Exception as exc:  # openai raises a broad family of API/connection errors
                raise BettyError(f"Upstream model request failed: {exc}") from exc
            message = response.choices[0].message if response.choices else None
            tool_calls = getattr(message, "tool_calls", None) if tools else None
            logger.info(
                "LLM response (model=%s round=%d): %r tool_calls=%s",
                self._settings.betty_model,
                round_number,
                message.content if message else None,
                [(c.function.name, c.function.arguments) for c in tool_calls]
                if tool_calls
                else None,
            )
            if not tool_calls:
                content = message.content if message else None
                if not content or not content.strip():
                    # The NVIDIA endpoint occasionally returns no content; one retry fixes it.
                    if not retried_empty:
                        retried_empty = True
                        logger.warning("empty model response, retrying once")
                        continue
                    raise BettyError("Model returned an empty response.")
                return str(content).strip()
            assert message is not None
            payload.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                handler = handlers.get(call.function.name)
                if handler is None:
                    result = json.dumps({"error": f"Unknown tool {call.function.name}"})
                else:
                    result = handler(call.function.arguments)
                logger.info("tool %s -> %s", call.function.name, result[:500])
                payload.append({"role": "tool", "tool_call_id": call.id, "content": result})
        raise BettyError("Model did not produce a final answer.")
