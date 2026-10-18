"""Streamed chat: BettyService.chat_stream and POST /api/v1/chat/stream (no network)."""

import json
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_betty_service
from app.schemas import ChatResponse, Message, PlaceSearch, StreamDelta
from app.services.betty_service import BettyService, ChatEvent
from app.services.llm_loop import BettyError
from app.services.place_finder import FoundPlaces
from app.services.search_places_tool import TOOL_NAME
from tests.test_search_places import CAFE, USER, FakeFinder, Script, args, make_service, text

Chunk = SimpleNamespace | Exception


def chunk(content: str | None = None, calls: list[SimpleNamespace] | None = None) -> Chunk:
    # `reasoning_content` is what the NVIDIA endpoint streams before the answer; it is never shown.
    delta = SimpleNamespace(content=content, tool_calls=calls, reasoning_content="thinking")
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


def call(arguments: str, *, name: str | None = TOOL_NAME, id: str | None = "c1") -> SimpleNamespace:
    """One streamed tool-call piece; later pieces of the same call have no id or name."""
    return SimpleNamespace(index=0, id=id, function=SimpleNamespace(name=name, arguments=arguments))


class FakeStream:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for item in self.chunks:
            if isinstance(item, Exception):
                raise item
            yield item

    def close(self) -> None:
        self.closed = True


class StreamScript(Script):
    """Like Script, but a list of chunks answers a `stream=True` request."""

    def __init__(self, *replies: SimpleNamespace | list[Chunk] | Exception) -> None:
        super().__init__(*replies)  # type: ignore[arg-type]
        self.streams: list[FakeStream] = []

    def create(self, **kwargs: Any) -> Any:
        if not kwargs.get("stream"):
            return super().create(**kwargs)
        self.calls.append(kwargs)
        reply: Any = self.queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        self.streams.append(FakeStream(reply))
        return self.streams[-1]


def names(events: list[ChatEvent]) -> list[str]:
    return [event.name for event in events]


def done(events: list[ChatEvent]) -> ChatResponse:
    assert events[-1].name == "done" and isinstance(events[-1].data, ChatResponse)
    return events[-1].data


def deltas(events: list[ChatEvent]) -> str:
    return "".join(e.data.text for e in events if isinstance(e.data, StreamDelta))


def test_plain_reply_streams_as_deltas_then_done() -> None:
    script = StreamScript([chunk(None), chunk(" Hi"), chunk(" there!"), chunk("  ")])
    events = list(make_service(script).chat_stream(USER))
    assert names(events) == ["delta", "delta", "delta", "done"]
    assert deltas(events) == "Hi there!  "  # leading whitespace dropped, reasoning not shown
    assert done(events).reply == "Hi there!" and done(events).search is None
    assert script.calls[0]["stream"] is True
    assert script.calls[0]["tools"][0]["function"]["name"] == TOOL_NAME
    assert script.streams[0].closed


def test_search_turn_sends_status_search_and_the_route() -> None:
    finder = FakeFinder(FoundPlaces(status="ok", total=1, places=[CAFE]))
    script = StreamScript(
        [chunk(calls=[call(args()[:20])]), chunk(calls=[call(args()[20:], name=None, id=None)])],
        text("The traveller loves castles and coffee."),  # summariser (not streamed)
        [chunk("Try "), chunk("Cafe A.")],
    )
    events = list(make_service(script, finder).chat_stream(USER))

    assert names(events) == ["status", "search", "delta", "delta", "done"]
    assert events[0].data.model_dump() == {"stage": "searching_places"}
    search = events[1].data
    assert isinstance(search, PlaceSearch) and search.places == [CAFE]
    assert [stop.osm_id for stop in search.routes[0].stops] == [CAFE.osm_id]
    assert done(events).reply == "Try Cafe A." and done(events).search == search
    assert len(finder.requests) == 1  # the split tool-call arguments were joined
    tool = script.calls[-1]["messages"][-1]
    assert tool["role"] == "tool" and json.loads(tool["content"])["status"] == "ok"


def test_text_before_a_tool_call_stays_and_the_next_round_starts_a_paragraph() -> None:
    finder = FakeFinder(FoundPlaces(status="ok", total=1, places=[CAFE]))
    script = StreamScript(
        [chunk("Let me look."), chunk(calls=[call(args())])],
        text("NONE"),
        [chunk("Here you go.")],
    )
    events = list(make_service(script, finder).chat_stream(USER))
    assert deltas(events) == "Let me look.\n\nHere you go."
    assert done(events).reply == "Let me look.\n\nHere you go."
    assert script.calls[-1]["messages"][-2]["content"] == "Let me look."  # with the tool call


def test_rejected_trip_sends_status_but_no_search() -> None:
    script = StreamScript([chunk(calls=[call(args(scope="country"))])], [chunk("Which city?")])
    events = list(make_service(script).chat_stream(USER))
    assert names(events) == ["status", "delta", "done"]
    assert done(events).search is None


def test_empty_stream_is_retried_once() -> None:
    script = StreamScript([], [chunk("Hello!")])
    events = list(make_service(script).chat_stream(USER))
    assert done(events).reply == "Hello!" and len(script.calls) == 2


def test_two_empty_streams_fail() -> None:
    with pytest.raises(BettyError, match="empty"):
        list(make_service(StreamScript([], [])).chat_stream(USER))


def test_failure_mid_stream_raises_after_the_text_so_far() -> None:
    script = StreamScript([chunk("Hel"), RuntimeError("connection reset")])
    stream = make_service(script).chat_stream(USER)
    assert next(stream).data == StreamDelta(text="Hel")
    with pytest.raises(BettyError, match="connection reset"):
        next(stream)
    assert script.streams[0].closed


def test_closing_the_generator_closes_the_model_stream() -> None:
    script = StreamScript([chunk("Hi"), chunk(" there")])
    stream = make_service(script).chat_stream(USER)
    next(stream)
    stream.close()
    assert script.streams[0].closed


# ---- POST /api/v1/chat/stream


def sse(body: str) -> list[tuple[str, Any]]:
    """Parse an SSE body into (event, data) pairs; comments become ("comment", text)."""
    parsed: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            parsed.append(("comment", block[1:].strip()))
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


def post_stream(service: BettyService, messages: list[dict[str, str]] | None = None) -> Any:
    app.dependency_overrides[get_betty_service] = lambda: service
    try:
        body = {"messages": messages or [m.model_dump() for m in USER]}
        return TestClient(app).post("/api/v1/chat/stream", json=body)
    finally:
        app.dependency_overrides.clear()


def test_api_streams_start_deltas_and_done() -> None:
    response = post_stream(make_service(StreamScript([chunk("Hi"), chunk(" there!")])))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    events = sse(response.text)
    assert [name for name, _ in events] == ["start", "delta", "delta", "done"]
    assert len(events[0][1]["turn_id"]) == 12
    assert events[-1][1] == {"reply": "Hi there!", "events": None, "search": None}


def test_api_sends_the_search_with_routes_before_the_reply() -> None:
    finder = FakeFinder(FoundPlaces(status="ok", total=1, places=[CAFE]))
    script = StreamScript([chunk(calls=[call(args())])], text("NONE"), [chunk("Try Cafe A.")])
    events = sse(post_stream(make_service(script, finder)).text)
    assert [name for name, _ in events] == ["start", "status", "search", "delta", "done"]
    route = events[2][1]["routes"][0]
    assert [(s["osm_id"], s["lat"], s["lon"]) for s in route["stops"]] == [
        (CAFE.osm_id, CAFE.lat, CAFE.lon)
    ]
    assert events[-1][1]["search"] == events[2][1]


def test_api_model_failure_is_an_error_event() -> None:
    events = sse(post_stream(make_service(StreamScript(RuntimeError("model down")))).text)
    assert [name for name, _ in events] == ["start", "error"]
    error = events[1][1]
    assert error["code"] == "upstream_unavailable" and error["retryable"] is True
    assert "model down" in error["message"]


def test_api_crash_is_an_internal_error_event() -> None:
    class Broken(FakeFinder):
        def __call__(self, request: str) -> FoundPlaces:
            raise RuntimeError("bug")

    script = StreamScript([chunk(calls=[call(args())])], text("NONE"))
    events = sse(post_stream(make_service(script, Broken(FoundPlaces(status="ok")))).text)
    assert [name for name, _ in events] == ["start", "status", "error"]
    assert events[-1][1] == {
        "code": "internal",
        "message": "Unexpected server error.",
        "retryable": True,
    }


def test_api_sends_keep_alives_while_searching(monkeypatch: pytest.MonkeyPatch) -> None:
    class Slow(FakeFinder):
        def __call__(self, request: str) -> FoundPlaces:
            time.sleep(0.3)
            return super().__call__(request)

    monkeypatch.setattr("app.main.PING_SECONDS", 0.05)
    script = StreamScript([chunk(calls=[call(args())])], text("NONE"), [chunk("Done.")])
    events = sse(post_stream(make_service(script, Slow(FoundPlaces(status="ok")))).text)
    assert ("comment", "ping") in events
    assert events[-1][0] == "done"


def test_api_rejects_assistant_last_before_streaming() -> None:
    response = post_stream(
        make_service(StreamScript()),
        [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}],
    )
    assert response.status_code == 422


def test_blocking_chat_still_works_alongside() -> None:
    assert make_service(Script(text("Hello"))).chat([Message(role="user", content="hi")]).reply == (
        "Hello"
    )
