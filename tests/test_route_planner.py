"""plan_route (RouteBuilder): LLM choice parsing, ordering and on-the-way insertion (no network)."""

import json
from typing import Any

import pytest

from app.schemas import Place, WikipediaSnippet
from app.services.route_planner import (
    MAX_STOPS,
    Pick,
    RouteBuilder,
    build_request,
    build_route,
    cheapest_insertion,
    order_path,
    parse_choice,
)

# About 111 m per 0.001 degree of latitude; longitude is fixed so places lie on a north-south line.
LON = 21.0


def place(n: int, lat: float, category: str = "tourism=museum", lon: float = LON) -> Place:
    return Place(osm_id=f"node/{n}", name=f"P{n}", type="node", category=category, lat=lat, lon=lon)


def ids(route: Any) -> list[str]:
    return [stop.osm_id for stop in route.stops]


def builder(reply: str) -> tuple[RouteBuilder, list[list[dict[str, Any]]]]:
    seen: list[list[dict[str, Any]]] = []

    def complete(messages: list[dict[str, Any]]) -> str:
        seen.append(messages)
        return reply

    return RouteBuilder(lambda: complete), seen


def test_order_path_visits_a_line_end_to_end() -> None:
    picks = [Pick(place(n, lat)) for n, lat in [(1, 52.20), (2, 52.23), (3, 52.21), (4, 52.22)]]
    ordered = [p.place.osm_id for p in order_path(picks)]
    assert ordered in (
        ["node/1", "node/3", "node/4", "node/2"],
        ["node/2", "node/4", "node/3", "node/1"],
    )


def test_cheapest_insertion_prefers_the_gap_it_sits_in() -> None:
    path = [Pick(place(1, 52.20)), Pick(place(2, 52.22))]
    detour, position = cheapest_insertion(path, Pick(place(3, 52.21)))
    assert position == 1 and detour < 1


def test_parse_choice_drops_unknown_and_repeated_ids() -> None:
    places = [place(n, 52.2 + n / 1000) for n in range(1, 9)]
    reply = (
        "Sure!\n```json\n"
        + json.dumps(
            {
                "must": [{"id": f"p{n}", "why": "x"} for n in (1, 2, 3, 4, 5, 6)] + [{"id": "p99"}],
                "maybe": [{"id": "p1", "score": 3}, {"id": "p7", "score": "high"}, {"id": "p8"}],
            }
        )
        + "\n```"
    )
    must, maybe = parse_choice(reply, places)
    assert [p.place.osm_id for p in must] == [f"node/{n}" for n in range(1, 6)]
    # The 6th must-visit becomes a maybe; a bad score falls back to the default.
    assert [(p.place.osm_id, p.score) for p in maybe] == [
        ("node/6", 3.0),
        ("node/7", 1.0),
        ("node/8", 1.0),
    ]


def test_parse_choice_rejects_non_json() -> None:
    with pytest.raises(ValueError):
        parse_choice("I cannot help with that.", [place(1, 52.2)])


def test_places_on_the_way_are_added_and_far_ones_are_not() -> None:
    must = [Pick(place(1, 52.200), why="castle"), Pick(place(2, 52.220), why="palace")]
    on_way = [Pick(place(n, 52.200 + n / 1000, f"kind={n}"), score=2) for n in range(3, 8)]
    far = Pick(place(99, 52.300, "kind=far"), score=3)  # ~9 km away
    route = build_route(must, [far, *on_way])
    assert "node/99" not in ids(route)
    assert set(ids(route)) == {"node/1", "node/2", *(f"node/{n}" for n in range(3, 8))}
    roles = {stop.osm_id: stop.role for stop in route.stops}
    assert roles["node/1"] == "must" and roles["node/3"] == "on_the_way"
    assert route.stops[0].osm_id in ("node/1", "node/2")  # stops in order along the line
    assert 2.1 < route.distance_km < 2.3
    # One step per change: the anchors, then each added place with its detour.
    assert route.steps[0].action == "anchors" and set(route.steps[0].order) == {"node/1", "node/2"}
    added = [step for step in route.steps if step.action == "add"]
    assert len(added) == 5 and all(step.detour_m is not None for step in added)
    assert route.steps[-1].order == ids(route)


def test_short_routes_take_a_detour_to_reach_the_minimum() -> None:
    must = [Pick(place(1, 52.200)), Pick(place(2, 52.201))]
    far = [Pick(place(n, 52.25 + n / 100, f"kind={n}")) for n in range(3, 6)]
    assert len(build_route(must, far).stops) == 5


def test_repeated_categories_are_penalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.route_planner.MAX_STOPS", 3)  # room for one more stop
    must = [Pick(place(1, 52.200, "amenity=cafe")), Pick(place(2, 52.210, "amenity=cafe"))]
    cafe = Pick(place(3, 52.205, "amenity=cafe"), score=2)  # right on the path
    park = Pick(place(4, 52.205, "leisure=park", lon=LON + 0.001), score=2)  # ~70 m off it
    assert set(ids(build_route(must, [cafe, park]))) == {"node/1", "node/2", "node/4"}


def test_route_is_capped() -> None:
    picks = [Pick(place(n, 52.2 + n / 10000, f"kind={n}")) for n in range(1, 30)]
    assert len(build_route(picks[:3], picks[3:]).stops) == MAX_STOPS


def test_builder_uses_the_llm_choice() -> None:
    places = [place(n, 52.2 + n / 1000) for n in range(1, 6)]
    reply = json.dumps(
        {"must": [{"id": "p2", "why": "Loves it"}, {"id": "p4", "why": "Iconic"}], "maybe": []}
    )
    plan, seen = builder(reply)
    [route] = plan("Castles please", places)
    # Only two must-visits and no maybes: the route can't reach the minimum and stays short.
    assert ids(route) in (["node/2", "node/4"], ["node/4", "node/2"])
    assert {stop.why for stop in route.stops} == {"Loves it", "Iconic"}
    assert "Castles please" in seen[0][1]["content"]


def test_builder_falls_back_when_the_llm_fails() -> None:
    places = [place(n, 52.2 + n / 1000, f"kind={n}") for n in range(1, 8)]
    plan, _ = builder("not json")
    [route] = plan("anything", places)
    assert len(route.stops) == 7
    assert all(stop.why is None for stop in route.stops)


def test_builder_skips_places_without_coordinates() -> None:
    nowhere = Place(osm_id="node/9", name="X", type="node", category="other")
    plan, _ = builder("not json")
    assert plan("x", [nowhere]) == []
    [route] = plan("x", [nowhere, place(1, 52.2)])
    assert ids(route) == ["node/1"]


def test_request_lists_places_with_ids_names_and_descriptions() -> None:
    museum = place(1, 52.2).model_copy(
        update={
            "tags": {"name:en": "Big Museum"},
            "wikipedia": {"en": WikipediaSnippet(title="t", url="u", extract="A  big\nmuseum.")},
        }
    )
    text = build_request("Art", [museum, place(2, 52.21)])
    assert text.startswith("Traveller's wishes: Art")
    assert (
        "p1 | Big Museum (P1) | tourism=museum | fame 0 | 0.6 km from centre | A big museum."
        in text
    )
    assert "p2 | P2 |" in text
