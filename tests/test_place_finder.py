"""find_places (PlaceFinder): plain text in, typed places out (no network)."""

import random
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.place_finder import (
    MAX_PER_KIND,
    MAX_REQUEST_CHARS,
    MAX_RESULTS,
    SAMPLE_POOL,
    FoundPlaces,
    PlaceFinder,
    cap_per_kind,
    drop_duplicates,
    is_food,
    place_image,
    place_images,
    select_places,
)
from osm.pipeline import NeedsMoreInfo, QueryGenerationFailed
from osm.schemas import OverpassElement, WikipediaInfo, WikipediaText
from osm.wikipedia import ImageCredit

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


def famous(id: int, editions: int, kind: str = "museum") -> OverpassElement:
    return element(id, tourism=kind, wikidata=f"Q{editions}")


@pytest.fixture
def fame_from_qid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fame = the number in the element's wikidata tag (Q43 -> 43), so tests need no file."""
    monkeypatch.setattr(
        "app.services.place_finder.fame",
        lambda e: int(e.tags["wikidata"][1:]) if "wikidata" in e.tags else 0,
    )


@pytest.mark.usefixtures("fame_from_qid")
def test_temperature_zero_takes_the_best_known_in_order() -> None:
    elements = [
        famous(i, editions, kind)
        for i, (editions, kind) in enumerate(
            [(3, "museum"), (43, "attraction"), (0, "gallery"), (51, "viewpoint"), (12, "zoo")]
        )
    ]
    assert [e.id for e in select_places(elements, 3, 0)] == [3, 1, 4]


@pytest.mark.usefixtures("fame_from_qid")
def test_wikipedia_tag_beats_unknown_when_nothing_has_fame() -> None:
    """Outside Warsaw there are no fame counts: places with a Wikipedia tag still come first."""
    plain = [element(i, historic="castle") for i in range(5)]
    notable = element(9, historic="castle", wikipedia="pl:Zamek")
    assert select_places([*plain, notable], 1, 0) == [notable]


@pytest.mark.usefixtures("fame_from_qid")
def test_one_kind_cannot_fill_every_slot_while_others_are_left() -> None:
    museums = [famous(i, 40 - i) for i in range(10)]
    parks = [famous(100 + i, 5, "viewpoint") for i in range(3)]
    picked = select_places(museums + parks, 6, 0)
    assert MAX_PER_KIND == 4
    assert [e.id for e in picked] == [0, 1, 2, 3, 100, 101]


def test_cap_fills_remaining_slots_when_only_one_kind_is_left() -> None:
    museums = [element(i, tourism="museum") for i in range(10)]
    assert cap_per_kind(museums, 6) == museums[:6]


@pytest.mark.usefixtures("fame_from_qid")
def test_sampling_is_reproducible_with_the_same_seed_and_varies_otherwise() -> None:
    elements = [famous(i, i % 50, f"k{i % 20}") for i in range(300)]
    first = select_places(elements, 30, 0.5, random.Random(1))
    assert first == select_places(elements, 30, 0.5, random.Random(1))
    assert first != select_places(elements, 30, 0.5, random.Random(2))


@pytest.mark.usefixtures("fame_from_qid")
def test_sampling_only_draws_from_the_best_known_pool() -> None:
    known = [famous(i, 1000 + i, f"k{i % 40}") for i in range(SAMPLE_POOL)]
    unknown = [element(1000 + i, tourism="artwork") for i in range(2000)]
    for seed in range(20):
        picked = select_places(known + unknown, 30, 1.0, random.Random(seed))
        assert len(picked) == 30 and all("wikidata" in e.tags for e in picked)


@pytest.mark.usefixtures("fame_from_qid")
def test_low_temperature_keeps_more_of_the_top_than_high_temperature() -> None:
    elements = [famous(i, 1 + i, f"k{i % 40}") for i in range(SAMPLE_POOL)]
    top = {e.id for e in select_places(elements, 30, 0)}

    def kept(temperature: float) -> float:
        runs = [select_places(elements, 30, temperature, random.Random(s)) for s in range(30)]
        return sum(len(top & {e.id for e in run}) for run in runs) / len(runs)

    assert 30 > kept(0.1) > kept(0.5) > kept(1.0)


@pytest.mark.usefixtures("fame_from_qid")
def test_small_results_ignore_temperature() -> None:
    elements = [famous(i, i) for i in range(3)]
    assert [e.id for e in select_places(elements, 30, 1.0)] == [2, 1, 0]


@pytest.mark.usefixtures("fame_from_qid")
def test_finder_samples_with_a_seed_from_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [famous(i, i % 60, f"k{i % 25}") for i in range(400)]
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=1, results=many),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)

    def ids(found: FoundPlaces) -> list[str]:
        return [p.osm_id for p in found.places]

    first = finder()(REQUEST, temperature=0.5)
    assert ids(first) == ids(finder()(REQUEST, temperature=0.5))
    assert ids(first) != ids(finder()(REQUEST + " They love art.", temperature=0.5))
    assert first.places[0].fame == int(first.places[0].tags["wikidata"][1:])


def test_large_results_are_cropped_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    many = [element(i, historic="castle") for i in range(MAX_RESULTS + 30)]
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(query="Q", attempts=1, results=many),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    caplog.set_level("INFO")
    found = finder()(REQUEST)
    assert found.total == MAX_RESULTS + 30
    assert len(found.places) == MAX_RESULTS == 30
    assert f"returned {MAX_RESULTS + 30} places; sampling {MAX_RESULTS}" in caplog.text


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


def with_wikipedia(**images: str | None) -> OverpassElement:
    texts = {
        lang: WikipediaText(title="T", url="u", extract="e", image=url, image_file="F x.jpg")
        for lang, url in images.items()
    }
    return element(1, wikimedia_commons="File:Other.jpg").model_copy(
        update={"wikipedia": WikipediaInfo(**texts)}
    )


def test_image_prefers_wikipedia_polish_then_english() -> None:
    image = place_image(with_wikipedia(pl="https://pl/img.jpg", en="https://en/img.jpg"))
    assert image is not None
    assert (image.url, image.source) == ("https://pl/img.jpg", "wikipedia")
    assert image.page_url == "https://pl.wikipedia.org/wiki/File:F%20x.jpg"
    english = place_image(with_wikipedia(pl=None, en="https://en/img.jpg"))
    assert english is not None and english.url == "https://en/img.jpg"


def test_image_falls_back_to_osm_commons_file() -> None:
    image = place_image(with_wikipedia(pl=None))
    assert image is not None and image.source == "osm"
    assert image.url == ("https://commons.wikimedia.org/wiki/Special:FilePath/Other.jpg?width=800")
    assert image.page_url == "https://commons.wikimedia.org/wiki/File:Other.jpg"


def test_image_from_the_osm_image_tag() -> None:
    commons = place_image(element(1, image="File:Zamek Królewski.jpg"))
    assert commons is not None and commons.url.startswith(
        "https://commons.wikimedia.org/wiki/Special:FilePath/Zamek_Kr%C3%B3lewski.jpg"
    )
    direct = place_image(element(1, image="https://example.org/photo.JPG?x=1"))
    assert direct is not None and (direct.url, direct.page_url) == (
        "https://example.org/photo.JPG?x=1",
        None,
    )


def test_no_image_when_neither_source_has_one() -> None:
    assert place_image(element(1)) is None
    assert place_image(element(1, wikimedia_commons="Category:Royal Castle")) is None
    assert place_image(element(1, image="https://example.org/gallery")) is None  # a web page
    assert place_image(element(1, image="ftp://example.org/a.jpg")) is None


def test_found_places_carry_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete, **_: SimpleNamespace(
            query="Q", attempts=1, results=[element(1, image="File:A.jpg"), element(2)]
        ),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda elements: elements)
    credit = ImageCredit(author="Ala", license="CC BY-SA 3.0", license_url="https://cc/by-sa")
    monkeypatch.setattr(
        "app.services.place_finder.fetch_image_credits",
        lambda files: {file: credit for file in files},
    )
    first, second = finder()(REQUEST).places
    assert first.image is not None and first.image.source == "osm"
    assert (first.image.author, first.image.license) == ("Ala", "CC BY-SA 3.0")
    assert second.image is None and second.model_dump()["image"] is None


def test_credits_are_looked_up_once_for_wiki_files_only() -> None:
    elements = [
        with_wikipedia(pl="https://pl/img.jpg"),
        element(2, image="File:B c.jpg"),
        element(3, image="https://example.org/d.png"),
        element(4),
    ]
    calls: list[list[tuple[str, str]]] = []

    def fetch(files: list[tuple[str, str]]) -> dict[tuple[str, str], ImageCredit]:
        calls.append(files)
        return {("pl.wikipedia.org", "F x.jpg"): ImageCredit("Ala", "CC BY 4.0", "https://cc")}

    wiki, commons, url, none = place_images(elements, fetch)
    assert calls == [[("pl.wikipedia.org", "F x.jpg"), ("commons.wikimedia.org", "B_c.jpg")]]
    assert wiki is not None and (wiki.author, wiki.license_url) == ("Ala", "https://cc")
    assert commons is not None and commons.author is None  # not found: page_url still credits
    assert url is not None and url.author is None and none is None
