"""find_places (PlaceFinder): plain text in, typed places out (no network)."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.place_finder import MAX_REQUEST_CHARS, PlaceFinder, select_diverse
from osm.pipeline import NeedsMoreInfo
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

    def fake(request: str, complete: Any) -> Any:
        seen.append(request)
        return SimpleNamespace(results=[])

    monkeypatch.setattr("app.services.place_finder.generate_and_run", fake)
    found = finder()(REQUEST + "x" * 3000)
    assert seen == [(REQUEST + "x" * 3000)[:MAX_REQUEST_CHARS]]
    assert found.status == "ok" and found.total == 0 and found.places == []


def test_reports_needs_more_info(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(request: str, complete: Any) -> Any:
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
        name="Cafe A",
        tags={"amenity": "cafe", "email": "x"},
        wikipedia=WikipediaInfo(en=WikipediaText(title="Cafe A", url="u", extract="word " * 100)),
    )
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete: SimpleNamespace(results=[element]),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    found = finder()(REQUEST)
    assert found.status == "ok" and found.total == 1
    place = found.places[0]
    assert place.name == "Cafe A"
    assert place.tags == {"amenity": "cafe"}  # unrelated tags are dropped
    assert place.wikipedia is not None and set(place.wikipedia) == {"en"}
    assert place.wikipedia["en"].extract.endswith("…")  # long extracts are trimmed


def test_failures_become_an_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: Any, complete: Any) -> Any:
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.services.place_finder.generate_and_run", boom)
    found = finder()(REQUEST)
    assert found.status == "error"
    assert found.message is not None and "unavailable" in found.message


def element(id: int, **tags: str) -> OverpassElement:
    return OverpassElement(id=id, type="node", name=f"P{id}", tags=tags)


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
