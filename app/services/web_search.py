"""Web search for the sight guide, behind a small interface (Tavily is the first provider)."""

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS = 4
MAX_SNIPPET_CHARS = 600
MAX_QUERY_CHARS = 200


class SearchError(RuntimeError):
    """The search could not be done (network, quota, bad key, unexpected answer)."""


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str


class WebSearcher(Protocol):
    def __call__(self, query: str) -> list[WebResult]: ...


def _trim(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_SNIPPET_CHARS:
        return text
    return text[:MAX_SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"


class TavilySearch:
    """Tavily's search API: ranked results with page text, so we never fetch pages ourselves."""

    def __init__(
        self, api_key: str, client: httpx.Client | None = None, timeout: float = 10.0
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    def __call__(self, query: str) -> list[WebResult]:
        body = {
            "query": query.strip()[:MAX_QUERY_CHARS],
            "search_depth": "basic",
            "max_results": MAX_RESULTS,
            "include_answer": False,
            "include_raw_content": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.post(TAVILY_URL, json=body, headers=headers)
            response.raise_for_status()
            items = response.json()["results"]
            return [
                WebResult(
                    title=" ".join(str(item.get("title") or "").split()),
                    url=str(item["url"]),
                    snippet=_trim(str(item.get("content") or "")),
                )
                for item in items[:MAX_RESULTS]
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # The message may echo the request; the API key is only ever in the header.
            raise SearchError(f"web search failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
