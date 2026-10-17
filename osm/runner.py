import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .schemas import OverpassElement

DEFAULT_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
# overpass-api.de answers 406 to requests with a generic client User-Agent or no Accept header.
HEADERS = {"User-Agent": "ai-server-osm-query-builder/0.1", "Accept": "application/json"}


class QuerySyntaxError(Exception):
    """Overpass rejected the query itself; the model can be asked to fix it."""


class OverpassUnavailable(Exception):
    """Overpass failed for reasons unrelated to the query text."""


def _error_text(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if "error" in line.lower()]
    return "\n".join(lines) or body.strip()[:500]


def _to_element(raw: dict[str, Any]) -> OverpassElement:
    center = raw.get("center", {})
    tags = raw.get("tags", {})
    return OverpassElement(
        id=raw["id"],
        type=raw["type"],
        lat=raw.get("lat", center.get("lat")),
        lon=raw.get("lon", center.get("lon")),
        name=tags.get("name"),
        tags=tags,
    )


def _urls() -> list[str]:
    """Endpoints in priority order: OVERPASS_URLS (comma-separated), OVERPASS_URL, or defaults."""
    raw = os.environ.get("OVERPASS_URLS") or os.environ.get("OVERPASS_URL")
    return [u.strip() for u in raw.split(",") if u.strip()] if raw else DEFAULT_URLS


def run_query(
    ql: str,
    *,
    client: httpx.Client | None = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> list[OverpassElement]:
    """Run a query, trying each endpoint in turn; back off and repeat if all of them fail."""
    urls = _urls()
    own_client = client is None
    client = client or httpx.Client(timeout=60)
    last_error = "no endpoints configured"
    try:
        for attempt in range(retries + 1):
            for url in urls:
                host = urlsplit(url).netloc  # never log the full URL: it may embed an API key
                try:
                    response = client.post(url, data={"data": ql}, headers=HEADERS)
                except httpx.TransportError as exc:
                    last_error = f"{host}: {exc}"
                    continue
                if response.status_code == 400:
                    raise QuerySyntaxError(_error_text(response.text))
                if response.status_code in TRANSIENT_STATUSES:
                    last_error = f"{host}: HTTP {response.status_code}"
                    continue
                response.raise_for_status()
                body = response.json()
                # On timeout/out-of-memory Overpass answers HTTP 200 with partial or empty
                # elements and a "remark"; returning that would look like "no places found".
                remark = str(body.get("remark", ""))
                if "runtime error" in remark:
                    last_error = f"{host}: {remark}"
                    continue
                return [_to_element(e) for e in body.get("elements", [])]
            if attempt < retries:
                time.sleep(backoff * (2**attempt))
    finally:
        if own_client:
            client.close()
    raise OverpassUnavailable(last_error)
