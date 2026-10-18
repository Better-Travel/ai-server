"""Fame (Wikipedia language editions) of OSM places, from the precomputed Warsaw file."""

import json
from pathlib import Path

import httpx

from osm.fame import FAME_FILE, city_query, fame, fetch_fame, load_fame, wikidata_id
from osm.schemas import OverpassElement

ROYAL_CASTLE = OverpassElement(
    id=1, type="way", lat=52.2479, lon=21.0152, name="Zamek Królewski", tags={"wikidata": "Q1"}
)
TABLE = {"Q1": (43, 52.2481, 21.0149), "Q2": (84, 40.0, -75.0)}


def test_wikidata_id_takes_the_first_valid_id() -> None:
    def with_tag(value: str) -> OverpassElement:
        return OverpassElement(id=1, type="node", tags={"wikidata": value})

    assert wikidata_id(with_tag(" Q7;Q8")) == "Q7"
    assert wikidata_id(with_tag("none")) is None
    assert wikidata_id(OverpassElement(id=1, type="node")) is None


def test_fame_is_used_only_when_the_item_is_near_the_place() -> None:
    assert fame(ROYAL_CASTLE, TABLE) == 43
    # A Quaker meeting house tagged with the Quaker movement (its item is in Pennsylvania).
    wrong_item = ROYAL_CASTLE.model_copy(update={"tags": {"wikidata": "Q2"}})
    assert fame(wrong_item, TABLE) == 0
    assert fame(ROYAL_CASTLE.model_copy(update={"lat": None}), TABLE) == 0
    assert fame(ROYAL_CASTLE.model_copy(update={"tags": {}}), TABLE) == 0


def test_missing_file_means_no_fame(tmp_path: Path) -> None:
    assert load_fame(tmp_path / "nope.json") == {}


def test_shipped_warsaw_file_knows_the_royal_castle() -> None:
    table = load_fame(FAME_FILE)
    assert len(table) > 500
    assert fame(ROYAL_CASTLE.model_copy(update={"tags": {"wikidata": "Q756098"}}), table) > 30


def test_city_query_asks_only_for_places_with_wikidata() -> None:
    query = city_query()
    assert 'area["name"="Warszawa"](area.c1)->.a1;' in query
    assert '  nwr["wikidata"]["tourism"~"^(' in query
    assert "restaurant" not in query


def test_fetch_fame_reads_counts_and_coordinates() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        rows = [
            {
                "item": {"value": "http://www.wikidata.org/entity/Q1"},
                "n": {"value": "43"},
                "coord": {"value": "Point(21.0149 52.2481)"},
            }
        ]
        return httpx.Response(200, text=json.dumps({"results": {"bindings": rows}}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch_fame(["Q1", "Q2"], client) == {"Q1": (43, 52.2481, 21.0149)}
    assert len(seen) == 1 and "wd%3AQ1+wd%3AQ2" in seen[0]
