import pytest

from osm.pipeline import (
    Message,
    NeedsMoreInfo,
    QueryGenerationFailed,
    clean_query,
    generate_and_run,
)
from osm.runner import QuerySyntaxError
from osm.schemas import OverpassElement

REQUEST = "Visiting Paris, France: cafes and museums."


def test_clean_query_strips_fences() -> None:
    assert clean_query("```overpassql\nout;\n```") == "out;"


def test_error_is_fed_back_and_second_attempt_succeeds() -> None:
    replies = iter(["[out:json]; bad", "[out:json]; good"])
    seen: list[list[Message]] = []

    def complete(messages: list[Message]) -> str:
        seen.append([dict(m) for m in messages])
        return next(replies)

    def run(query: str) -> list[OverpassElement]:
        if query == "[out:json]; bad":
            raise QuerySyntaxError("line 1: parse error")
        return []

    result = generate_and_run(REQUEST, complete, run)
    assert (result.query, result.attempts) == ("[out:json]; good", 2)
    feedback = "Overpass rejected this query:\nline 1: parse error\nReturn a corrected query only."
    assert seen[1][-2:] == [
        {"role": "assistant", "content": "[out:json]; bad"},
        {"role": "user", "content": feedback},
    ]


def test_gives_up_after_max_fixes() -> None:
    def run(query: str) -> list[OverpassElement]:
        raise QuerySyntaxError("nope")

    with pytest.raises(QueryGenerationFailed) as info:
        generate_and_run(REQUEST, lambda m: "q", run, max_fixes=2)
    assert info.value.attempts == 3


def test_text_reaches_the_model() -> None:
    seen: list[list[Message]] = []

    def complete(messages: list[Message]) -> str:
        seen.append(messages)
        return "[out:json];out;"

    generate_and_run(REQUEST, complete, lambda q: [])
    assert "Visiting Paris, France" in seen[0][1]["content"]


def test_no_query_reply_raises_needs_more_info_without_running() -> None:
    def run(query: str) -> list[OverpassElement]:
        raise AssertionError("must not run")

    with pytest.raises(NeedsMoreInfo, match="country"):
        generate_and_run(REQUEST, lambda m: "NO_QUERY: the country is missing", run)


def test_non_query_reply_is_sent_back_for_correction() -> None:
    replies = iter(["Sure! Here you go", "[out:json];out;"])
    result = generate_and_run(REQUEST, lambda m: next(replies), lambda q: [])
    assert result.attempts == 2
