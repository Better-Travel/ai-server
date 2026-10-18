"""find_places: turn a plain-text trip summary into real OpenStreetMap places.

Not an LLM tool itself: Betty's `search_places` tool builds the summary and calls this.
"""

import hashlib
import logging
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.schemas import Place, WikipediaSnippet
from osm.fame import fame
from osm.llm import FallbackClient, make_clients
from osm.pipeline import Completer, NeedsMoreInfo, QueryGenerationFailed, generate_and_run
from osm.prompt import SIGHTS_SCOPE
from osm.runner import OverpassUnavailable
from osm.schemas import FOOD_AMENITIES, SIGHT_TAGS, OverpassElement
from osm.wikipedia import OUTPUT_LANGS, enrich

logger = logging.getLogger(__name__)

# Places sent back to the LLM (and the client). Larger result sets are ranked by fame and
# sampled (`select_places`); cropping is logged.
MAX_RESULTS = 30
# Places are sampled from this many of the best known, so a high temperature means "anything in
# the city's top 150", not "any bench in the city" (2,146 of 2,826 Warsaw sights have no fame).
SAMPLE_POOL = 150
# At most this many places of one kind (castle, museum, ...), while other kinds are left.
MAX_PER_KIND = 4
DEFAULT_TEMPERATURE = 0.2
# Scales how strongly fame wins at a given temperature. Measured on a broad Warsaw search: at 0.2
# 9 of the 10 best known places stay and about a third of the 30 are other picks; at 0.5 4 of 10.
# With 1 almost all the variety happened between 0 and 0.2 (only 5 of the 10 stayed at 0.2).
SHARPNESS = 3.0
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


class PlaceSearcher(Protocol):
    def __call__(self, request: str, temperature: float = DEFAULT_TEMPERATURE) -> FoundPlaces: ...


def _default_completer() -> Completer:
    return FallbackClient(make_clients()).complete


class PlaceFinder:
    """Runs the OSM query pipeline on a summary; failures become a status, never an exception."""

    def __init__(self, completer_factory: Callable[[], Completer] = _default_completer) -> None:
        self._completer_factory = completer_factory
        self._completer: Completer | None = None

    def __call__(self, request: str, temperature: float = DEFAULT_TEMPERATURE) -> FoundPlaces:
        """Search for places; `temperature` (0-1) trades the best known places for variety."""
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
            logger.info(
                "find_places returned %d places; sampling %d at temperature %.2f",
                len(unique),
                MAX_RESULTS,
                temperature,
            )
        # The same summary gives the same places; a different trip gives a different mix.
        rng = random.Random(hashlib.sha256(request.encode()).digest())
        # Only the places Betty will see are looked up on Wikipedia; failures there are non-fatal.
        shown = enrich(select_places(unique, MAX_RESULTS, temperature, rng))
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


def _score(element: OverpassElement) -> float:
    """Ranking weight: log of the fame, or a small bonus for a Wikipedia/Wikidata tag."""
    known = fame(element)
    if known:
        return math.log1p(known)
    return 0.5 if {"wikipedia", "wikidata"} & element.tags.keys() else 0.0


def select_places(
    elements: list[OverpassElement],
    limit: int,
    temperature: float = DEFAULT_TEMPERATURE,
    rng: random.Random | None = None,
) -> list[OverpassElement]:
    """Up to `limit` places, best known first, with variety controlled by `temperature`.

    Temperature 0 takes the best known places in order; higher values sample them (without
    replacement, softmax over the normalised scores, via the Gumbel-top-k trick) from the
    SAMPLE_POOL best known; 1 samples the pool uniformly. Then at most MAX_PER_KIND of one kind
    are taken while other kinds are left (the rest only fill remaining slots).
    """
    ranked = sorted(elements, key=lambda e: (-_score(e), -len(e.tags)))[:SAMPLE_POOL]
    temperature = min(max(temperature, 0.0), 1.0)
    if temperature > 0 and len(elements) > limit:
        rng = rng or random.Random()
        top = max(_score(e) for e in ranked) or 1.0
        sharpness = SHARPNESS * (1 - temperature) / temperature  # 0 at temperature 1 (uniform)

        def key(element: OverpassElement) -> float:
            gumbel = -math.log(-math.log(rng.random() or 1e-300))
            return sharpness * _score(element) / top + gumbel

        ranked = sorted(ranked, key=key, reverse=True)
    return cap_per_kind(ranked, limit)


def cap_per_kind(elements: list[OverpassElement], limit: int) -> list[OverpassElement]:
    """The first `limit` elements in order, at most MAX_PER_KIND per kind while others are left."""
    counts: dict[str, int] = {}
    picked: list[OverpassElement] = []
    skipped: list[OverpassElement] = []
    for element in elements:
        kind = _kind(element)
        if counts.get(kind, 0) < MAX_PER_KIND:
            counts[kind] = counts.get(kind, 0) + 1
            picked.append(element)
        else:
            skipped.append(element)
    picked = picked[:limit]
    return picked + skipped[: limit - len(picked)]


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
        fame=fame(element),
        wikipedia=wikipedia,
    )
