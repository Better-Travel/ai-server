"""find_places: turn a plain-text trip summary into real OpenStreetMap places.

Not an LLM tool itself: Betty's `search_places` tool builds the summary and calls this.
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.schemas import Place, WikipediaSnippet
from osm.llm import FallbackClient, make_clients
from osm.pipeline import Completer, NeedsMoreInfo, QueryGenerationFailed, generate_and_run
from osm.prompt import SIGHTS_SCOPE
from osm.runner import OverpassUnavailable
from osm.schemas import FOOD_AMENITIES, SIGHT_TAGS, OverpassElement
from osm.wikipedia import OUTPUT_LANGS, enrich

logger = logging.getLogger(__name__)

# Interim cap on places sent back to the LLM (and the client). Larger result sets are cropped to
# the notable ones first (`select_notable_first`) and logged as a warning, until ranking exists.
MAX_RESULTS = 30
MAX_EXTRACT_CHARS = 300
MAX_REQUEST_CHARS = 2000
KEPT_TAGS = (
    "amenity",
    "tourism",
    "historic",
    "leisure",
    "cuisine",
    "opening_hours",
    "website",
    "contact:website",
    "phone",
    "contact:phone",
    "name:en",
    "addr:street",
    "addr:housenumber",
    "addr:postcode",
    "wikidata",
    "wheelchair",
    "diet:vegetarian",
    "diet:vegan",
    "fee",
)
# Tag keys that say what kind of place something is, in the order used to classify it.
KIND_KEYS = ("historic", "tourism", "amenity", "leisure", "natural", "man_made", "shop")
# Elements with the same name closer than this are one place mapped twice (see drop_duplicates).
DUPLICATE_DISTANCE_M = 100.0
_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class FoundPlaces:
    """Result of one search. `message` explains a `needs_info` or `error` status."""

    status: Literal["ok", "needs_info", "error"]
    total: int = 0
    places: list[Place] = field(default_factory=list)
    message: str | None = None
    query: str | None = None  # the Overpass QL that produced (or last failed to produce) results
    attempts: int = 0  # how many queries the model wrote (1 = no syntax fixes needed)
    fetched: int = 0  # raw elements Overpass returned, before duplicates are dropped


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
            result = generate_and_run(
                request[:MAX_REQUEST_CHARS],
                self._completer,
                allowed_tags=SIGHT_TAGS,
                scope=SIGHTS_SCOPE,
            )
        except NeedsMoreInfo as exc:
            return FoundPlaces(
                status="needs_info", message=str(exc) or "destination or country unclear"
            )
        except QueryGenerationFailed as exc:
            return FoundPlaces(
                status="error",
                message=f"Place search failed: {exc}",
                query=exc.query,
                attempts=exc.attempts,
            )
        except OverpassUnavailable as exc:
            return FoundPlaces(status="error", message=f"Place search failed: {exc}")
        except Exception as exc:  # LLM/network failures must not break the chat turn
            logger.exception("place search crashed")
            return FoundPlaces(status="error", message=f"Place search unavailable: {exc}")
        unique = drop_duplicates(result.results)
        if len(unique) < len(result.results):
            logger.info("dropped %d duplicate places", len(result.results) - len(unique))
        sights = [e for e in unique if not is_food(e)]
        if len(sights) < len(unique):
            logger.info("dropped %d food and drink places", len(unique) - len(sights))
        unique = sights
        if len(unique) > MAX_RESULTS:
            # TODO: handle large result sets properly (narrower queries or real ranking).
            logger.warning(
                "find_places returned %d places; cropping to the %d best for the LLM. Request: %r",
                len(unique),
                MAX_RESULTS,
                request[:MAX_REQUEST_CHARS],
            )
        # Only the places Betty will see are looked up on Wikipedia; failures there are non-fatal.
        shown = enrich(select_notable_first(unique, MAX_RESULTS))
        return FoundPlaces(
            status="ok",
            total=len(unique),
            places=[_to_place(e) for e in shown],
            query=result.query,
            attempts=result.attempts,
            fetched=len(result.results),
        )


def _kind(element: OverpassElement) -> str:
    for key in KIND_KEYS:
        if key in element.tags:
            return f"{key}={element.tags[key]}"
    return "other"


def _near(a: OverpassElement, b: OverpassElement) -> bool:
    """True if both have coordinates less than DUPLICATE_DISTANCE_M apart."""
    if a.lat is None or a.lon is None or b.lat is None or b.lon is None:
        return False
    # Equirectangular approximation: accurate to well under 1 m at this distance.
    dx = math.radians(b.lon - a.lon) * math.cos(math.radians((a.lat + b.lat) / 2))
    dy = math.radians(b.lat - a.lat)
    return _EARTH_RADIUS_M * math.hypot(dx, dy) < DUPLICATE_DISTANCE_M


def drop_duplicates(elements: list[OverpassElement]) -> list[OverpassElement]:
    """Drop elements that map a place another element already covers; keep the one with more tags.

    OSM often maps one place twice, e.g. the Royal Castle as a building and as a museum point
    inside it. Two elements are the same place if they share a `wikidata` tag, or have the same
    name and are less than DUPLICATE_DISTANCE_M apart. The original order is kept.
    """
    wikidata_seen: set[str] = set()
    kept_by_name: dict[str, list[OverpassElement]] = {}
    kept: set[int] = set()
    # Most tags first (stable, so ties keep the earlier element).
    for index in sorted(range(len(elements)), key=lambda i: -len(elements[i].tags)):
        element = elements[index]
        wikidata = element.tags.get("wikidata", "").strip()
        name = (element.name or "").strip().casefold()
        if wikidata in wikidata_seen:
            continue
        same_name = kept_by_name.setdefault(name, []) if name else []
        if any(_near(element, other) for other in same_name):
            continue
        kept.add(index)
        same_name.append(element)
        if wikidata:
            wikidata_seen.add(wikidata)
    return [element for index, element in enumerate(elements) if index in kept]


def is_food(element: OverpassElement) -> bool:
    """True for food and drink places (restaurant, cafe, bar, ...), unless also a listed sight.

    OSM often tags one object twice, e.g. a hotel or a bus-shaped kiosk as `amenity=cafe`. A place
    is kept if it also carries a sight tag we search for (a catalogued tourism/historic/natural/
    man_made value), so a castle that houses a restaurant stays.
    """
    if element.tags.get("amenity") not in FOOD_AMENITIES:
        return False
    return not any(
        element.tags.get(key) in SIGHT_TAGS[key]
        for key in ("tourism", "historic", "natural", "man_made")
    )


def select_notable_first(elements: list[OverpassElement], limit: int) -> list[OverpassElement]:
    """Up to `limit` places: the notable ones (Wikipedia/Wikidata tag) first, then the rest.

    Each tier is picked with `select_diverse`, so no single kind fills every slot.
    """
    notable = [e for e in elements if {"wikipedia", "wikidata"} & e.tags.keys()]
    rest = [e for e in elements if not {"wikipedia", "wikidata"} & e.tags.keys()]
    picked = select_diverse(notable, limit)
    return picked + select_diverse(rest, limit - len(picked))


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
        osm_id=f"{element.type}/{element.id}",
        name=element.name,
        type=element.type,
        category=_kind(element),
        lat=element.lat,
        lon=element.lon,
        tags={k: v for k, v in element.tags.items() if k in KEPT_TAGS},
        wikipedia=wikipedia,
    )
