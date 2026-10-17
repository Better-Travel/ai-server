"""Wikipedia lookups by exact title, in Polish and English (offline, httpx.MockTransport)."""

from types import SimpleNamespace
from typing import Any

import httpx

from app.services.place_finder import PlaceFinder
from osm.schemas import OverpassElement
from osm.wikipedia import enrich, fetch_summaries, parse_wikipedia_tag

REQUEST = "Visiting Warszawa, Poland: sights and cafes."


class FakeWikipedia:
    """Fake MediaWiki API. `articles[(lang, title)]` is the extract text (or a page dict);
    `links[(lang, title, target)]` is the title of the same article in `target`."""

    def __init__(self) -> None:
        self.articles: dict[tuple[str, str], Any] = {}
        self.links: dict[tuple[str, str, str], str] = {}
        self.normalized: dict[str, str] = {}
        self.redirects: dict[str, str] = {}
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        lang = request.url.host.split(".")[0]
        params = request.url.params
        requested = params["titles"].split("|")
        pages, normalized, redirects = [], [], []
        for title in requested:
            norm = self.normalized.get(title, title)
            if norm != title:
                normalized.append({"from": title, "to": norm})
            final = self.redirects.get(norm, norm)
            if final != norm:
                redirects.append({"from": norm, "to": final})
            article = self.articles.get((lang, final))
            if article is None:
                pages.append({"title": final, "missing": True})
            elif params["prop"] == "langlinks":
                page: dict[str, Any] = {"title": final}
                target = self.links.get((lang, final, params["lllang"]))
                if target:
                    page["langlinks"] = [{"lang": params["lllang"], "title": target}]
                pages.append(page)
            elif isinstance(article, dict):
                pages.append({"title": final, "fullurl": f"https://{lang}/{final}", **article})
            else:
                pages.append(
                    {"title": final, "fullurl": f"https://{lang}/{final}", "extract": article}
                )
        query = {"pages": pages, "normalized": normalized, "redirects": redirects}
        return httpx.Response(200, json={"query": query})

    def text_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "extracts" in r.url.params["prop"]]


def test_parse_wikipedia_tag() -> None:
    assert parse_wikipedia_tag("pl:Zamek Królewski w Warszawie") == (
        "pl",
        "Zamek Królewski w Warszawie",
    )
    assert parse_wikipedia_tag("en:Palace#History") == ("en", "Palace")
    assert parse_wikipedia_tag("simple:Warsaw") == ("simple", "Warsaw")
    assert parse_wikipedia_tag("https://en.wikipedia.org/wiki/Palace") is None
    assert parse_wikipedia_tag("Palace of Culture") is None
    assert parse_wikipedia_tag("evil.com/x:Title") is None
    assert parse_wikipedia_tag("") is None


def test_polish_article_gets_english_via_language_link() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("pl", "Zamek")] = "Polski opis."
    wiki.articles[("en", "Castle")] = "English intro."
    wiki.links[("pl", "Zamek", "en")] = "Castle"

    info = fetch_summaries([("pl", "Zamek")], wiki.client())[("pl", "Zamek")]

    assert info.pl is not None and info.pl.extract == "Polski opis."
    assert info.en is not None and info.en.extract == "English intro."
    assert info.en.title == "Castle"


def test_english_article_gets_polish_via_language_link() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("en", "Castle")] = "English intro."
    wiki.articles[("pl", "Zamek")] = "Polski opis."
    wiki.links[("en", "Castle", "pl")] = "Zamek"

    info = fetch_summaries([("en", "Castle")], wiki.client())[("en", "Castle")]

    assert info.pl is not None and info.pl.extract == "Polski opis."
    assert info.en is not None and info.en.extract == "English intro."


def test_other_language_gets_both_translations() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("de", "Schloss")] = "Deutsch."
    wiki.articles[("pl", "Zamek")] = "Polski."
    wiki.articles[("en", "Castle")] = "English."
    wiki.links[("de", "Schloss", "pl")] = "Zamek"
    wiki.links[("de", "Schloss", "en")] = "Castle"

    info = fetch_summaries([("de", "Schloss")], wiki.client())[("de", "Schloss")]

    assert info.pl is not None and info.pl.extract == "Polski."
    assert info.en is not None and info.en.extract == "English."


def test_missing_translation_leaves_that_language_empty() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("pl", "Zamek")] = "Polski opis."

    info = fetch_summaries([("pl", "Zamek")], wiki.client())[("pl", "Zamek")]

    assert info.pl is not None and info.en is None


def test_titles_are_grouped_per_language_and_chunked() -> None:
    wiki = FakeWikipedia()
    for i in range(45):
        wiki.articles[("pl", f"T{i}")] = "x"
    wiki.articles[("en", "C")] = "y"
    refs = [("pl", f"T{i}") for i in range(45)] + [("en", "C"), ("pl", "T0")]

    fetch_summaries(refs, wiki.client())

    pl = [
        len(r.url.params["titles"].split("|"))
        for r in wiki.text_requests()
        if r.url.host == "pl.wikipedia.org"
    ]
    assert pl == [20, 20, 5]  # deduplicated, 20 per request
    assert any(r.url.host == "en.wikipedia.org" for r in wiki.text_requests())
    assert all("user-agent" in r.headers for r in wiki.requests)


def test_normalized_and_redirected_titles_map_back_to_requested() -> None:
    wiki = FakeWikipedia()
    wiki.normalized["old castle"] = "Old castle"
    wiki.redirects["Old castle"] = "Royal Castle"
    wiki.articles[("en", "Royal Castle")] = "A castle."

    info = fetch_summaries([("en", "old castle")], wiki.client())[("en", "old castle")]

    assert info.en is not None and info.en.title == "Royal Castle"


def test_missing_and_disambiguation_pages_are_skipped() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("en", "Many")] = {"extract": "x", "pageprops": {"disambiguation": ""}}
    wiki.articles[("en", "Good")] = "Fine."

    out = fetch_summaries([("en", "Nope"), ("en", "Many"), ("en", "Good")], wiki.client())

    assert set(out) == {("en", "Good")}


def test_failure_in_one_language_does_not_affect_others() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("en", "C")] = "y"
    inner = wiki.client()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pl.wikipedia.org":
            return httpx.Response(500)
        return inner.send(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = fetch_summaries([("pl", "A"), ("en", "C")], client)

    assert set(out) == {("en", "C")}


def test_transport_error_is_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_summaries([("pl", "A")], client) == {}


def test_enrich_only_touches_tagged_elements() -> None:
    wiki = FakeWikipedia()
    wiki.articles[("en", "A")] = "About A."
    tagged = OverpassElement(id=1, type="node", name="A", tags={"wikipedia": "en:A"})
    plain = OverpassElement(id=2, type="node", name="B", tags={"name": "B"})
    bare_url = OverpassElement(
        id=3, type="node", tags={"wikipedia": "https://en.wikipedia.org/wiki/C"}
    )

    out = enrich([tagged, plain, bare_url], wiki.client())

    assert out[0].wikipedia is not None and out[0].wikipedia.en is not None
    assert out[1].wikipedia is None and out[2].wikipedia is None


def test_enrich_makes_no_request_without_tags() -> None:
    wiki = FakeWikipedia()
    element = OverpassElement(id=1, type="node", tags={"name": "B"})
    assert enrich([element], wiki.client()) == [element]
    assert wiki.requests == []


def test_place_finder_returns_both_languages_trimmed(monkeypatch: Any) -> None:
    wiki = FakeWikipedia()
    wiki.articles[("pl", "Zamek")] = "słowo " * 200
    wiki.articles[("en", "Castle")] = "English intro."
    wiki.links[("pl", "Zamek", "en")] = "Castle"
    element = OverpassElement(
        id=1, type="way", lat=52.2, lon=21.0, name="Zamek", tags={"wikipedia": "pl:Zamek"}
    )
    monkeypatch.setattr(
        "app.services.place_finder.generate_and_run",
        lambda request, complete: SimpleNamespace(results=[element]),
    )
    monkeypatch.setattr("app.services.place_finder.enrich", lambda els: enrich(els, wiki.client()))

    place = PlaceFinder(lambda: lambda m: "q")(REQUEST).places[0].model_dump(exclude_none=True)

    assert set(place["wikipedia"]) == {"pl", "en"}
    assert place["wikipedia"]["en"]["extract"] == "English intro."
    assert place["wikipedia"]["pl"]["extract"].endswith("…")
    assert len(place["wikipedia"]["pl"]["extract"]) <= 301
    assert "wikipedia" not in place["tags"]  # raw tag is not passed on


def test_429_is_retried_with_retry_after(monkeypatch: Any) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("osm.wikipedia.time.sleep", sleeps.append)
    wiki = FakeWikipedia()
    wiki.articles[("en", "A")] = "About A."
    inner = wiki.client()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "3"})
        return inner.send(request)

    out = fetch_summaries([("en", "A")], httpx.Client(transport=httpx.MockTransport(handler)))

    assert ("en", "A") in out
    assert sleeps == [3.0]


def test_persistent_429_gives_up_without_raising(monkeypatch: Any) -> None:
    monkeypatch.setattr("osm.wikipedia.time.sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    assert fetch_summaries([("en", "A")], client) == {}
