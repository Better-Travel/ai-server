"""Look up OSM `wikipedia=lang:Title` tags on Wikipedia by exact title, in Polish and English."""

import html
import logging
import re
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from .schemas import OverpassElement, WikipediaInfo, WikipediaText

logger = logging.getLogger(__name__)

# Wikimedia asks for a User-Agent that identifies the project and says how to reach it.
HEADERS = {
    "User-Agent": "ai-server/0.1 (https://github.com/Better-Travel/ai-server)",
    "Accept": "application/json",
}
MAX_429_RETRIES = 2
CHUNK_SIZE = 20  # the extracts API returns at most 20 intros per request
IMAGE_WIDTH = 800  # px; the lead image is returned as a thumbnail about this wide
OUTPUT_LANGS = ("pl", "en")
# The language becomes part of a hostname, so only plain language codes are accepted.
_LANG = r"[a-z]{2,3}(?:-[a-z0-9]+)*|simple"
_TAG = re.compile(rf"^(?P<lang>{_LANG}):(?P<title>.+)$")
_LANG_ONLY = re.compile(rf"^(?:{_LANG})$")

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
    lang: str,
    titles: list[str],
    extra: dict[str, str],
    client: httpx.Client,
    site: str | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """One API call. Returns (requested title -> resolved title, resolved title -> page).

    `site` is another wiki's host (e.g. commons.wikimedia.org) instead of `lang`'s Wikipedia.
    """
    params = {**_BASE_PARAMS, "titles": "|".join(titles), **extra}
    url = f"https://{site or f'{lang}.wikipedia.org'}/w/api.php"
    for attempt in range(MAX_429_RETRIES + 1):
        response = client.get(url, params=params, headers=HEADERS)
        if response.status_code != 429 or attempt == MAX_429_RETRIES:
            break
        retry_after = response.headers.get("retry-after", "")
        time.sleep(min(float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1), 10.0))
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
    # pageimages returns only freely licensed lead images (its default), as a thumbnail.
    extra = {
        "prop": "extracts|info|pageprops|pageimages",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "max",
        "exsentences": "3",
        "inprop": "url",
        "ppprop": "disambiguation",
        "piprop": "thumbnail|name",
        "pithumbsize": str(IMAGE_WIDTH),
        "pilimit": "max",
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
                            title=page["title"],
                            url=page["fullurl"],
                            extract=extract,
                            image=page.get("thumbnail", {}).get("source"),
                            image_file=page.get("pageimage"),
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


@dataclass(frozen=True)
class ImageCredit:
    """Who made an image and under which licence, from its file description page."""

    author: str | None
    license: str | None
    license_url: str | None


COMMONS = "commons.wikimedia.org"
IMAGE_CHUNK_SIZE = 50  # imageinfo accepts 50 titles per request
_HTML_TAG = re.compile(r"<[^>]+>")


def _plain(value: Any) -> str | None:
    """extmetadata values are HTML (links to user pages); keep the visible text only."""
    if not isinstance(value, str):
        return None
    text = " ".join(html.unescape(_HTML_TAG.sub(" ", value)).split())
    return text or None


def fetch_image_credits(
    files: Iterable[tuple[str, str]], client: httpx.Client | None = None
) -> dict[tuple[str, str], ImageCredit]:
    """Author and licence per (site, file name), e.g. ("pl.wikipedia.org", "Castle.jpg").

    A Wikipedia site also answers for Commons files it uses. Missing files and failed
    requests are skipped.
    """
    by_site: dict[str, list[str]] = defaultdict(list)
    for site, file in dict.fromkeys(files):
        by_site[site].append(file)
    extra = {
        "prop": "imageinfo",
        "iiprop": "extmetadata",
        "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl",
    }
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    found: dict[tuple[str, str], ImageCredit] = {}
    try:
        for site, names in by_site.items():
            for chunk in _chunks(names, IMAGE_CHUNK_SIZE):
                titles = [f"File:{name}" for name in chunk]
                try:
                    resolved, pages = _query("", titles, extra, client, site=site)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    logger.warning("image credit lookup failed (%s, %d): %s", site, len(chunk), exc)
                    continue
                for name, title in zip(chunk, titles, strict=True):
                    # Commons files used by a Wikipedia are "missing" there but have imageinfo.
                    info = pages.get(resolved[title], {}).get("imageinfo") or [{}]
                    meta = info[0].get("extmetadata", {})
                    if not meta:
                        continue
                    found[(site, name)] = ImageCredit(
                        author=_plain(meta.get("Artist", {}).get("value")),
                        license=_plain(meta.get("LicenseShortName", {}).get("value")),
                        license_url=meta.get("LicenseUrl", {}).get("value") or None,
                    )
    finally:
        if own_client:
            client.close()
    return found


class WikipediaUnavailable(Exception):
    """Wikipedia could not be reached or answered with an error (not: the page does not exist)."""


@dataclass(frozen=True)
class WikipediaArticle:
    lang: str
    title: str
    url: str
    text: str


def is_valid_language(lang: str) -> bool:
    """True for a plain language code such as `pl` or `en` (it becomes part of a hostname)."""
    return bool(_LANG_ONLY.match(lang))


def fetch_article(
    lang: str,
    title: str,
    *,
    intro_only: bool = True,
    client: httpx.Client | None = None,
) -> WikipediaArticle | None:
    """The plain text of one article (its intro, or all of it), looked up by exact title.

    Returns None if the page does not exist or is a disambiguation page; raises
    WikipediaUnavailable if Wikipedia cannot be reached.
    """
    title = title.strip()
    if not is_valid_language(lang) or not title or "|" in title or len(title) > 300:
        return None
    extra = {
        "prop": "extracts|info|pageprops",
        "explaintext": "1",
        "inprop": "url",
        "ppprop": "disambiguation",
    }
    if intro_only:
        extra["exintro"] = "1"
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resolved, pages = _query(lang, [title], extra, client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise WikipediaUnavailable(str(exc)) from exc
    finally:
        if own_client:
            client.close()
    page = pages.get(resolved[title])
    if not page or page.get("missing") or "disambiguation" in page.get("pageprops", {}):
        return None
    text = (page.get("extract") or "").strip()
    if not text:
        return None
    return WikipediaArticle(lang=lang, title=page["title"], url=page["fullurl"], text=text)


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
