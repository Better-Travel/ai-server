"""finalize_trip_context: readiness criteria and the chat/summarize wiring (no network)."""

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_betty_service
from app.schemas import Message
from app.services.betty_service import BettyService
from app.services.trip_context_tool import TOOL_NAME, parse_trip_context

START = date.today() + timedelta(days=200)
END = START + timedelta(days=2)


def args(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "destination": {"city": "Warsaw", "country": "Poland", "scope": "city"},
        "start_date": START.isoformat(),
        "end_date": END.isoformat(),
        "interests": ["local food"],
    }
    data.update(overrides)
    return json.dumps(data)


def test_valid_context_is_accepted() -> None:
    context, feedback = parse_trip_context(args())
    assert context is not None
    assert context.destination.city == "Warsaw"
    assert json.loads(feedback)["status"] == "ok"


def test_duration_without_dates_is_enough() -> None:
    context, _ = parse_trip_context(args(start_date=None, end_date=None, duration_days=3))
    assert context is not None


@pytest.mark.parametrize("scope", ["region", "country"])
def test_too_broad_destination_is_rejected(scope: str) -> None:
    context, feedback = parse_trip_context(
        args(destination={"city": "Poland", "country": "Poland", "scope": scope})
    )
    assert context is None
    assert "too broad" in feedback


def test_missing_timing_is_rejected() -> None:
    context, feedback = parse_trip_context(args(start_date=None, end_date=None))
    assert context is None
    assert "timing is missing" in feedback


def test_end_before_start_is_rejected() -> None:
    context, _ = parse_trip_context(args(start_date=END.isoformat(), end_date=START.isoformat()))
    assert context is None


def test_garbage_arguments_are_rejected() -> None:
    context, feedback = parse_trip_context("not json")
    assert context is None
    assert json.loads(feedback)["status"] == "rejected"


class Script:
    """Replays queued model messages; records every request."""

    def __init__(self, *messages: SimpleNamespace) -> None:
        self.queue = list(messages)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.queue.pop(0))])


def tool_call(arguments: str, call_id: str = "c1") -> SimpleNamespace:
    call = SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=TOOL_NAME, arguments=arguments)
    )
    return SimpleNamespace(content=None, tool_calls=[call])


def text(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=None)


def make_service(script: Script) -> BettyService:
    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=script))
    settings = Settings(nvidia_api_key_2="x", betty_places_tool=False, _env_file=None)
    return BettyService(settings, client=client)


USER = [Message(role="user", content="Warsaw, May 3-5")]


def test_chat_returns_context_after_tool_call() -> None:
    script = Script(tool_call(args()), text("Got it: Warsaw, May 3-5!"))
    result = make_service(script).chat(USER)
    assert result.reply == "Got it: Warsaw, May 3-5!"
    assert result.trip_context is not None
    assert result.trip_context.duration_days is None
    assert script.calls[0]["tools"][0]["function"]["name"] == TOOL_NAME


def test_rejected_call_gives_no_context_and_feedback_reaches_model() -> None:
    bad = args(destination={"city": "Poland", "country": "Poland", "scope": "country"})
    script = Script(tool_call(bad), text("Which city in Poland?"))
    result = make_service(script).chat(USER)
    assert result.trip_context is None
    tool_msg = script.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert "too broad" in tool_msg["content"]


def test_summarize_includes_structured_context() -> None:
    script = Script(text("The traveller plans Warsaw."))
    context, _ = parse_trip_context(args())
    make_service(script).summarize(USER, context)
    prompt = script.calls[0]["messages"][-1]["content"]
    assert "Confirmed trip details" in prompt
    assert '"city":"Warsaw"' in prompt
    assert "tools" not in script.calls[0]


def test_api_exposes_ready_flag() -> None:
    service = make_service(Script(tool_call(args()), text("All set!")))
    app.dependency_overrides[get_betty_service] = lambda: service
    try:
        r = TestClient(app).post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "Warsaw, May 3-5"}]}
        )
    finally:
        app.dependency_overrides.clear()
    body = r.json()
    assert r.status_code == 200
    assert body["ready"] is True
    assert body["trip_context"]["destination"]["city"] == "Warsaw"


def test_api_not_ready_by_default() -> None:
    service = make_service(Script(text("Where to?")))
    app.dependency_overrides[get_betty_service] = lambda: service
    try:
        r = TestClient(app).post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    finally:
        app.dependency_overrides.clear()
    assert r.json() == {"reply": "Where to?", "ready": False, "trip_context": None}


def test_tolerates_llm_quirks_seen_in_live_model() -> None:
    """Live model JSON-encoded `destination` and wrote "none" for unknown fields."""
    raw = json.dumps(
        {
            "destination": json.dumps({"city": "Warsaw", "country": "Poland", "scope": "city"}),
            "start_date": START.isoformat(),
            "duration_days": 3,
            "interests": ["local food", "none"],
            "companions": "none",
            "budget": "none",
            "free_hours": "none",
            "notes": "none",
        }
    )
    context, _ = parse_trip_context(raw)
    assert context is not None
    assert context.destination.city == "Warsaw"
    assert context.companions is None and context.budget is None
    assert context.interests == ["local food"]


def test_llm_response_text_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    make_service(Script(text("Where to, traveller?"))).chat(USER)
    assert "Where to, traveller?" in caplog.text


def test_past_dates_are_rejected() -> None:
    context, feedback = parse_trip_context(args(start_date="2020-05-03", end_date="2020-05-05"))
    assert context is None
    assert "in the past" in feedback


def test_duration_contradicting_dates_is_rejected() -> None:
    context, feedback = parse_trip_context(args(duration_days=7))
    assert context is None
    assert "contradicts" in feedback


def test_chat_prompt_contains_todays_date_and_integrity_rules() -> None:
    from app.prompts import build_chat_prompt

    prompt = build_chat_prompt(date(2026, 10, 17))
    assert "Today's date is 2026-10-17." in prompt
    assert "no data is better than incorrect data" in prompt.lower()
    assert "READINESS RULES" in prompt


def test_chat_sends_todays_date_to_the_model() -> None:
    script = Script(text("Where to?"))
    make_service(script).chat(USER)
    system = script.calls[0]["messages"][0]["content"]
    assert f"Today's date is {date.today().isoformat()}." in system
