"""The `search_places` tool: Betty's single tool, and her signal that she has enough info.

Betty passes only the confirmed minimum (destination + timing). The server then summarises the
whole conversation and runs find_places on that summary (see BettyService.chat).
"""

import json
from typing import Any

from pydantic import ValidationError

from app.schemas import Trip
from app.services.place_finder import FoundPlaces

TOOL_NAME = "search_places"
TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Search OpenStreetMap for real sights and attractions that fit this trip (it does not "
            "return restaurants, cafes or bars). Call it only when the "
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


def search_result(summary: str, found: FoundPlaces) -> str:
    """Tool result after a search attempt, in one envelope Betty can act on."""
    if found.status != "ok":
        return json.dumps({"status": found.status, "message": found.message, "summary": summary})
    return json.dumps(
        {
            "status": "ok",
            "summary": summary,
            "total": found.total,
            "shown": len(found.places),
            "places": [p.model_dump(exclude_none=True) for p in found.places],
        },
        ensure_ascii=False,
    )
