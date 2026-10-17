"""search_places: the minimum-info gate, and chat -> summary -> find_places wiring (no network)."""

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_betty_service
from app.prompts import build_chat_prompt
from app.schemas import Message, Place, Trip
from app.services.betty_service import BettyService
from app.services.place_finder import FoundPlaces
from app.services.search_places_tool import TOOL_NAME, parse_trip

START = date.today() + timedelta(days=200)
END = START + timedelta(days=2)
USER = [Message(role="user", content="Warsaw for 3 days, I love castles and coffee")]
CAFE = Place(
    osm_id="node/1",
    name="Cafe A",
    type="node",
    category="amenity=cafe",
    lat=52.2,
    lon=21.0,
    tags={"amenity": "cafe"},
)


def args(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "city": "Warsaw",
        "country": "PL",
        "scope": "city",
        "start_date": START.isoformat(),
        "end_date": END.isoformat(),
    }
    data.update(overrides)
    return json.dumps({k: v for k, v in data.items() if v is not None})


def trip(**overrides: Any) -> Trip:
    result = parse_trip(args(**overrides))
    assert isinstance(result, Trip), result
    return result


# ---- the minimum-info gate (Trip validation)


def test_valid_trip_is_accepted() -> None:
    assert trip().city == "Warsaw"


def test_duration_without_dates_is_enough() -> None:
    assert trip(start_date=None, end_date=None, duration_days=3).duration_days == 3


@pytest.mark.parametrize("scope", ["region", "country"])
def test_too_broad_destination_is_rejected(scope: str) -> None:
    problems = parse_trip(args(scope=scope))
    assert isinstance(problems, list) and "too broad" in problems[0]


def test_missing_timing_is_rejected() -> None:
    problems = parse_trip(args(start_date=None, end_date=None))
    assert isinstance(problems, list) and "timing is missing" in problems[0]


def test_past_dates_are_rejected() -> None:
    problems = parse_trip(args(start_date="2020-05-03", end_date="2020-05-05"))
    assert isinstance(problems, list) and "in the past" in problems[0]


def test_end_before_start_is_rejected() -> None:
    assert isinstance(
        parse_trip(args(start_date=END.isoformat(), end_date=START.isoformat())), list
    )


def test_duration_contradicting_dates_is_rejected() -> None:
    problems = parse_trip(args(duration_days=7))
    assert isinstance(problems, list) and "contradicts" in problems[0]


def test_country_must_be_iso_code() -> None:
    assert trip(country="pl").country == "PL"
    problems = parse_trip(args(country="Poland"))
    assert isinstance(problems, list) and problems[0].startswith("country:")


def test_missing_fields_are_named() -> None:
    problems = parse_trip(json.dumps({"scope": "city", "duration_days": 2}))
    assert isinstance(problems, list)
    assert any(p.startswith("city:") for p in problems)


def test_garbage_arguments_are_rejected() -> None:
    assert isinstance(parse_trip("not json"), list)


def test_none_words_are_treated_as_missing() -> None:
    """The live model wrote "none" for unknown fields."""
    assert trip(district="none", end_date="none").district is None


def test_describe_states_only_validated_facts() -> None:
    assert trip(district="Praga", duration_days=3).describe() == (
        f"Destination: Praga, Warsaw, country PL. Dates: {START} to {END}, 3 days."
    )
    assert trip(start_date=None, end_date=None, duration_days=1).describe() == (
        "Destination: Warsaw, country PL. Dates: 1 day."
    )


# ---- chat -> summary -> find_places


class Script:
    """Replays queued model replies (an Exception is raised instead); records every request."""

    def __init__(self, *replies: SimpleNamespace | Exception) -> None:
        self.queue = list(replies)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        reply = self.queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=reply)])


def tool_call(arguments: str) -> SimpleNamespace:
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name=TOOL_NAME, arguments=arguments))
    return SimpleNamespace(content=None, tool_calls=[call])


def text(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=None)


class FakeFinder:
    def __init__(self, found: FoundPlaces) -> None:
        self.found = found
        self.requests: list[str] = []

    def __call__(self, request: str) -> FoundPlaces:
        self.requests.append(request)
        return self.found


def make_service(
    script: Script, finder: FakeFinder | None = None, places_enabled: bool = True
) -> BettyService:
    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=script))
    settings = Settings(nvidia_api_key_2="x", betty_places_tool=places_enabled, _env_file=None)
    return BettyService(settings, client=client, place_finder=finder)


def tool_message(script: Script) -> dict[str, Any]:
    """The tool result Betty saw (the last message of the request after the tool call)."""
    message = next(
        c["messages"][-1] for c in script.calls if c["messages"][-1].get("role") == "tool"
    )
    return dict(json.loads(message["content"]))


def test_search_runs_summary_then_find_places() -> None:
    finder = FakeFinder(FoundPlaces(status="ok", total=57, places=[CAFE]))
    script = Script(
        tool_call(args()),
        text("The traveller loves castles and coffee."),  # summariser
        text("Lovely! Try Cafe A."),  # Betty's final reply
    )
    result = make_service(script, finder).chat(USER)

    assert result.reply == "Lovely! Try Cafe A."
    assert script.calls[0]["tools"][0]["function"]["name"] == TOOL_NAME
    assert len(script.calls[0]["tools"]) == 1  # search_places is Betty's only tool
    assert "tools" not in script.calls[1]  # the summariser gets no tools
    expected = f"{trip().describe()} The traveller loves castles and coffee."
    assert finder.requests == [expected]
    search = result.search
    assert search is not None
    assert search.summary == expected
    assert (search.total, search.places, search.error) == (57, [CAFE], None)
    seen = tool_message(script)
    assert seen["status"] == "ok" and seen["places"][0]["name"] == "Cafe A"


def test_summariser_sees_the_whole_conversation() -> None:
    finder = FakeFinder(FoundPlaces(status="ok"))
    script = Script(tool_call(args()), text("NONE"), text("Done"))
    make_service(script, finder).chat(USER)
    summariser_input = script.calls[1]["messages"][-1]["content"]
    assert "I love castles and coffee" in summariser_input


def test_no_preferences_sends_only_the_trip_facts() -> None:
    finder = FakeFinder(FoundPlaces(status="ok"))
    script = Script(tool_call(args()), text("NONE"), text("Done"))
    make_service(script, finder).chat(USER)
    assert finder.requests == [trip().describe()]


def test_summary_failure_falls_back_to_trip_facts() -> None:
    finder = FakeFinder(FoundPlaces(status="ok"))
    script = Script(tool_call(args()), RuntimeError("upstream 500"), text("Done"))
    result = make_service(script, finder).chat(USER)
    assert finder.requests == [trip().describe()]
    assert result.search is not None and result.search.summary == trip().describe()


def test_rejected_call_does_not_search() -> None:
    finder = FakeFinder(FoundPlaces(status="ok"))
    script = Script(tool_call(args(scope="country")), text("Which city in Poland?"))
    result = make_service(script, finder).chat(USER)
    assert result.search is None
    assert finder.requests == []
    seen = tool_message(script)
    assert seen["status"] == "rejected" and "too broad" in seen["problems"][0]


@pytest.mark.parametrize("status", ["needs_info", "error"])
def test_failed_search_keeps_the_trip_and_reports_the_problem(status: str) -> None:
    finder = FakeFinder(FoundPlaces(status=status, message="something is wrong"))  # type: ignore[arg-type]
    script = Script(tool_call(args()), text("NONE"), text("Sorry!"))
    result = make_service(script, finder).chat(USER)
    assert result.search is not None
    assert result.search.error == "something is wrong" and result.search.places == []
    assert tool_message(script)["status"] == status


def test_disabled_place_search_still_validates_the_trip() -> None:
    script = Script(tool_call(args()), text("NONE"), text("Noted!"))
    result = make_service(script, places_enabled=False).chat(USER)
    assert result.search is not None
    assert result.search.error is not None and "disabled" in result.search.error


def test_summarize_without_trip_uses_the_full_summary_prompt() -> None:
    script = Script(text("The traveller plans\n Warsaw."))
    summary = make_service(script).summarize(USER)
    assert summary == "The traveller plans Warsaw."
    assert "destination city and country" in script.calls[0]["messages"][0]["content"]


# ---- prompts and logging


def test_chat_prompt_contains_todays_date_and_rules() -> None:
    prompt = build_chat_prompt(date(2026, 10, 17))
    assert "Today's date is 2026-10-17." in prompt
    assert "no data is better than incorrect data" in prompt.lower()
    assert "SEARCH RULES" in prompt and "search_places" in prompt
    assert "find_places" not in prompt and "finalize_trip_context" not in prompt


def test_chat_sends_todays_date_to_the_model() -> None:
    script = Script(text("Where to?"))
    make_service(script).chat(USER)
    system = script.calls[0]["messages"][0]["content"]
    assert f"Today's date is {date.today().isoformat()}." in system


def test_llm_response_text_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    make_service(Script(text("Where to, traveller?"))).chat(USER)
    assert "Where to, traveller?" in caplog.text


# ---- HTTP API


def post_chat(service: BettyService) -> dict[str, Any]:
    app.dependency_overrides[get_betty_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "Warsaw, 3 days"}]}
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    return dict(response.json())


def test_api_returns_the_search() -> None:
    finder = FakeFinder(FoundPlaces(status="ok", total=1, places=[CAFE]))
    body = post_chat(make_service(Script(tool_call(args()), text("NONE"), text("Done")), finder))
    assert body["reply"] == "Done"
    assert body["search"]["trip"]["city"] == "Warsaw"
    assert body["search"]["summary"] == trip().describe()
    assert body["search"]["places"][0]["name"] == "Cafe A"
    assert body["search"]["error"] is None


def test_api_has_no_search_by_default() -> None:
    assert post_chat(make_service(Script(text("Where to?")))) == {
        "reply": "Where to?",
        "search": None,
    }


def test_api_summarize_accepts_a_trip() -> None:
    service = make_service(Script(text("The traveller loves castles.")))
    app.dependency_overrides[get_betty_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/api/v1/summarize",
            json={
                "messages": [{"role": "user", "content": "castles please"}],
                "trip": json.loads(args()),
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert response.json() == {"summary": f"{trip().describe()} The traveller loves castles."}


def test_empty_model_response_is_retried_once() -> None:
    script = Script(text(""), text("Where to?"))
    assert make_service(script).chat(USER).reply == "Where to?"
    assert len(script.calls) == 2


def test_two_empty_model_responses_fail() -> None:
    from app.services.betty_service import BettyError

    with pytest.raises(BettyError, match="empty response"):
        make_service(Script(text(""), text(""))).chat(USER)
