"""The `finalize_trip_context` tool: Betty's signal that enough trip info was gathered."""

import json

from pydantic import ValidationError

from app.schemas import TripContext

TOOL_NAME = "finalize_trip_context"
TOOL_SPEC: dict[str, object] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Call ONLY when the destination is narrowed to one city or district AND the timing "
            "is known (dates, or a number of days). Signals that enough information was "
            "gathered to plan the trip. Never guess a place or date. Call it again if the "
            "traveller later changes important details."
        ),
        "parameters": TripContext.model_json_schema(),
    },
}


def parse_trip_context(arguments: str) -> tuple[TripContext | None, str]:
    """Validate tool arguments; return the context (or None) and the text to feed back."""
    try:
        context = TripContext.model_validate_json(arguments)
    except ValidationError as exc:
        problems = [str(e["msg"]).removeprefix("Value error, ") for e in exc.errors()]
        return None, json.dumps({"status": "rejected", "problems": problems})
    return context, json.dumps(
        {"status": "ok", "message": "Saved. Confirm what you captured in one friendly sentence."}
    )
