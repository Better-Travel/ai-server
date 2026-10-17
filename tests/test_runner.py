from collections.abc import Callable

import httpx
import pytest

from osm.runner import OverpassUnavailable, QuerySyntaxError, run_query


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_parses_nodes_and_centers() -> None:
    body = {
        "elements": [
            {"type": "node", "id": 1, "lat": 1.0, "lon": 2.0, "tags": {"name": "A"}},
            {"type": "way", "id": 2, "center": {"lat": 3.0, "lon": 4.0}, "tags": {"name": "B"}},
        ]
    }
    out = run_query("q", client=_client(lambda r: httpx.Response(200, json=body)))
    assert [(e.id, e.lat, e.lon, e.name) for e in out] == [(1, 1.0, 2.0, "A"), (2, 3.0, 4.0, "B")]


def test_400_raises_syntax_error_with_message() -> None:
    resp = httpx.Response(400, text="<p>Error: line 1: parse error</p>\n<p>other</p>")
    with pytest.raises(QuerySyntaxError, match="parse error"):
        run_query("q", client=_client(lambda r: resp))


def test_429_is_retried_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return (
            httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json={"elements": []})
        )

    assert run_query("q", client=_client(handler), backoff=0) == []
    assert len(calls) == 2


def test_persistent_429_is_unavailable_not_syntax_error() -> None:
    with pytest.raises(OverpassUnavailable):
        run_query("q", client=_client(lambda r: httpx.Response(429)), retries=1, backoff=0)


def test_sends_user_agent_and_accept_headers() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"elements": []})

    run_query("q", client=_client(handler))
    assert seen["user-agent"].startswith("ai-server") and seen["accept"] == "application/json"
