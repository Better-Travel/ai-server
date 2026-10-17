"""Look up OSM `wikipedia=lang:Title` tags on Wikipedia by exact title, in Polish and English."""

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import httpx

from .schemas import OverpassElement, WikipediaInfo, WikipediaText

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "ai-server-osm-query-builder/0.1", "Accept": "application/json"}
CHUNK_SIZE = 20  # the extracts API returns at most 20 intros per request
OUTPUT_LANGS = ("pl", "en")
# The language becomes part of a hostname, so only plain language codes are accepted.
_TAG = re.compile(r"^(?P<lang>[a-z]{2,3}(?:-[a-z0-9]+)*|simple):(?P<title>.+)$")

WikiRef = tuple[str, str]
_BASE_PARAMS = {"action": "query", "format": "json", "formatversion": "2", "redirects": "1"}


def parse_wikipedia_tag(value: str) -> WikiRef | None:
    """Split an OSM `lang:Title` value; None if it can't be looked up by exact title."""
    match = _TAG.match(value.split(";")[0].strip())
    if not match:
        return None
    title = match["title"].split("#")[0].strip()
    if not title or "|" in title:
        return None
    return match["lang"], title


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _group_by_lang(refs: Iterable[WikiRef]) -> dict[str, list[str]]:
    by_lang: dict[str, list[str]] = defaultdict(list)
    for lang, title in dict.fromkeys(refs):
        by_lang[lang].append(title)
    return by_lang


def _query(
    lang: str, titles: list[str], extra: dict[str, str], client: httpx.Client
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """One API call. Returns (requested title -> resolved title, resolved title -> page)."""
    params = {**_BASE_PARAMS, "titles": "|".join(titles), **extra}
    response = client.get(f"https://{lang}.wikipedia.org/w/api.php", params=params, headers=HEADERS)
    response.raise_for_status()
    query: dict[str, Any] = response.json().get("query", {})
    normalized = {n["from"]: n["to"] for n in query.get("normalized", [])}
    redirects = {r["from"]: r["to"] for r in query.get("redirects", [])}
    resolved = {}
    for requested in titles:
        title = normalized.get(requested, requested)
        resolved[requested] = redirects.get(title, title)
    return resolved, {page["title"]: page for page in query.get("pages", [])}


def _fetch_texts(refs: Iterable[WikiRef], client: httpx.Client) -> dict[WikiRef, WikipediaText]:
    """Intro text per (lang, exact title); missing/disambiguation pages and errors are skipped."""
    extra = {
        "prop": "extracts|info|pageprops",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "max",
        "exsentences": "3",
        "inprop": "url",
        "ppprop": "disambiguation",
    }
    found: dict[WikiRef, WikipediaText] = {}
    for lang, titles in _group_by_lang(refs).items():
        for chunk in _chunks(titles, CHUNK_SIZE):
            try:
                resolved, pages = _query(lang, chunk, extra, client)
                for requested in chunk:
                    page = pages.get(resolved[requested])
                    if (
                        not page
                        or page.get("missing")
                        or "disambiguation" in page.get("pageprops", {})
                    ):
                        continue
                    extract = (page.get("extract") or "").strip()
                    if extract:
                        found[(lang, requested)] = WikipediaText(
                            title=page["title"], url=page["fullurl"], extract=extract
                        )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("Wikipedia text lookup failed (%s, %d): %s", lang, len(chunk), exc)
    return found


def _fetch_translations(
    refs: Iterable[WikiRef], target: str, client: httpx.Client
) -> dict[WikiRef, WikiRef]:
    """Map (lang, title) to the same article's (target, title) using Wikipedia's language links."""
    extra = {"prop": "langlinks", "lllang": target, "lllimit": "max"}
    linked: dict[WikiRef, WikiRef] = {}
    for lang, titles in _group_by_lang(refs).items():
        for chunk in _chunks(titles, CHUNK_SIZE):
            try:
                resolved, pages = _query(lang, chunk, extra, client)
                for requested in chunk:
                    links = pages.get(resolved[requested], {}).get("langlinks", [])
                    if links:
                        linked[(lang, requested)] = (target, links[0]["title"])
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("Wikipedia langlinks failed (%s->%s): %s", lang, target, exc)
    return linked


def fetch_summaries(
    refs: Iterable[WikiRef], client: httpx.Client | None = None
) -> dict[WikiRef, WikipediaInfo]:
    """Polish and English intros for (lang, title) pairs; failed requests are skipped."""
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        refs = list(dict.fromkeys(refs))
        source = _fetch_texts(refs, client)
        texts: dict[WikiRef, dict[str, WikipediaText]] = {ref: {} for ref in source}
        for ref, text in source.items():
            if ref[0] in OUTPUT_LANGS:
                texts[ref][ref[0]] = text
        for target in OUTPUT_LANGS:
            needing = [ref for ref in source if ref[0] != target]
            links = _fetch_translations(needing, target, client)
            translated = _fetch_texts(set(links.values()), client)
            for ref, target_ref in links.items():
                if target_ref in translated:
                    texts[ref][target] = translated[target_ref]
        return {
            ref: WikipediaInfo(pl=found.get("pl"), en=found.get("en"))
            for ref, found in texts.items()
            if found
        }
    finally:
        if own_client:
            client.close()


def enrich(
    elements: list[OverpassElement], client: httpx.Client | None = None
) -> list[OverpassElement]:
    """Attach Polish/English Wikipedia summaries to elements with a usable `wikipedia` tag."""
    refs = [parse_wikipedia_tag(element.tags.get("wikipedia", "")) for element in elements]
    if not any(refs):
        return elements
    summaries = fetch_summaries([ref for ref in refs if ref], client)
    return [
        element.model_copy(update={"wikipedia": summaries.get(ref)}) if ref else element
        for element, ref in zip(elements, refs, strict=True)
    ]
