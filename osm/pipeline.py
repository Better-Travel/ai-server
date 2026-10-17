import logging
import re
from collections.abc import Callable
from typing import Any

from .prompt import NO_QUERY_PREFIX, build_system_prompt, build_user_prompt
from .runner import QuerySyntaxError, run_query
from .schemas import OverpassElement, PipelineResult

logger = logging.getLogger(__name__)
Message = dict[str, Any]
Completer = Callable[[list[Message]], str]
Runner = Callable[[str], list[OverpassElement]]

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")


class NeedsMoreInfo(Exception):
    """The request doesn't say enough (e.g. no destination or country) to build a query."""


class QueryGenerationFailed(Exception):
    def __init__(self, query: str, error: str, attempts: int):
        super().__init__(f"no valid query after {attempts} attempts; last error: {error}")
        self.query = query
        self.error = error
        self.attempts = attempts


def clean_query(text: str) -> str:
    return _FENCE.sub("", text.strip()).strip()


def generate_and_run(
    request: str,
    complete: Completer,
    run: Runner = run_query,
    max_fixes: int = 3,
) -> PipelineResult:
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(request)},
    ]
    attempts = 0
    while True:
        attempts += 1
        query = clean_query(complete(messages))
        logger.info("Overpass query attempt %d: %s", attempts, query)
        if query.startswith(NO_QUERY_PREFIX):
            raise NeedsMoreInfo(query[len(NO_QUERY_PREFIX) :].strip())
        try:
            if not query.startswith("["):
                raise QuerySyntaxError("The reply was not an Overpass QL query.")
            return PipelineResult(query=query, results=run(query), attempts=attempts)
        except QuerySyntaxError as exc:
            logger.warning("Overpass rejected query (attempt %d): %s", attempts, exc)
            if attempts > max_fixes:
                raise QueryGenerationFailed(query, str(exc), attempts) from exc
            messages += [
                {"role": "assistant", "content": query},
                {
                    "role": "user",
                    "content": (
                        f"Overpass rejected this query:\n{exc}\nReturn a corrected query only."
                    ),
                },
            ]
