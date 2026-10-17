import os
import time

import httpx

from .schemas import OverpassElement

DEFAULT_URL = "https://overpass-api.de/api/interpreter"
TRANSIENT_STATUSES = {429, 502, 503, 504}
# overpass-api.de answers 406 to requests with a generic client User-Agent or no Accept header.
HEADERS = {"User-Agent": "ai-server-osm-query-builder/0.1", "Accept": "application/json"}


class QuerySyntaxError(Exception):
    """Overpass rejected the query itself; the model can be asked to fix it."""


class OverpassUnavailable(Exception):
    """Overpass failed for reasons unrelated to the query text."""


def _error_text(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if "error" in line.lower()]
    return "\n".join(lines) or body.strip()[:500]


def _to_element(raw: dict) -> OverpassElement:
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


def run_query(
    ql: str,
    *,
    client: httpx.Client | None = None,
    retries: int = 3,
    backoff: float = 2.0,
) -> list[OverpassElement]:
    url = os.environ.get("OVERPASS_URL", DEFAULT_URL)
    own_client = client is None
    client = client or httpx.Client(timeout=60)
    try:
        for attempt in range(retries + 1):
            try:
                response = client.post(url, data={"data": ql}, headers=HEADERS)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise OverpassUnavailable(str(exc)) from exc
            else:
                if response.status_code == 400:
                    raise QuerySyntaxError(_error_text(response.text))
                if response.status_code not in TRANSIENT_STATUSES:
                    response.raise_for_status()
                    return [_to_element(e) for e in response.json().get("elements", [])]
                if attempt == retries:
                    raise OverpassUnavailable(f"HTTP {response.status_code}")
            time.sleep(backoff * (2**attempt))
    finally:
        if own_client:
            client.close()
    raise AssertionError("unreachable")
