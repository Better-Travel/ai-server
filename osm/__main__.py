"""CLI smoke test: `python -m osm places.json` generates and runs a query."""

import sys
from pathlib import Path

from osm.llm import FallbackClient, make_clients
from osm.pipeline import generate_and_run
from osm.runner import OverpassUnavailable
from osm.schemas import PlacesInput
from osm.wikipedia import enrich


def main(path: str) -> None:
    places = PlacesInput.model_validate_json(Path(path).read_text())
    try:
        result = generate_and_run(places, FallbackClient(make_clients()).complete)
    except OverpassUnavailable as exc:
        sys.exit(f"Overpass server problem (not an empty result): {exc}")
    result.results = enrich(result.results)
    print(f"-- attempts: {result.attempts}\n{result.query}\n")
    print(result.model_dump_json(include={"results"}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m osm places.json")
    main(sys.argv[1])
