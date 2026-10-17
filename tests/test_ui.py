"""Smoke tests for the Streamlit app via Streamlit's headless AppTest (API calls mocked)."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "ui" / "streamlit_app.py")


class FakeResponse:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        recorded.append((url, json))
        if url.endswith("/summarize"):
            return FakeResponse({"summary": "A tidy paragraph."})
        return FakeResponse({"reply": "Lovely!", "ready": False, "trip_context": None})

    monkeypatch.setattr(httpx, "post", fake_post)
    return recorded


def button(at: AppTest, key: str) -> Any:
    return next(b for b in at.button if b.key == key)


def chat_once(at: AppTest) -> None:
    at.chat_input[0].set_value("Warsaw in May").run()
    assert not at.exception


def test_history_buttons_are_disabled_for_an_empty_chat(
    calls: list[tuple[str, dict[str, Any]]],
) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert button(at, "run_history_summarize").disabled
    assert button(at, "run_history_check").disabled


def test_summarize_with_chat_history(calls: list[tuple[str, dict[str, Any]]]) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    chat_once(at)
    button(at, "run_history_summarize").click().run()
    url, body = calls[-1]
    assert url.endswith("/api/v1/summarize")
    assert body["messages"] == [
        {"role": "user", "content": "Warsaw in May"},
        {"role": "assistant", "content": "Lovely!"},
    ]
    assert any("A tidy paragraph." in i.value for i in at.info)


def test_check_with_chat_history_ends_on_user_turn(
    calls: list[tuple[str, dict[str, Any]]],
) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    chat_once(at)
    button(at, "run_history_check").click().run()
    url, body = calls[-1]
    assert url.endswith("/api/v1/chat")
    assert body["messages"] == [{"role": "user", "content": "Warsaw in May"}]
    assert any("ready = false" in w.value for w in at.warning)


def test_run_with_plain_text_input(calls: list[tuple[str, dict[str, Any]]]) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_area(key="input_check").set_value("3 days in Warsaw").run()
    button(at, "run_input_check").click().run()
    url, body = calls[-1]
    assert url.endswith("/api/v1/chat")
    assert body == {"messages": [{"role": "user", "content": "3 days in Warsaw"}]}


def test_run_with_json_input(calls: list[tuple[str, dict[str, Any]]]) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    raw = '{"messages": [{"role": "user", "content": "hi"}]}'
    at.text_area(key="input_summarize").set_value(raw).run()
    button(at, "run_input_summarize").click().run()
    assert calls[-1][1] == {"messages": [{"role": "user", "content": "hi"}]}
