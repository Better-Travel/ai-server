"""OpenStreetMap place search exposed to Betty as a chat tool."""

import json
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from osm.llm import FallbackClient, make_clients
from osm.pipeline import Completer, QueryGenerationFailed, generate_and_run
from osm.runner import OverpassUnavailable
from osm.schemas import OverpassElement, PlacesInput
from osm.wikipedia import OUTPUT_LANGS, enrich

TOOL_NAME = "find_places"
MAX_RESULTS = 20
MAX_EXTRACT_CHARS = 300
KEPT_TAGS = ("amenity", "tourism", "historic", "leisure", "cuisine", "opening_hours", "website")

TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Look up real places (restaurants, cafes, bars, museums, viewpoints, cinemas, ...) "
            "in OpenStreetMap. Use it when the traveller wants concrete venue suggestions in a "
            "known city. Never invent venues; only mention places returned by this tool."
        ),
        "parameters": PlacesInput.model_json_schema(),
    },
}


def _default_completer() -> Completer:
    return FallbackClient(make_clients()).complete


class PlacesTool:
    """Validates tool arguments, runs the OSM query pipeline, and returns compact JSON text."""

    def __init__(self, completer_factory: Callable[[], Completer] = _default_completer) -> None:
        self._completer_factory = completer_factory
        self._completer: Completer | None = None

    def __call__(self, arguments: str) -> str:
        """Run the tool; failures are returned as text so the model can react to them."""
        try:
            places = PlacesInput.model_validate_json(arguments)
        except ValidationError as exc:
            return json.dumps({"error": f"Invalid arguments: {exc.errors(include_url=False)}"})
        try:
            if self._completer is None:
                self._completer = self._completer_factory()
            result = generate_and_run(places, self._completer)
        except (QueryGenerationFailed, OverpassUnavailable) as exc:
            return json.dumps({"error": f"Place search failed: {exc}"})
        except Exception as exc:  # LLM/network failures must not break the chat turn
            return json.dumps({"error": f"Place search unavailable: {exc}"})
        # Only the places Betty will see are looked up on Wikipedia; failures there are non-fatal.
        shown_elements = enrich(result.results[:MAX_RESULTS])
        shown = [_compact(e) for e in shown_elements]
        return json.dumps({"total": len(result.results), "shown": len(shown), "places": shown})


def _trim(extract: str) -> str:
    if len(extract) <= MAX_EXTRACT_CHARS:
        return extract
    return extract[:MAX_EXTRACT_CHARS].rsplit(" ", 1)[0] + "…"


def _compact(element: OverpassElement) -> dict[str, Any]:
    place: dict[str, Any] = {
        "name": element.name,
        "type": element.type,
        "lat": element.lat,
        "lon": element.lon,
        "tags": {k: v for k, v in element.tags.items() if k in KEPT_TAGS},
    }
    if element.wikipedia:
        place["wikipedia"] = {
            lang: {"title": text.title, "url": text.url, "extract": _trim(text.extract)}
            for lang in OUTPUT_LANGS
            if (text := getattr(element.wikipedia, lang))
        }
    return place
