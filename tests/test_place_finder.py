"""find_places (PlaceFinder): plain text in, typed places out (no network)."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.place_finder import (
    MAX_REQUEST_CHARS,
    MAX_RESULTS,
    PlaceFinder,
    drop_duplicates,
    is_food,
    select_diverse,
    select_notable_first,
)
from osm.pipeline import NeedsMoreInfo, QueryGenerationFailed
from osm.schemas import OverpassElement, WikipediaInfo, WikipediaText

REQUEST = "Destination: Warsaw, country PL. The traveller wants sights and cafes."


def finder() -> PlaceFinder:
    return PlaceFinder(lambda: lambda m: "q")


def test_empty_request_is_an_error() -> None:
    found = finder()("   ")
    assert found.status == "error"
    assert found.message == "The search request is empty."


def test_passes_plain_text_to_the_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(request: str, complete: Any, **_: Any) -> Any:
        seen.append(request)
        return SimpleNamespace(query="Q", attempts=1, results=[])

    monkeypatch.setattr("app.services.place_finder.generate_and_run", fake)
    found = finder()(REQUEST + "x" * 3000)
    assert seen == [(REQUEST + "x" * 3000)[:MAX_REQUEST_CHARS]]
    assert found.status == "ok" and found.total == 0 and found.places == []


def test_reports_needs_more_info(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(request: str, complete: Any, **_: Any) -> Any:
        raise NeedsMoreInfo("the country is missing")

    monkeypatch.setattr("app.services.place_finder.generate_and_run", fake)
    found = finder()(REQUEST)
    assert found.status == "needs_info"
    assert found.message == "the country is missing"


def test_returns_compact_places(monkeypatch: pytest.MonkeyPatch) -> None:
    element = OverpassElement(
        id=1,
        type="node",
        lat=52.2,
        lon=21.0,
        name="Museum A",
        tags={"tourism": "museum", "email": "x", "name:en": "Museum A", "wheelchair": "yes"},
        wikipedia=WikipediaInfo(en=WikipediaText(title="Museum A", url="u", extract="word " * 100)),
    )
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=1, results=[element]),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    found = finder()(REQUEST)
    assert found.status == "ok" and found.total == 1
    assert (found.query, found.attempts, found.fetched) == ("Q", 1, 1)
    place = found.places[0]
    assert place.name == "Museum A"
    assert place.osm_id == "node/1" and place.category == "tourism=museum"
    # unrelated tags are dropped
    assert place.tags == {"tourism": "museum", "name:en": "Museum A", "wheelchair": "yes"}
    assert place.wikipedia is not None and set(place.wikipedia) == {"en"}
    assert place.wikipedia["en"].extract.endswith("…")  # long extracts are trimmed


def test_failures_become_an_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: Any, complete: Any, **_: Any) -> Any:
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.services.place_finder.generate_and_run", boom)
    found = finder()(REQUEST)
    assert found.status == "error"
    assert found.message is not None and "unavailable" in found.message


def element(id: int, **tags: str) -> OverpassElement:
    return OverpassElement(id=id, type="node", name=f"P{id}", tags=tags)


def test_place_json_has_id_category_and_app_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = {
        "name": "Muzeum",
        "tourism": "museum",
        "addr:street": "Plac Zamkowy",
        "addr:housenumber": "4",
        "contact:website": "https://example.org",
        "fee": "yes",
        "wikidata": "Q1",
        "source": "survey",
    }
    museum = OverpassElement(id=7, type="way", name="Muzeum", tags=tags)
    unknown = OverpassElement(id=8, type="node", name="Thing")
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(
            query="Q", attempts=1, results=[museum, unknown]
        ),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    place, other = (p.model_dump() for p in finder()(REQUEST).places)
    assert place["osm_id"] == "way/7"
    assert place["category"] == "tourism=museum"
    assert set(place["tags"]) == {
        "tourism",
        "addr:street",
        "addr:housenumber",
        "contact:website",
        "fee",
        "wikidata",
    }
    assert other["osm_id"] == "node/8" and other["category"] == "other"


# The Royal Castle in Warsaw: mapped as a building and as a museum point inside it (~20 m apart).
CASTLE = OverpassElement(
    id=1,
    type="way",
    lat=52.2479,
    lon=21.0152,
    name="Zamek Królewski",
    tags={"name": "Zamek Królewski", "historic": "castle", "building": "yes", "wikidata": "Q1"},
)
CASTLE_MUSEUM = OverpassElement(
    id=2,
    type="node",
    lat=52.2480,
    lon=21.0155,
    name="Zamek Królewski",
    tags={"name": "Zamek Królewski", "tourism": "museum"},
)


def test_same_name_nearby_is_one_place_keeping_more_tags() -> None:
    assert drop_duplicates([CASTLE_MUSEUM, CASTLE]) == [CASTLE]


def test_shared_wikidata_is_one_place_even_far_apart() -> None:
    elsewhere = OverpassElement(
        id=3, type="node", lat=52.0, lon=21.0, name="Royal Castle", tags={"wikidata": "Q1"}
    )
    assert drop_duplicates([elsewhere, CASTLE]) == [CASTLE]


def test_same_name_far_apart_or_without_coordinates_is_kept() -> None:
    far = CASTLE_MUSEUM.model_copy(update={"id": 3, "lat": 52.2530})  # ~570 m north
    unplaced = CASTLE_MUSEUM.model_copy(update={"id": 4, "lat": None, "lon": None})
    other = element(5, amenity="cafe")
    elements = [CASTLE, far, unplaced, other]
    assert drop_duplicates(elements) == elements


def test_duplicates_are_dropped_before_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(
            query="Q", attempts=1, results=[CASTLE_MUSEUM, CASTLE]
        ),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    found = finder()(REQUEST)
    assert found.total == 1
    assert [p.osm_id for p in found.places] == ["way/1"]


def test_selection_takes_turns_between_kinds() -> None:
    """Live search returned 947 places and the first 20 were all cafes."""
    cafes = [element(i, amenity="cafe") for i in range(30)]
    castles = [element(100 + i, historic="castle") for i in range(2)]
    palaces = [element(200 + i, historic="palace") for i in range(2)]
    picked = select_diverse(cafes + castles + palaces, 6)
    kinds = [e.tags.get("historic") or e.tags["amenity"] for e in picked]
    assert sorted(kinds) == ["cafe", "cafe", "castle", "castle", "palace", "palace"]


def test_selection_prefers_places_with_wikipedia() -> None:
    plain = [element(i, amenity="cafe") for i in range(5)]
    notable = element(99, amenity="cafe", wikipedia="pl:Cafe")
    assert select_diverse([*plain, notable], 1) == [notable]


def test_selection_keeps_everything_when_under_the_limit() -> None:
    elements = [element(1, amenity="cafe"), element(2, tourism="museum")]
    assert select_diverse(elements, 20) == elements


def test_large_results_are_cropped_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    many = [element(i, historic="castle") for i in range(MAX_RESULTS + 30)]
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=1, results=many),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    caplog.set_level("WARNING")
    found = finder()(REQUEST)
    assert found.total == MAX_RESULTS + 30
    assert len(found.places) == MAX_RESULTS == 30
    assert f"returned {MAX_RESULTS + 30} places; cropping to the {MAX_RESULTS} best" in caplog.text


def test_small_results_are_not_cropped_or_warned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    few = [element(i, historic="castle") for i in range(3)]
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=1, results=few),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    caplog.set_level("WARNING")
    assert len(finder()(REQUEST).places) == 3
    assert "cropping" not in caplog.text


def test_fetched_counts_raw_elements_before_duplicates_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    twins = [
        element(1, tourism="museum", wikidata="Q1"),
        element(2, tourism="museum", wikidata="Q1"),
    ]
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=2, results=twins),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    found = finder()(REQUEST)
    assert found.fetched == 2 and found.total == 1 and found.attempts == 2


def test_failed_generation_keeps_the_last_query(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: str, complete: object, **_: object) -> object:
        raise QueryGenerationFailed("[out:json]; broken", "parse error", 4)

    monkeypatch.setattr("app.services.place_finder.generate_and_run", boom)
    found = finder()(REQUEST)
    assert found.status == "error" and found.query == "[out:json]; broken"
    assert found.attempts == 4


def test_food_and_drink_are_excluded_but_sights_with_a_cafe_tag_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        element(1, amenity="cafe"),
        element(2, amenity="restaurant", tourism="hotel"),  # a hotel also mapped as a restaurant
        element(3, amenity="pub", historic="bus"),  # not a catalogued sight
        element(4, historic="castle", amenity="restaurant"),  # a castle housing a restaurant
        element(5, tourism="museum"),
        element(6, amenity="cinema"),  # not food, so not excluded
    ]
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=1, results=results),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    found = finder()(REQUEST)
    assert sorted(p.osm_id for p in found.places) == ["node/4", "node/5", "node/6"]
    assert found.total == 3 and found.fetched == 6


def test_is_food() -> None:
    assert is_food(element(1, amenity="bar"))
    assert not is_food(element(2, amenity="bar", historic="castle"))
    assert not is_food(element(3, tourism="museum"))
    assert not is_food(element(4))


def test_notable_places_come_first_even_when_their_kind_is_common() -> None:
    plain_castles = [element(i, historic="castle") for i in range(10)]
    notable_monuments = [element(100 + i, historic="monument", wikipedia="pl:M") for i in range(3)]
    notable_museum = element(200, tourism="museum", wikidata="Q9")
    picked = select_notable_first([*plain_castles, *notable_monuments, notable_museum], 6)
    assert {e.id for e in picked[:4]} == {100, 101, 102, 200}  # all notable ones before the rest
    assert all(e.tags["historic"] == "castle" for e in picked[4:])
    assert len(picked) == 6


def test_the_pipeline_is_told_to_search_sights_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(request: str, complete: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(query="Q", attempts=1, results=[])

    monkeypatch.setattr("app.services.place_finder.generate_and_run", fake)
    finder()(REQUEST)
    assert "food and drink" in seen["scope"].lower()
    assert "shop" not in seen["allowed_tags"]
    assert not {"restaurant", "cafe", "bar", "pub"} & seen["allowed_tags"]["amenity"]
    assert "museum" in seen["allowed_tags"]["tourism"]
