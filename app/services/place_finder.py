"""find_places: turn a plain-text trip summary into real OpenStreetMap places.

Not an LLM tool itself: Betty's `search_places` tool builds the summary and calls this.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.schemas import Place, WikipediaSnippet
from osm.llm import FallbackClient, make_clients
from osm.pipeline import Completer, NeedsMoreInfo, QueryGenerationFailed, generate_and_run
from osm.runner import OverpassUnavailable
from osm.schemas import OverpassElement
from osm.wikipedia import OUTPUT_LANGS, enrich

logger = logging.getLogger(__name__)

# Interim cap on places sent back to the LLM (and the client). Larger result sets are cropped to
# the "best" ones by `select_diverse` and logged as a warning, until narrowing/ranking exists.
MAX_RESULTS = 20
MAX_EXTRACT_CHARS = 300
MAX_REQUEST_CHARS = 2000
KEPT_TAGS = ("amenity", "tourism", "historic", "leisure", "cuisine", "opening_hours", "website")
# Tag keys that say what kind of place something is, in the order used to classify it.
KIND_KEYS = ("historic", "tourism", "amenity", "leisure", "natural", "man_made", "shop")


@dataclass(frozen=True)
class FoundPlaces:
    """Result of one search. `message` explains a `needs_info` or `error` status."""

    status: Literal["ok", "needs_info", "error"]
    total: int = 0
    places: list[Place] = field(default_factory=list)
    message: str | None = None


PlaceSearcher = Callable[[str], FoundPlaces]


def _default_completer() -> Completer:
    return FallbackClient(make_clients()).complete


class PlaceFinder:
    """Runs the OSM query pipeline on a summary; failures become a status, never an exception."""

    def __init__(self, completer_factory: Callable[[], Completer] = _default_completer) -> None:
        self._completer_factory = completer_factory
        self._completer: Completer | None = None

    def __call__(self, request: str) -> FoundPlaces:
        if not request.strip():
            return FoundPlaces(status="error", message="The search request is empty.")
        try:
            if self._completer is None:
                self._completer = self._completer_factory()
            result = generate_and_run(request[:MAX_REQUEST_CHARS], self._completer)
        except NeedsMoreInfo as exc:
            return FoundPlaces(
                status="needs_info", message=str(exc) or "destination or country unclear"
            )
        except (QueryGenerationFailed, OverpassUnavailable) as exc:
            return FoundPlaces(status="error", message=f"Place search failed: {exc}")
        except Exception as exc:  # LLM/network failures must not break the chat turn
            logger.exception("place search crashed")
            return FoundPlaces(status="error", message=f"Place search unavailable: {exc}")
        if len(result.results) > MAX_RESULTS:
            # TODO: handle large result sets properly (narrower queries or real ranking).
            logger.warning(
                "find_places returned %d places; cropping to the %d best for the LLM. Request: %r",
                len(result.results),
                MAX_RESULTS,
                request[:MAX_REQUEST_CHARS],
            )
        # Only the places Betty will see are looked up on Wikipedia; failures there are non-fatal.
        shown = enrich(select_diverse(result.results, MAX_RESULTS))
        return FoundPlaces(
            status="ok", total=len(result.results), places=[_to_place(e) for e in shown]
        )


def _kind(element: OverpassElement) -> str:
    for key in KIND_KEYS:
        if key in element.tags:
            return f"{key}={element.tags[key]}"
    return "other"


def select_diverse(elements: list[OverpassElement], limit: int) -> list[OverpassElement]:
    """Pick up to `limit` places, taking turns between kinds (castle, palace, cafe, ...).

    Overpass can return hundreds of results and one kind (e.g. cafes) would otherwise fill every
    slot. Within a kind, places with a Wikipedia/Wikidata tag (usually the notable ones) go first.
    """
    groups: dict[str, list[OverpassElement]] = {}
    for element in elements:
        groups.setdefault(_kind(element), []).append(element)
    for group in groups.values():
        group.sort(key=lambda e: not ({"wikipedia", "wikidata"} & e.tags.keys()))
    picked: list[OverpassElement] = []
    while len(picked) < limit and any(groups.values()):
        for group in groups.values():
            if group and len(picked) < limit:
                picked.append(group.pop(0))
    return picked


def _trim(extract: str) -> str:
    if len(extract) <= MAX_EXTRACT_CHARS:
        return extract
    return extract[:MAX_EXTRACT_CHARS].rsplit(" ", 1)[0] + "…"


def _to_place(element: OverpassElement) -> Place:
    wikipedia = None
    if element.wikipedia:
        wikipedia = {
            lang: WikipediaSnippet(title=text.title, url=text.url, extract=_trim(text.extract))
            for lang in OUTPUT_LANGS
            if (text := getattr(element.wikipedia, lang))
        } or None
    return Place(
        name=element.name,
        type=element.type,
        lat=element.lat,
        lon=element.lon,
        tags={k: v for k, v in element.tags.items() if k in KEPT_TAGS},
        wikipedia=wikipedia,
    )
