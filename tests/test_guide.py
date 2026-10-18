"""The sight guide: questions asked during a narration, resume points, web search (no network)."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_guide_service
from app.schemas import Message
from app.services.guide_service import (
    GuideService,
    InvalidCursor,
    PlaceNotFound,
    bridge_for,
    plain_speech,
)
from app.services.llm_loop import BettyError
from app.services.narration import NarrationService
from app.services.web_search import SearchError, WebResult
from osm.wikipedia import WikipediaArticle
from tests.test_search_places import Script, text

SEGMENTS = [f"Segment number {i} about the Barbican." for i in range(6)]
INTRO = "\n".join(SEGMENTS)  # one segment per paragraph
ARTICLE = "FULL ARTICLE: the Barbican was built in 1548 by Giovanni Battista the Venetian."


def fake_fetch(lang: str, title: str, *, intro_only: bool = True) -> WikipediaArticle | None:
    if title == "Missing":
        return None
    return WikipediaArticle(lang, title, "https://x/" + title, INTRO if intro_only else ARTICLE)


def search_call(*queries: str) -> SimpleNamespace:
    calls = [
        SimpleNamespace(
            id=f"s{i}",
            function=SimpleNamespace(name="search_web", arguments=json.dumps({"query": q})),
        )
        for i, q in enumerate(queries)
    ]
    return SimpleNamespace(content=None, tool_calls=calls)


class FakeSearch:
    def __init__(self, results: list[WebResult] | Exception | None = None) -> None:
        self.results = (
            results
            if results is not None
            else [WebResult("Official site", "https://zamek.pl", "Open daily 10-18.")]
        )
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[WebResult]:
        self.queries.append(query)
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


def make_guide(script: Script, searcher: FakeSearch | None = None, **settings: Any) -> GuideService:
    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=script))
    config = Settings(nvidia_api_key_2="x", _env_file=None, **settings)
    return GuideService(config, NarrationService(fetch=fake_fetch), client, searcher)


def ask(guide: GuideService, cursor: int = 2, question: str = "Who built it?", **kw: Any) -> Any:
    return guide.ask("en", "Barbican", cursor, question, **kw)


def prompt_of(script: Script) -> str:
    return str(script.calls[0]["messages"][-1]["content"])


# ---- context, resume, bridge


def test_the_guide_sees_what_was_just_said_and_where_it_was_interrupted() -> None:
    script = Script(text("It was built in 1548."))
    result = ask(make_guide(script), cursor=4)
    prompt = prompt_of(script)
    assert "- Segment number 2" in prompt and "- Segment number 3" in prompt
    assert "- Segment number 4 about the Barbican.  <-- you were interrupted here" in prompt
    assert "Segment number 1" not in prompt  # only the last three
    assert "Segment number 5" not in prompt  # nothing after the cursor is revealed as spoken
    assert ARTICLE in prompt and "Traveller's question: Who built it?" in prompt
    assert result.answer == "It was built in 1548."


def test_resume_is_the_interrupted_segment_with_a_bridge() -> None:
    result = ask(make_guide(Script(text("Fine."))), cursor=3)
    assert (result.resume_from, result.finished) == (3, False)
    assert result.bridge == "Back to Barbican."


def test_an_interruption_in_the_first_segment() -> None:
    script = Script(text("Fine."))
    result = ask(make_guide(script), cursor=0)
    assert result.resume_from == 0
    assert prompt_of(script).count("- Segment number") == 1


def test_questions_after_the_narration_has_finished() -> None:
    script = Script(text("Fine."))
    result = ask(make_guide(script), cursor=len(SEGMENTS))
    assert (result.resume_from, result.bridge, result.finished) == (None, "", True)
    prompt = prompt_of(script)
    assert "already finished" in prompt and "interrupted here" not in prompt
    assert "- Segment number 5" in prompt


def test_the_cursor_cannot_be_beyond_the_end() -> None:
    with pytest.raises(InvalidCursor):
        ask(make_guide(Script()), cursor=len(SEGMENTS) + 1)


def test_an_unknown_place_is_reported() -> None:
    with pytest.raises(PlaceNotFound):
        make_guide(Script()).ask("en", "Missing", 0, "Hello?")


def test_earlier_questions_come_first() -> None:
    script = Script(text("Fine."))
    history = [Message(role="user", content="Q1"), Message(role="assistant", content="A1")]
    ask(make_guide(script), history=history)
    sent = script.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "Q1"


def test_bridge_follows_the_narration_language() -> None:
    assert bridge_for("pl", "Barbakan") == "Wracając do tematu: Barbakan."
    assert bridge_for("en", "Barbican") == "Back to Barbican."
    assert bridge_for("de", "Barbakan") == ""


# ---- the answer is spoken text


def test_answers_are_plain_speech() -> None:
    raw = "**Built** in 1548.\n\n- by [Giovanni](https://x.org)\n- see https://y.org now\n# Done"
    assert plain_speech(raw) == "Built in 1548. by Giovanni see now Done"


def test_the_model_used_defaults_to_betty_s_and_can_be_overridden() -> None:
    script = Script(text("A."), text("B."))
    ask(make_guide(script))
    ask(make_guide(script, guide_model="other/model"))
    assert [c["model"] for c in script.calls] == ["nemotron-3.5", "other/model"]


# ---- web search


def test_without_a_search_backend_the_tool_is_not_offered() -> None:
    script = Script(text("I do not know."))
    guide = make_guide(script)
    result = ask(guide)
    assert not guide.can_search_web and "tools" not in script.calls[0]
    assert (result.used_web, result.sources) == (False, [])


def test_the_guide_can_search_the_web_and_returns_its_sources() -> None:
    searcher = FakeSearch()
    script = Script(search_call("Barbican Warsaw opening hours"), text("It opens at ten."))
    result = ask(make_guide(script, searcher), question="When does it open?")

    assert searcher.queries == ["Barbican Warsaw opening hours"]
    assert [t["function"]["name"] for t in script.calls[0]["tools"]] == ["search_web"]
    assert result.used_web and [(s.title, s.url) for s in result.sources] == [
        ("Official site", "https://zamek.pl")
    ]
    tool_result = json.loads(script.calls[1]["messages"][-1]["content"])
    assert "untrusted" in tool_result["note"]
    assert tool_result["results"][0] == {
        "title": "Official site",
        "url": "https://zamek.pl",
        "text": "Open daily 10-18.",
    }


def test_searches_are_limited_even_when_asked_in_parallel() -> None:
    searcher = FakeSearch()
    script = Script(search_call("one", "two", "three"), text("Done."))
    ask(make_guide(script, searcher, guide_max_searches=2))
    assert searcher.queries == ["one", "two"]
    last = [m for m in script.calls[1]["messages"] if m["role"] == "tool"][-1]
    assert "no more searches" in json.loads(last["content"])["error"]


def test_a_failed_search_does_not_break_the_answer() -> None:
    searcher = FakeSearch(SearchError("quota"))
    script = Script(search_call("q"), text("I could not check that."))
    result = ask(make_guide(script, searcher))
    assert result.answer == "I could not check that." and not result.used_web
    tool = json.loads(script.calls[1]["messages"][-1]["content"])
    assert "unavailable" in tool["error"] and "quota" not in tool["error"]


def test_bad_search_arguments_are_rejected_without_searching() -> None:
    searcher = FakeSearch()
    bad = SimpleNamespace(id="x", function=SimpleNamespace(name="search_web", arguments="nope"))
    script = Script(SimpleNamespace(content=None, tool_calls=[bad]), text("Fine."))
    ask(make_guide(script, searcher))
    assert searcher.queries == []
    assert "error" in json.loads(script.calls[1]["messages"][-1]["content"])


def test_the_model_failing_is_reported() -> None:
    script = Script(RuntimeError("down"))
    with pytest.raises(BettyError):
        ask(make_guide(script))


# ---- the HTTP endpoint


def body(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "place": {"lang": "en", "title": "Barbican"},
        "cursor": 2,
        "question": "Who built it?",
        "history": [],
    }
    return {**data, **overrides}


def post(guide: GuideService, payload: dict[str, Any]) -> Any:
    app.dependency_overrides[get_guide_service] = lambda: guide
    try:
        return TestClient(app).post("/api/v1/narration/ask", json=payload)
    finally:
        app.dependency_overrides.clear()


def test_ask_endpoint_returns_the_answer_and_where_to_resume() -> None:
    searcher = FakeSearch()
    script = Script(search_call("q"), text("Built in 1548."))
    response = post(make_guide(script, searcher), body())
    assert response.status_code == 200
    assert response.json() == {
        "answer": "Built in 1548.",
        "used_web": True,
        "sources": [{"title": "Official site", "url": "https://zamek.pl"}],
        "resume": {"from_segment": 2, "bridge": "Back to Barbican."},
        "finished": False,
    }


def test_ask_endpoint_after_the_end_has_no_resume() -> None:
    response = post(make_guide(Script(text("Yes."))), body(cursor=len(SEGMENTS)))
    assert response.json()["resume"] is None and response.json()["finished"] is True


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        (body(cursor=99), 422),
        (body(cursor=-1), 422),
        (body(question=""), 422),
        (body(place={"lang": "evil.com", "title": "x"}), 422),
        (body(place={"lang": "en", "title": "Missing"}), 404),
        ({"place": {"lang": "en", "title": "x"}}, 422),
    ],
)
def test_ask_endpoint_errors(payload: dict[str, Any], status: int) -> None:
    assert post(make_guide(Script(text("x"))), payload).status_code == status


def test_ask_endpoint_reports_a_failing_model() -> None:
    assert post(make_guide(Script(RuntimeError("down"))), body()).status_code == 502
