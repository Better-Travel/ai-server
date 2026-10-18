"""The Tavily search client (no network)."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.services.web_search import MAX_SNIPPET_CHARS, SearchError, TavilySearch


def search_with(handler: Callable[[httpx.Request], httpx.Response]) -> TavilySearch:
    return TavilySearch("tvly-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))


def ok(results: list[dict[str, str]]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json={"results": results})


def test_results_are_parsed() -> None:
    results = search_with(
        ok(
            [
                {
                    "title": "  Royal  Castle ",
                    "url": "https://zamek-krolewski.pl",
                    "content": "Open 10-18.",
                }
            ]
        )
    )("Royal Castle Warsaw opening hours")
    assert [(r.title, r.url, r.snippet) for r in results] == [
        ("Royal Castle", "https://zamek-krolewski.pl", "Open 10-18.")
    ]


def test_request_has_the_key_in_the_header_and_a_bounded_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    search_with(handler)("q" * 500)
    request = seen[0]
    body = json.loads(request.content)
    assert request.url == "https://api.tavily.com/search"
    assert request.headers["authorization"] == "Bearer tvly-secret"
    assert "tvly-secret" not in request.content.decode()  # never in the body
    assert (
        len(body["query"]) == 200 and body["max_results"] == 4 and body["include_answer"] is False
    )


def test_long_snippets_are_trimmed_and_results_capped() -> None:
    many = [{"title": f"T{i}", "url": f"https://x/{i}", "content": "word " * 400} for i in range(9)]
    results = search_with(ok(many))("q")
    assert len(results) == 4
    assert all(len(r.snippet) <= MAX_SNIPPET_CHARS + 1 and r.snippet.endswith("…") for r in results)


def test_missing_optional_fields_are_tolerated() -> None:
    [result] = search_with(ok([{"url": "https://x"}]))("q")
    assert (result.title, result.snippet) == ("", "")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "bad key"}),
        httpx.Response(429),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": 1}),
        httpx.Response(200, json={"results": [{"title": "no url"}]}),
        httpx.Response(200, json={"results": "nope"}),
    ],
)
def test_failures_become_search_errors(response: httpx.Response) -> None:
    with pytest.raises(SearchError):
        search_with(lambda request: response)("q")


def test_connection_failures_become_search_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(SearchError):
        search_with(handler)("q")
