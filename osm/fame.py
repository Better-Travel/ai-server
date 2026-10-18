"""How well known a place is: the number of Wikipedia language editions of its Wikidata item.

Overpass returns thousands of places for a city (2,826 sights for "the most popular places in
Warsaw"), and OSM tags alone cannot tell the Royal Castle from a roadside memorial. The number of
Wikipedia editions can (Royal Castle 43, Palace of Culture 51, most memorials 0-2).

The counts are precomputed for Warsaw into `data/warsaw_fame.json` (`python -m osm.fame`), since
the Wikidata services are slow (about 10 s per query) and rate-limit bursts. Places elsewhere, or
without a `wikidata` tag, have fame 0.

Some OSM objects point at the wrong item, e.g. a Quaker meeting house tagged with the Quaker
movement (84 editions) or a copy of a statue tagged with the original. Those items have no
coordinates, or coordinates far away, so a count is only used when the item's coordinates lie
within MAX_DISTANCE_M of the OSM object.
"""

import json
import math
import sys
from functools import lru_cache
from pathlib import Path

import httpx

from .runner import run_query
from .schemas import SIGHT_TAGS, OverpassElement

FAME_FILE = Path(__file__).parent / "data" / "warsaw_fame.json"
MAX_DISTANCE_M = 2000.0
SPARQL_URL = "https://query.wikidata.org/sparql"
SPARQL_BATCH = 400
HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "ai-server-osm-query-builder/0.1 (fame precompute)",
}
_EARTH_RADIUS_M = 6_371_000.0


def wikidata_id(element: OverpassElement) -> str | None:
    """The element's first Wikidata id (`Q...`), if it has one."""
    value = element.tags.get("wikidata", "").split(";")[0].strip()
    return value if value.startswith("Q") else None


@lru_cache
def load_fame(path: Path = FAME_FILE) -> dict[str, tuple[int, float, float]]:
    """Wikidata id -> (language editions, lat, lon); empty if the file is missing."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {qid: (int(n), float(lat), float(lon)) for qid, (n, lat, lon) in raw.items()}


def fame(element: OverpassElement, table: dict[str, tuple[int, float, float]] | None = None) -> int:
    """Wikipedia language editions of the element's Wikidata item; 0 if unknown or implausible."""
    qid = wikidata_id(element)
    entry = (load_fame() if table is None else table).get(qid or "")
    if entry is None or element.lat is None or element.lon is None:
        return 0
    editions, lat, lon = entry
    dx = math.radians(lon - element.lon) * math.cos(math.radians((lat + element.lat) / 2))
    dy = math.radians(lat - element.lat)
    return editions if _EARTH_RADIUS_M * math.hypot(dx, dy) <= MAX_DISTANCE_M else 0


def city_query(city: str = "Warszawa", country: str = "PL") -> str:
    """Every catalogued sight in the city that has a Wikidata tag."""
    statements = "\n".join(
        f'  nwr["wikidata"]["{key}"~"^({"|".join(sorted(values))})$"](area.a1);'
        for key, values in SIGHT_TAGS.items()
    )
    return (
        "[out:json][timeout:120];\n"
        f'area["ISO3166-1"="{country}"][admin_level=2]->.c1;\n'
        f'area["name"="{city}"](area.c1)->.a1;\n'
        f"(\n{statements}\n);\nout center tags;"
    )


def fetch_fame(ids: list[str], client: httpx.Client) -> dict[str, tuple[int, float, float]]:
    """Language editions and coordinates of Wikidata items; items without coordinates are left
    out (their counts would never pass the distance check)."""
    found: dict[str, tuple[int, float, float]] = {}
    for start in range(0, len(ids), SPARQL_BATCH):
        values = " ".join(f"wd:{qid}" for qid in ids[start : start + SPARQL_BATCH])
        query = (
            f"SELECT ?item ?n (SAMPLE(?c) AS ?coord) WHERE {{ VALUES ?item {{ {values} }} "
            "?item wikibase:sitelinks ?n . ?item wdt:P625 ?c } GROUP BY ?item ?n"
        )
        response = client.post(SPARQL_URL, data={"query": query}, headers=HEADERS)
        response.raise_for_status()
        for row in response.json()["results"]["bindings"]:
            lon, lat = map(float, row["coord"]["value"].removeprefix("Point(")[:-1].split())
            found[row["item"]["value"].rsplit("/", 1)[1]] = (int(row["n"]["value"]), lat, lon)
    return found


def build(path: Path = FAME_FILE, city: str = "Warszawa", country: str = "PL") -> int:
    """Rebuild the fame file for one city; returns the number of items written."""
    elements = run_query(city_query(city, country))
    ids = sorted({qid for e in elements if (qid := wikidata_id(e))})
    with httpx.Client(timeout=120) as client:
        found = fetch_fame(ids, client)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = {qid: [n, round(lat, 6), round(lon, 6)] for qid, (n, lat, lon) in sorted(found.items())}
    path.write_text(json.dumps(rows, separators=(",", ":")) + "\n")
    return len(rows)


if __name__ == "__main__":
    print(f"wrote {build()} Wikidata items to {FAME_FILE}", file=sys.stderr)
