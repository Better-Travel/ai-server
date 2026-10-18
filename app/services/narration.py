"""Narration for a sight: Wikipedia text cleaned for speech and split into numbered segments.

The server only produces the text; the client reads it aloud and keeps the cursor (see
`GuideService` for questions asked while a segment is being spoken).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from osm.wikipedia import WikipediaArticle, WikipediaUnavailable, fetch_article

SEGMENT_CHARS = 250  # a segment holds whole sentences up to about this length
LONG_SENTENCE_CHARS = 350  # longer sentences are cut at a semicolon or comma
REFERENCE_CHARS = 12_000  # how much of the whole article the guide may read when answering
CACHE_SECONDS = 24 * 3600

# Pronunciation guides look like [ˈpawad͡z v vilaˈnɔvjɛ]: IPA letters, stress marks and ties.
_IPA_BRACKETS = re.compile(r"\[[^\]]*[ɐ-˿͡][^\]]*\]")
_CITATIONS = re.compile(r"\[(?:\d+|[a-z]|citation needed|note \d+|przypis \d+)\]")
# Parentheses that only give foreign names or pronunciation: "(Polish: Pałac w Wilanowie)".
_FOREIGN_PAREN = re.compile(
    r"^\(\s*(?:"
    r"[A-Z][a-z]+(?: [A-Z][a-z]+)?:"  # "Polish:", "Old Polish:"
    r"|(?:ang|łac|niem|fr|ros|ukr|jid|hebr|wymowa)[.:]"  # Polish language abbreviations
    r"|.*(?:pronunciation|IPA|wymowa)"
    r")",
    re.IGNORECASE,
)
_INNER_PAREN = re.compile(r"\([^()]*\)")
# A full stop after one of these does not end a sentence.
_ABBREVIATIONS = frozenset(
    {
        # English
        *("st", "mr", "mrs", "ms", "dr", "prof", "no", "vs", "ca", "c", "cf", "e.g", "i.e"),
        # Polish
        *("ul", "al", "pl", "os", "np", "tzw", "ok", "im", "ks", "gen", "płk", "ppłk", "inż"),
        *("mgr", "hab", "św", "ww", "w", "r", "n.e", "p.n.e", "m.in", "tys", "mln", "mld"),
        *("zm", "wyd", "nr"),
    }
)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[\"„(\[]?[A-ZĄĆĘŁŃÓŚŹŻ0-9])")


def _drop_parenthetical_names(text: str) -> str:
    """Remove (innermost first) parentheses that only carry foreign names or pronunciation."""
    previous = None
    while previous != text:
        previous = text
        text = _INNER_PAREN.sub(
            lambda m: "" if _FOREIGN_PAREN.match(m.group(0)) or m.group(0) == "()" else m.group(0),
            text,
        )
    return text


def speakable(text: str) -> str:
    """Text that reads well aloud: no pronunciation guides, citation marks or odd spacing.

    Dates and other plain parentheses are kept. Paragraph breaks (newlines) are kept.
    """
    text = _IPA_BRACKETS.sub("", text)
    text = _CITATIONS.sub("", text)
    # An IPA bracket inside a parenthesis leaves "(Polish: Pałac, Polish pronunciation: )".
    text = _drop_parenthetical_names(text)
    paragraphs = []
    for paragraph in text.splitlines():
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        paragraph = re.sub(r"\s+([,.;:!?])", r"\1", paragraph)
        paragraph = re.sub(r"\(\s*\)", "", paragraph)
        paragraph = re.sub(r"\s{2,}", " ", paragraph).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return "\n".join(paragraphs)


def _ends_with_abbreviation(piece: str) -> bool:
    last = piece.rstrip().rsplit(" ", 1)[-1].rstrip(".").lower().strip('"„(')
    return last in _ABBREVIATIONS or (
        len(last) == 1 and last.isalpha() and piece.rstrip()[-1] == "."
    )


def split_sentences(paragraph: str) -> list[str]:
    """Sentences of one paragraph; a full stop after an abbreviation or an initial doesn't split."""
    sentences: list[str] = []
    for piece in _SENTENCE_END.split(paragraph):
        if sentences and _ends_with_abbreviation(sentences[-1]):
            sentences[-1] = f"{sentences[-1]} {piece}"
        else:
            sentences.append(piece)
    return [s.strip() for s in sentences if s.strip()]


def _cut_long(sentence: str) -> list[str]:
    """Cut a very long sentence at the last semicolon or comma before the size limit."""
    parts: list[str] = []
    while len(sentence) > LONG_SENTENCE_CHARS:
        window = sentence[:SEGMENT_CHARS]
        cut = max(window.rfind("; "), window.rfind(", "))
        if cut < SEGMENT_CHARS // 3:
            break
        parts.append(sentence[: cut + 1].strip())
        sentence = sentence[cut + 1 :].strip()
    return [*parts, sentence]


def split_segments(text: str, max_chars: int = SEGMENT_CHARS) -> list[str]:
    """Group the sentences of `text` into segments of whole sentences, within each paragraph."""
    segments: list[str] = []
    for paragraph in text.splitlines():
        current = ""
        for sentence in (part for s in split_sentences(paragraph) for part in _cut_long(s)):
            if current and len(current) + 1 + len(sentence) > max_chars:
                segments.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            segments.append(current)
    return segments


def attribution(lang: str, url: str) -> str:
    """The credit Wikipedia's licence asks for; the client should show or speak it."""
    return f"Text from Wikipedia ({lang}), licensed CC BY-SA 4.0: {url}"


@dataclass(frozen=True)
class Narration:
    lang: str
    title: str
    url: str
    segments: list[str]
    attribution: str = field(default="")


class NarrationService:
    """Builds narrations (and reference text for questions) from Wikipedia, cached for a day."""

    def __init__(
        self,
        fetch: Callable[..., WikipediaArticle | None] = fetch_article,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_SECONDS,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._ttl = ttl
        self._narrations: dict[tuple[str, str], tuple[float, Narration]] = {}
        self._references: dict[tuple[str, str], tuple[float, str]] = {}

    def narration(self, lang: str, title: str) -> Narration | None:
        """The narration for an article's intro; None if the article does not exist.

        Raises WikipediaUnavailable if Wikipedia cannot be reached.
        """
        key = (lang, title.strip().casefold())
        cached = self._narrations.get(key)
        if cached and cached[0] > self._clock():
            return cached[1]
        article = self._fetch(lang, title, intro_only=True)
        if article is None:
            return None
        segments = split_segments(speakable(article.text))
        if not segments:
            return None
        narration = Narration(
            lang=lang,
            title=article.title,
            url=article.url,
            segments=segments,
            attribution=attribution(lang, article.url),
        )
        self._narrations[key] = (self._clock() + self._ttl, narration)
        return narration

    def reference(self, lang: str, title: str) -> str:
        """The whole article as plain text (cut to REFERENCE_CHARS); empty if unavailable."""
        key = (lang, title.strip().casefold())
        cached = self._references.get(key)
        if cached and cached[0] > self._clock():
            return cached[1]
        try:
            article = self._fetch(lang, title, intro_only=False)
        except WikipediaUnavailable:
            return ""  # not cached: try again next time; the narration itself is enough to answer
        text = _cut_at_paragraph(speakable(article.text), REFERENCE_CHARS) if article else ""
        self._references[key] = (self._clock() + self._ttl, text)
        return text


def _cut_at_paragraph(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip()
