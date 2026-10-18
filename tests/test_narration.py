"""Narration: speech cleaning, segments, caching, the Wikipedia article fetch, the endpoint."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, get_narration_service
from app.services.narration import (
    REFERENCE_CHARS,
    NarrationService,
    speakable,
    split_segments,
    split_sentences,
)
from osm.wikipedia import WikipediaArticle, WikipediaUnavailable, fetch_article

PALACE = (
    "Wilanów Palace (Polish: Pałac w Wilanowie, Polish pronunciation: [ˈpawad͡z v vilaˈnɔvjɛ]) is "
    "a former royal palace in Warsaw, Poland.[1] It was built between 1677 and 1696 for John III "
    "Sobieski (1629–1696)."
)


# ---- speakable


def test_pronunciation_and_foreign_names_are_removed() -> None:
    assert speakable(PALACE) == (
        "Wilanów Palace is a former royal palace in Warsaw, Poland. It was built between 1677 and "
        "1696 for John III Sobieski (1629–1696)."
    )


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Zamek (ang. Royal Castle) stoi tu.", "Zamek stoi tu."),
        ("Zamek (łac. Castrum) stoi tu.", "Zamek stoi tu."),
        ("Zamek (wymowa: [ˈzamɛk]) stoi tu.", "Zamek stoi tu."),
        ("The palace[2][note 1] stands here[citation needed].", "The palace stands here."),
        ("Built in (1548) by Venetians.", "Built in (1548) by Venetians."),  # dates stay
        ("A  spaced   text , with  oddities .", "A spaced text, with oddities."),
    ],
)
def test_speakable_cases(raw: str, clean: str) -> None:
    assert speakable(raw) == clean


def test_paragraph_breaks_are_kept() -> None:
    assert speakable("One.\n\n  Two.  \n") == "One.\nTwo."


# ---- sentences and segments


def test_abbreviations_and_initials_do_not_end_a_sentence() -> None:
    text = "St. Mary's chapel dates from c. 1700. Dr. J. Kowalski restored it. It is open daily."
    assert split_sentences(text) == [
        "St. Mary's chapel dates from c. 1700.",
        "Dr. J. Kowalski restored it.",
        "It is open daily.",
    ]


def test_polish_abbreviations() -> None:
    text = "Mieści się przy ul. Prostej 1 w Warszawie. Zbudowano go w XVII w. Jest otwarty."
    assert split_sentences(text)[0] == "Mieści się przy ul. Prostej 1 w Warszawie."
    assert len(split_sentences(text)) <= 3


def test_segments_hold_whole_sentences_within_the_limit() -> None:
    sentences = [f"This is sentence number {i} of the text." for i in range(12)]
    segments = split_segments(" ".join(sentences), max_chars=100)
    assert all(len(s) <= 100 for s in segments) and len(segments) > 1
    assert " ".join(segments) == " ".join(sentences)  # nothing lost or reordered
    assert all(s.endswith(".") for s in segments)  # never cut mid-sentence


def test_segments_do_not_cross_paragraphs() -> None:
    assert split_segments("Short one.\nShort two.") == ["Short one.", "Short two."]


def test_a_very_long_sentence_is_cut_at_a_comma_or_semicolon() -> None:
    long = "The palace has " + ", ".join(f"a very large room number {i}" for i in range(30)) + "."
    segments = split_segments(long)
    assert len(segments) > 1 and all(len(s) < 400 for s in segments)


def test_empty_text_has_no_segments() -> None:
    assert split_segments("") == []


# ---- the service: caching


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    def __init__(self, text: str = "First sentence. Second sentence.") -> None:
        self.text = text
        self.calls: list[tuple[str, str, bool]] = []
        self.fail = False

    def __call__(
        self, lang: str, title: str, *, intro_only: bool = True
    ) -> WikipediaArticle | None:
        self.calls.append((lang, title, intro_only))
        if self.fail:
            raise WikipediaUnavailable("down")
        if title == "Missing":
            return None
        return WikipediaArticle(
            lang, title, f"https://{lang}.wikipedia.org/wiki/{title}", self.text
        )


def test_a_narration_is_built_once_and_then_cached() -> None:
    fetch = Fetcher()
    service = NarrationService(fetch=fetch, clock=Clock())
    first = service.narration("en", "Barbican")
    again = service.narration("en", " barbican ")  # same place, other spelling of the key
    assert first is again and first is not None
    assert first.segments == ["First sentence. Second sentence."]
    assert (
        first.attribution.startswith("Text from Wikipedia (en)") and "CC BY-SA" in first.attribution
    )
    assert fetch.calls == [("en", "Barbican", True)]


def test_the_cache_expires() -> None:
    fetch, clock = Fetcher(), Clock()
    service = NarrationService(fetch=fetch, clock=clock, ttl=60)
    service.narration("en", "Barbican")
    clock.now += 61
    service.narration("en", "Barbican")
    assert len(fetch.calls) == 2


def test_a_missing_article_has_no_narration_and_is_not_cached() -> None:
    fetch = Fetcher()
    service = NarrationService(fetch=fetch, clock=Clock())
    assert service.narration("en", "Missing") is None
    assert service.narration("en", "Missing") is None
    assert len(fetch.calls) == 2


def test_wikipedia_errors_propagate_from_the_narration() -> None:
    fetch = Fetcher()
    fetch.fail = True
    with pytest.raises(WikipediaUnavailable):
        NarrationService(fetch=fetch, clock=Clock()).narration("en", "Barbican")


def test_reference_is_the_whole_article_cut_at_a_paragraph() -> None:
    paragraph = "A sentence in the article. " * 40
    fetch = Fetcher("\n".join([paragraph.strip()] * 30))
    reference = NarrationService(fetch=fetch, clock=Clock()).reference("en", "Barbican")
    assert fetch.calls == [("en", "Barbican", False)]
    assert 0 < len(reference) <= REFERENCE_CHARS and reference.endswith("article.")


def test_a_failed_reference_is_empty_and_retried_next_time() -> None:
    fetch = Fetcher()
    fetch.fail = True
    service = NarrationService(fetch=fetch, clock=Clock())
    assert service.reference("en", "Barbican") == ""
    fetch.fail = False
    assert service.reference("en", "Barbican") != ""


# ---- fetch_article against a fake MediaWiki


def wiki_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def page_response(**page: Any) -> httpx.Response:
    return httpx.Response(200, json={"query": {"pages": [page]}})


def test_fetch_article_asks_for_the_intro_or_the_whole_article() -> None:
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return page_response(title="Barbican", extract="Text.", fullurl="https://x/Barbican")

    article = fetch_article("en", "Barbican", client=wiki_client(handler))
    fetch_article("en", "Barbican", intro_only=False, client=wiki_client(handler))
    assert article is not None and (article.title, article.url, article.text) == (
        "Barbican",
        "https://x/Barbican",
        "Text.",
    )
    assert "exintro" in seen[0] and "exintro" not in seen[1]
    assert seen[0]["explaintext"] == "1" and "exsentences" not in seen[0]  # not cut to 3 sentences


@pytest.mark.parametrize(
    "response",
    [
        page_response(title="X", missing=True),
        page_response(title="X", extract="Many.", fullurl="u", pageprops={"disambiguation": ""}),
        page_response(title="X", extract="  ", fullurl="u"),
    ],
)
def test_fetch_article_returns_none_for_missing_disambiguation_and_empty(
    response: httpx.Response,
) -> None:
    assert fetch_article("en", "X", client=wiki_client(lambda r: response)) is None


def test_fetch_article_rejects_bad_input_without_a_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    client = wiki_client(handler)
    for lang, title in [("evil.com", "X"), ("en", ""), ("en", "A|B"), ("EN", "X")]:
        assert fetch_article(lang, title, client=client) is None


def test_fetch_article_reports_wikipedia_being_down() -> None:
    with pytest.raises(WikipediaUnavailable):
        fetch_article("en", "X", client=wiki_client(lambda r: httpx.Response(500)))


# ---- the endpoint


@pytest.fixture
def client() -> Any:
    service = NarrationService(fetch=Fetcher("One. Two."), clock=Clock())
    app.dependency_overrides[get_narration_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_narration_endpoint(client: TestClient) -> None:
    body = client.get("/api/v1/narration", params={"lang": "pl", "title": "Zamek"}).json()
    assert body["title"] == "Zamek" and body["lang"] == "pl"
    assert body["segments"] == [{"id": 0, "text": "One. Two."}]
    assert "CC BY-SA" in body["attribution"] and body["url"].endswith("/Zamek")


def test_narration_endpoint_errors(client: TestClient) -> None:
    assert (
        client.get("/api/v1/narration", params={"lang": "pl", "title": "Missing"}).status_code
        == 404
    )
    assert (
        client.get("/api/v1/narration", params={"lang": "evil.com", "title": "x"}).status_code
        == 422
    )
    assert client.get("/api/v1/narration", params={"lang": "pl"}).status_code == 422


def test_narration_endpoint_reports_wikipedia_down() -> None:
    fetch = Fetcher()
    fetch.fail = True
    service = NarrationService(fetch=fetch, clock=Clock())
    app.dependency_overrides[get_narration_service] = lambda: service
    try:
        response = TestClient(app).get("/api/v1/narration", params={"lang": "en", "title": "X"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 502
