"""CLI smoke test: `python -m osm "<text or path>"` builds a query from plain text and runs it."""

import sys
from pathlib import Path

from osm.llm import FallbackClient, make_clients
from osm.pipeline import NeedsMoreInfo, generate_and_run
from osm.runner import OverpassUnavailable
from osm.wikipedia import enrich


def main(arg: str, with_wikipedia: bool = True) -> None:
    try:
        request = Path(arg).read_text() if Path(arg).is_file() else arg
    except OSError:  # e.g. a long text that is not a valid file name
        request = arg
    try:
        result = generate_and_run(request, FallbackClient(make_clients()).complete)
    except NeedsMoreInfo as exc:
        sys.exit(f"Need more information: {exc}")
    except OverpassUnavailable as exc:
        sys.exit(f"Overpass server problem (not an empty result): {exc}")
    if with_wikipedia:
        result.results = enrich(result.results)
    print(f"-- attempts: {result.attempts}\n{result.query}\n")
    print(result.model_dump_json(include={"results"}, indent=2))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--no-wikipedia"]
    if len(args) != 1:
        sys.exit('usage: python -m osm [--no-wikipedia] "<text>" | <file.txt>')
    main(args[0], with_wikipedia="--no-wikipedia" not in sys.argv[1:])
