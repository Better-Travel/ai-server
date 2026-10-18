"""plan_route: pick 5-10 of the found places and order them into a walking route.

One LLM call chooses the must-visit places for this traveller and scores the rest. Code then
orders the must-visits into the shortest path and adds the scored places that lie on the way,
best score per metre of detour first. If the LLM fails, the first places in find_places order
(Wikipedia-tagged, a mix of kinds) are the must-visits instead, so there is always a route.
"""

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

from app.prompts import ROUTE_SYSTEM_PROMPT
from app.schemas import Place, Route, RouteBuildStep, RouteStop
from osm.llm import FallbackClient, make_clients
from osm.pipeline import Completer

logger = logging.getLogger(__name__)

MIN_STOPS = 5
MAX_STOPS = 10
MAX_MUST = 5
FALLBACK_MUST = 3
# A "maybe" place is added only if it lengthens the route by less than this (unless the route
# is still shorter than MIN_STOPS, then the best value wins whatever the detour).
MAX_DETOUR_M = 800.0
# Added to every detour, so a place right on the path doesn't win by dividing by ~0.
DETOUR_BASE_M = 200.0
# Each stop already in the route with the same category multiplies a candidate's value by this.
REPEAT_PENALTY = 0.5
MAX_DESCRIPTION_CHARS = 160
MAX_REQUEST_CHARS = 2000
_EARTH_RADIUS_M = 6_371_000.0

RoutePlanner = Callable[[str, list[Place]], list[Route]]


@dataclass(frozen=True)
class Pick:
    """A place the route may use: `score` is the LLM's 1-3 fit (3 for must-visits)."""

    place: Place
    why: str | None = None
    score: float = 3.0


def _default_completer() -> Completer:
    return FallbackClient(make_clients()).complete


class RouteBuilder:
    """Builds the route for one search; LLM failures fall back to code, never an exception."""

    def __init__(self, completer_factory: Callable[[], Completer] = _default_completer) -> None:
        self._completer_factory = completer_factory
        self._completer: Completer | None = None

    def __call__(self, request: str, places: list[Place]) -> list[Route]:
        located = [p for p in places if p.lat is not None and p.lon is not None]
        if not located:
            return []
        try:
            if self._completer is None:
                self._completer = self._completer_factory()
            reply = self._completer(
                [
                    {"role": "system", "content": ROUTE_SYSTEM_PROMPT},
                    {"role": "user", "content": build_request(request, located)},
                ]
            )
            must, maybe = parse_choice(reply, located)
        except Exception as exc:  # LLM/network/JSON failures must not break the chat turn
            logger.warning("route choice failed, using find_places order: %s", exc)
            must, maybe = [], []
        if not must:
            must, maybe = fallback_choice(located)
        return [build_route(must, maybe)]


def distance_m(a: Place, b: Place) -> float:
    """Equirectangular approximation: well under 1% off at city scale."""
    assert a.lat is not None and a.lon is not None and b.lat is not None and b.lon is not None
    dx = math.radians(b.lon - a.lon) * math.cos(math.radians((a.lat + b.lat) / 2))
    dy = math.radians(b.lat - a.lat)
    return _EARTH_RADIUS_M * math.hypot(dx, dy)


def _display_name(place: Place) -> str:
    name = place.name or "(unnamed)"
    english = place.tags.get("name:en")
    return f"{english} ({name})" if english and english != name else name


def _description(place: Place) -> str:
    snippet = (place.wikipedia or {}).get("en") or (place.wikipedia or {}).get("pl")
    if snippet is None:
        return ""
    text = " ".join(snippet.extract.split())
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def build_request(request: str, places: list[Place]) -> str:
    """User message for the LLM: the wishes, then one line per place with id `p<n>` (1-based)."""
    centre = Place(
        osm_id="centre",
        type="node",
        category="other",
        lat=sum(p.lat or 0 for p in places) / len(places),
        lon=sum(p.lon or 0 for p in places) / len(places),
    )
    lines = [
        f"p{i} | {_display_name(p)} | {p.category} | "
        f"{distance_m(p, centre) / 1000:.1f} km from centre | {_description(p)}"
        for i, p in enumerate(places, start=1)
    ]
    return f"Traveller's wishes: {request[:MAX_REQUEST_CHARS]}\n\nCandidates:\n" + "\n".join(lines)


def parse_choice(text: str, places: list[Place]) -> tuple[list[Pick], list[Pick]]:
    """Read the LLM's JSON; drop unknown and repeated ids; extra must-visits become maybes."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in the reply: {text[:200]!r}")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("the reply is not a JSON object")
    by_id = {f"p{i}": p for i, p in enumerate(places, start=1)}
    used: set[str] = set()

    def picks(key: str, default_score: float) -> list[Pick]:
        found: list[Pick] = []
        items = data.get(key)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            place_id = str(item.get("id", "")).strip()
            if place_id not in by_id or place_id in used:
                continue
            used.add(place_id)
            try:
                score = float(item.get("score", default_score))
            except (TypeError, ValueError):
                score = default_score
            why = item.get("why")
            found.append(
                Pick(
                    place=by_id[place_id],
                    why=why.strip() if isinstance(why, str) and why.strip() else None,
                    score=min(max(score, 1.0), 3.0),
                )
            )
        return found

    must = picks("must", 3.0)
    maybe = must[MAX_MUST:] + picks("maybe", 1.0)
    return must[:MAX_MUST], maybe


def fallback_choice(places: list[Place]) -> tuple[list[Pick], list[Pick]]:
    """Without the LLM: the first places (find_places puts notable ones first) are the anchors."""
    picks = [Pick(place=p, score=1.0) for p in places]
    return picks[:FALLBACK_MUST], picks[FALLBACK_MUST:]


def _path_length(path: list[Pick]) -> float:
    return sum(distance_m(a.place, b.place) for a, b in zip(path, path[1:], strict=False))


def order_path(stops: list[Pick]) -> list[Pick]:
    """Shortest open path found by nearest neighbour from every start, then 2-opt.

    With at most MAX_STOPS stops this takes milliseconds and is close to optimal.
    """
    n = len(stops)
    if n < 3:
        return list(stops)
    dist = [[distance_m(a.place, b.place) for b in stops] for a in stops]

    def length(order: list[int]) -> float:
        return sum(dist[a][b] for a, b in zip(order, order[1:], strict=False))

    best: list[int] = []
    best_length = math.inf
    for start in range(n):
        order = [start]
        left = set(range(n)) - {start}
        while left:
            nearest = min(left, key=lambda j: (dist[order[-1]][j], j))
            order.append(nearest)
            left.remove(nearest)
        improved = True
        while improved:
            improved = False
            for i in range(n - 1):
                for j in range(i + 1, n):
                    candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                    if length(candidate) < length(order) - 1e-6:
                        order, improved = candidate, True
        if length(order) < best_length - 1e-6:
            best, best_length = order, length(order)
    return [stops[i] for i in best]


def cheapest_insertion(path: list[Pick], pick: Pick) -> tuple[float, int]:
    """Extra metres to add `pick` at its best position in `path`, and that position."""
    if not path:
        return 0.0, 0
    place = pick.place
    options = [
        (distance_m(place, path[0].place), 0),
        (distance_m(path[-1].place, place), len(path)),
    ]
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False), start=1):
        detour = distance_m(a.place, place) + distance_m(place, b.place)
        options.append((detour - distance_m(a.place, b.place), i))
    return min(options)


def build_route(must: list[Pick], maybe: list[Pick]) -> Route:
    """Order the must-visits, then add maybes by score per metre of detour, up to MAX_STOPS."""

    def ids(picks: list[Pick]) -> list[str]:
        return [p.place.osm_id for p in picks]

    path = order_path(must)
    steps = [RouteBuildStep(action="anchors", order=ids(path))]
    must_ids = {p.place.osm_id for p in must}
    candidates = list(maybe)
    while len(path) < MAX_STOPS and candidates:
        best: tuple[float, Pick, int, float] | None = None
        for pick in candidates:
            detour, position = cheapest_insertion(path, pick)
            if detour > MAX_DETOUR_M and len(path) >= MIN_STOPS:
                continue
            repeats = sum(s.place.category == pick.place.category for s in path)
            value = pick.score * REPEAT_PENALTY**repeats / (detour + DETOUR_BASE_M)
            if best is None or value > best[0]:
                best = (value, pick, position, detour)
        if best is None:
            break
        _, pick, position, detour = best
        path.insert(position, pick)
        candidates.remove(pick)
        steps.append(
            RouteBuildStep(
                action="add", osm_id=pick.place.osm_id, detour_m=round(detour), order=ids(path)
            )
        )
    ordered = order_path(path)
    if ids(ordered) != ids(path):
        steps.append(RouteBuildStep(action="reorder", order=ids(ordered)))
    return Route(
        stops=[
            RouteStop(
                osm_id=s.place.osm_id,
                name=s.place.name,
                category=s.place.category,
                lat=s.place.lat or 0.0,
                lon=s.place.lon or 0.0,
                role="must" if s.place.osm_id in must_ids else "on_the_way",
                why=s.why,
            )
            for s in ordered
        ],
        distance_km=round(_path_length(ordered) / 1000, 2),
        steps=steps,
    )
