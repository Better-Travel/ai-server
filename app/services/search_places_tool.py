"""The `search_places` tool: Betty's single tool, and her signal that she has enough info.

Betty passes only the confirmed minimum (destination + timing). The server then summarises the
whole conversation and runs find_places on that summary (see BettyService.chat).
"""

import json
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from app.schemas import EventOccurrence, EventSearch, Route, Trip
from app.services.place_finder import FoundPlaces

TOOL_NAME = "search_places"
TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Search OpenStreetMap for real sights and attractions that fit this trip (it does not "
            "return restaurants, cafes or bars). When the trip has specific dates and is in "
            "Warsaw, the result also lists events on those dates. Call it only when the "
            "destination (one city or district, with its country) and the timing (dates or a "
            "number of days) are known. Pass only these confirmed facts: the server reads the "
            "whole conversation (interests, companions, budget, ...) and builds the search "
            "from it. Returns places to suggest; never mention places that are not in the "
            "result. Call it again if the destination, dates or wishes change."
        ),
        "parameters": Trip.model_json_schema(),
    },
}


def parse_trip(arguments: str) -> Trip | list[str]:
    """Validate the tool arguments; return the trip, or the problems to feed back to Betty."""
    try:
        return Trip.model_validate_json(arguments)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            message = str(error["msg"]).removeprefix("Value error, ")
            field = ".".join(str(part) for part in error["loc"])
            problems.append(f"{field}: {message}" if field else message)
        return problems


def rejected_result(problems: list[str]) -> str:
    """Tool result when the arguments don't meet the minimum; Betty must ask the traveller."""
    return json.dumps({"status": "rejected", "problems": problems})


MAX_EVENT_EXAMPLES = 5


def _when(run: EventOccurrence) -> str:
    if run.start.date() == run.end.date():
        return f"{run.start:%Y-%m-%d %H:%M}-{run.end:%H:%M}"
    return f"{run.start:%Y-%m-%d %H:%M} to {run.end:%Y-%m-%d %H:%M}"


def _events_for_betty(events: EventSearch) -> dict[str, Any]:
    """What Betty sees of the events: the count and a few examples (the client gets them all)."""
    return {
        "total": events.total,
        "shown": len(events.events),
        "examples": [
            {
                "title": event.title,
                "when": _when(event.occurrences[0]),
                "venue": ", ".join(event.venues),
                "category": ", ".join(event.categories),
            }
            for event in events.events[:MAX_EVENT_EXAMPLES]
        ],
    }


def search_result(
    summary: str,
    found: FoundPlaces,
    events: EventSearch | None = None,
    routes: Sequence[Route] = (),
) -> str:
    """Tool result after a search attempt, in one envelope Betty can act on.

    `events` (events found on the trip's dates) is added only if there are some, and `route`
    (the first suggested route) only if one was planned.
    """
    result: dict[str, Any] = {"status": found.status, "summary": summary}
    if found.status != "ok":
        result["message"] = found.message
    else:
        result["total"] = found.total
        result["shown"] = len(found.places)
        result["places"] = [p.model_dump(exclude_none=True) for p in found.places]
    if routes:
        # Betty only needs the order and the reasons; the client gets coordinates in `search`.
        result["route"] = [
            {"name": stop.name, "why": stop.why} if stop.why else {"name": stop.name}
            for stop in routes[0].stops
        ]
    if events and events.events and not events.error:
        result["events"] = _events_for_betty(events)
    return json.dumps(result, ensure_ascii=False)
