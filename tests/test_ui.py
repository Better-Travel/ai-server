"""Smoke tests for the Streamlit app via Streamlit's headless AppTest (API calls mocked)."""

import json
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


def test_debug_tools_load_chat_and_run(calls: list[tuple[str, dict[str, Any]]]) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.chat_input[0].set_value("Warsaw in May").run()
    assert not at.exception

    # "Load current chat" fills the textarea with the exact request shape.
    next(b for b in at.button if b.key == "load_summarize").click().run()
    loaded = json.loads(at.text_area(key="debug_input_summarize").value or "")
    assert loaded["messages"][0] == {"role": "user", "content": "Warsaw in May"}
    assert loaded["messages"][-1]["role"] == "assistant"

    next(b for b in at.button if b.key == "run_summarize").click().run()
    assert calls[-1][0].endswith("/api/v1/summarize")
    assert calls[-1][1]["messages"] == loaded["messages"]
    assert any("A tidy paragraph." in i.value for i in at.info)


def test_debug_plain_text_is_sent_as_user_message(calls: list[tuple[str, dict[str, Any]]]) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_area(key="debug_input_check").set_value("3 days in Warsaw").run()
    next(b for b in at.button if b.key == "run_check").click().run()
    url, body = calls[-1]
    assert url.endswith("/api/v1/chat")
    assert body == {"messages": [{"role": "user", "content": "3 days in Warsaw"}]}
    assert any("ready = false" in w.value for w in at.warning)
