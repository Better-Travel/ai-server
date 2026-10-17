import re
from typing import Callable

from .prompt import build_system_prompt, build_user_prompt
from .runner import QuerySyntaxError, run_query
from .schemas import OverpassElement, PipelineResult, PlacesInput

Completer = Callable[[list[dict]], str]
Runner = Callable[[str], list[OverpassElement]]

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")


class QueryGenerationFailed(Exception):
    def __init__(self, query: str, error: str, attempts: int):
        super().__init__(f"no valid query after {attempts} attempts; last error: {error}")
        self.query = query
        self.error = error
        self.attempts = attempts


def clean_query(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


def generate_and_run(
    places: PlacesInput,
    complete: Completer,
    run: Runner = run_query,
    max_fixes: int = 3,
) -> PipelineResult:
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(places)},
    ]
    attempts = 0
    while True:
        attempts += 1
        query = clean_query(complete(messages))
        try:
            return PipelineResult(query=query, results=run(query), attempts=attempts)
        except QuerySyntaxError as exc:
            if attempts > max_fixes:
                raise QueryGenerationFailed(query, str(exc), attempts) from exc
            messages += [
                {"role": "assistant", "content": query},
                {
                    "role": "user",
                    "content": f"Overpass rejected this query:\n{exc}\nReturn a corrected query only.",
                },
            ]
