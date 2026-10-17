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
    out = run_query("q", client=_client(lambda r: httpx.Response(200, json=body)), backoff=0)
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


def test_200_with_runtime_error_remark_is_not_an_empty_result() -> None:
    body = {
        "elements": [],
        "remark": 'runtime error: Query timed out in "query" at line 1 after 31 seconds.',
    }
    with pytest.raises(OverpassUnavailable, match="timed out"):
        run_query("q", client=_client(lambda r: httpx.Response(200, json=body)), backoff=0)


def test_500_is_unavailable_not_a_raw_http_error() -> None:
    with pytest.raises(OverpassUnavailable):
        run_query("q", client=_client(lambda r: httpx.Response(500)), retries=1, backoff=0)


def test_fails_over_to_next_endpoint_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVERPASS_URLS", "https://a.example/api, https://b.example/api")
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "a.example":
            return httpx.Response(500, text="<html>Internal Server Error</html>")
        return httpx.Response(200, json={"elements": []})

    assert run_query("q", client=_client(handler), backoff=0) == []
    assert hosts == ["a.example", "b.example"]


def test_syntax_error_does_not_fail_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVERPASS_URLS", "https://a.example/api,https://b.example/api")
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(400, text="Error: parse error")

    with pytest.raises(QuerySyntaxError):
        run_query("q", client=_client(handler))
    assert hosts == ["a.example"]


def test_all_endpoints_down_reports_hosts_without_full_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OVERPASS_URLS", "https://a.example/SECRETKEY/api")
    with pytest.raises(OverpassUnavailable) as info:
        run_query("q", client=_client(lambda r: httpx.Response(500)), retries=1, backoff=0)
    assert "a.example" in str(info.value) and "SECRETKEY" not in str(info.value)


def test_default_endpoint_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OVERPASS_URLS", raising=False)
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(500)

    with pytest.raises(OverpassUnavailable):
        run_query("q", client=_client(handler), retries=0)
    assert hosts == ["overpass-api.de", "overpass.private.coffee"]
