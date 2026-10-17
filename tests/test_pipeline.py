import pytest

from osm.pipeline import Message, QueryGenerationFailed, clean_query, generate_and_run
from osm.runner import QuerySyntaxError
from osm.schemas import OverpassElement, PlacesInput

PLACES = PlacesInput.model_validate(
    {
        "places": [
            {
                "category": "food_drink",
                "filters": [{"key": "amenity", "values": ["cafe"]}],
                "area": "Paris",
                "country": "FR",
            }
        ]
    }
)


def test_clean_query_strips_fences() -> None:
    assert clean_query("```overpassql\nout;\n```") == "out;"


def test_error_is_fed_back_and_second_attempt_succeeds() -> None:
    replies = iter(["bad query", "good query"])
    seen: list[list[Message]] = []

    def complete(messages: list[Message]) -> str:
        seen.append([dict(m) for m in messages])
        return next(replies)

    def run(query: str) -> list[OverpassElement]:
        if query == "bad query":
            raise QuerySyntaxError("line 1: parse error")
        return []

    result = generate_and_run(PLACES, complete, run)
    assert (result.query, result.attempts) == ("good query", 2)
    feedback = "Overpass rejected this query:\nline 1: parse error\nReturn a corrected query only."
    assert seen[1][-2:] == [
        {"role": "assistant", "content": "bad query"},
        {"role": "user", "content": feedback},
    ]


def test_gives_up_after_max_fixes() -> None:
    def run(query: str) -> list[OverpassElement]:
        raise QuerySyntaxError("nope")

    with pytest.raises(QueryGenerationFailed) as info:
        generate_and_run(PLACES, lambda m: "q", run, max_fixes=2)
    assert info.value.attempts == 3
