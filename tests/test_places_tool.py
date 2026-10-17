"""Betty's find_places tool, and the tool-calling loop in BettyService (no network)."""

import json
from types import SimpleNamespace
from typing import Any

from app.config import Settings
from app.schemas import Message
from app.services.betty_service import BettyService
from app.services.places_tool import TOOL_NAME, PlacesTool
from osm.schemas import OverpassElement

ARGS = json.dumps(
    {
        "places": [
            {
                "category": "food_drink",
                "filters": [{"key": "amenity", "values": ["cafe"]}],
                "area": "Warszawa",
                "country": "PL",
            }
        ]
    }
)


def test_tool_rejects_invalid_arguments() -> None:
    out = json.loads(PlacesTool(lambda: lambda m: "q")('{"places": []}'))
    assert "Invalid arguments" in out["error"]


def test_tool_returns_compact_results(monkeypatch: Any) -> None:
    element = OverpassElement(
        id=1, type="node", lat=52.2, lon=21.0, name="Cafe A", tags={"amenity": "cafe", "email": "x"}
    )
    monkeypatch.setattr(
        "app.services.places_tool.generate_and_run",
        lambda places, complete: SimpleNamespace(results=[element]),
    )
    out = json.loads(PlacesTool(lambda: lambda m: "q")(ARGS))
    assert out["total"] == 1
    assert out["places"][0]["name"] == "Cafe A"
    assert out["places"][0]["tags"] == {"amenity": "cafe"}  # unrelated tags are dropped


def test_tool_failures_are_returned_as_text(monkeypatch: Any) -> None:
    def boom(places: Any, complete: Any) -> Any:
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.services.places_tool.generate_and_run", boom)
    assert "unavailable" in json.loads(PlacesTool(lambda: lambda m: "q")(ARGS))["error"]


class ToolCallingCompletions:
    """First reply asks for the tool, second reply is the final answer."""

    def __init__(self, call_tool: bool = True) -> None:
        self.call_tool = call_tool
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.call_tool and len(self.calls) == 1:
            call = SimpleNamespace(
                id="c1", function=SimpleNamespace(name=TOOL_NAME, arguments=ARGS)
            )
            message = SimpleNamespace(content=None, tool_calls=[call])
        else:
            message = SimpleNamespace(content="Try Cafe A!", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_betty_runs_tool_then_answers() -> None:
    fake = ToolCallingCompletions()
    seen_args: list[str] = []

    def tool(arguments: str) -> str:
        seen_args.append(arguments)
        return '{"places": []}'

    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    settings = Settings(nvidia_api_key_2="x", _env_file=None)
    betty = BettyService(settings, client=client, places_tool=tool)

    reply = betty.chat([Message(role="user", content="cafes in Warsaw?")])

    assert reply.reply == "Try Cafe A!"
    assert reply.trip_context is None
    assert seen_args == [ARGS]
    assert [t["function"]["name"] for t in fake.calls[0]["tools"]] == [
        TOOL_NAME,
        "finalize_trip_context",
    ]
    roles = [m["role"] for m in fake.calls[1]["messages"]]
    assert roles[-2:] == ["assistant", "tool"]


def test_tool_can_be_disabled() -> None:
    fake = ToolCallingCompletions(call_tool=False)
    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    settings = Settings(nvidia_api_key_2="x", betty_places_tool=False, _env_file=None)
    BettyService(settings, client=client).summarize([Message(role="user", content="hi")])
    assert "tools" not in fake.calls[0]
