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


TRIP = {"city": "Warsaw", "country": "PL", "scope": "city", "duration_days": 3}
SEARCH = {
    "trip": TRIP,
    "summary": "Destination: Warsaw, country PL. Dates: 3 days.",
    "query": "[out:json];\nout center tags;",
    "query_attempts": 2,
    "fetched": 3,
    "total": 1,
    "places": [
        {
            "name": "Cafe A",
            "type": "node",
            "category": "amenity=cafe",
            "lat": 52.2,
            "lon": 21.0,
            "tags": {"amenity": "cafe", "addr:street": "Prosta", "addr:housenumber": "1"},
        }
    ],
    "error": None,
}
searching = False


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    global searching
    searching = False
    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        recorded.append((url, json))
        if url.endswith("/summarize"):
            return FakeResponse({"summary": "A tidy paragraph."})
        return FakeResponse({"reply": "Lovely!", "search": SEARCH if searching else None})

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
    assert any("No search this turn" in w.value for w in at.warning)


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


def test_search_is_shown_and_its_trip_is_sent_to_summarize(
    calls: list[tuple[str, dict[str, Any]]],
) -> None:
    global searching
    searching = True
    at = AppTest.from_file(APP, default_timeout=30).run()
    chat_once(at)
    assert any("returned 3 elements, 1 distinct places, showing 1" in s.value for s in at.success)
    # the generated Overpass query is displayed, with the number of syntax fixes it took
    assert any(c.value == SEARCH["query"] for c in at.code)
    assert any("after 1 syntax fix" in m.value for m in at.markdown)
    button(at, "run_history_summarize").click().run()
    assert calls[-1][1]["trip"] == TRIP


# ---- the guide panel

NARRATION = {
    "title": "Warsaw Barbican",
    "lang": "en",
    "url": "https://en.wikipedia.org/wiki/Warsaw_Barbican",
    "attribution": "Text from Wikipedia (en), licensed CC BY-SA 4.0",
    "segments": [{"id": i, "text": f"Segment {i}."} for i in range(3)],
}
ANSWER = {
    "answer": "It was built in 1548.",
    "used_web": True,
    "sources": [{"title": "Official site", "url": "https://zamek.pl"}],
    "resume": {"from_segment": 1, "bridge": "Back to Warsaw Barbican."},
    "finished": False,
}


@pytest.fixture
def guide_api(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_get(url: str, params: dict[str, Any], timeout: float) -> FakeResponse:
        recorded.append((url, params))
        return FakeResponse(NARRATION)

    def fake_post(url: str, json: dict[str, Any], timeout: float) -> FakeResponse:
        recorded.append((url, json))
        return FakeResponse(ANSWER)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    return recorded


def text_input(at: AppTest, label: str) -> Any:
    return next(t for t in at.text_input if t.label == label)


def test_guide_loads_a_narration_and_steps_through_it(
    guide_api: list[tuple[str, dict[str, Any]]],
) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    text_input(at, "or a Wikipedia title").set_value("Warsaw Barbican")
    button(at, "guide_load").click().run()
    assert guide_api[-1] == (
        "http://localhost:8000/api/v1/narration",
        {"lang": "en", "title": "Warsaw Barbican"},
    )
    assert any(m.value.startswith("▶ Segment 0.") for m in at.markdown)
    button(at, "guide_next").click().run()
    assert any(m.value.startswith("✓ Segment 0.") for m in at.markdown)
    assert any(m.value.startswith("▶ Segment 1.") for m in at.markdown)


def test_guide_question_sends_the_cursor_and_resumes_where_told(
    guide_api: list[tuple[str, dict[str, Any]]],
) -> None:
    at = AppTest.from_file(APP, default_timeout=30).run()
    text_input(at, "or a Wikipedia title").set_value("Warsaw Barbican")
    button(at, "guide_load").click().run()
    button(at, "guide_next").click().run()
    button(at, "guide_next").click().run()  # now speaking segment 2
    text_input(at, "Interrupt and ask a question").set_value("Who built it?")
    next(b for b in at.button if b.label == "✋ Interrupt & ask").click().run()

    url, body = guide_api[-1]
    assert url.endswith("/api/v1/narration/ask")
    assert body["cursor"] == 2 and body["question"] == "Who built it?" and body["history"] == []
    assert body["place"] == {"lang": "en", "title": "Warsaw Barbican"}
    assert at.session_state["guide_cursor"] == 1  # resumes at the segment the server named
    assert at.session_state["guide_history"][-1] == {
        "role": "assistant",
        "content": ANSWER["answer"],
    }
    assert any("It was built in 1548." in m.value for m in at.markdown)
    assert any("Back to Warsaw Barbican." in i.value and "segment 2" in i.value for i in at.info)
