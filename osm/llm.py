import logging
import os
from typing import Any, cast

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI

logger = logging.getLogger(__name__)
# Any OpenAI-compatible chat endpoint works: set LLM_BASE_URL and LLM_API_KEY (or GEMINI_API_KEY).
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
# Same variable and default as app.config.Settings.request_timeout_seconds. Without a timeout the
# SDK waits 600 s and retries twice, which could block a chat turn for about half an hour.
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_RETRIES = 1


def model_name() -> str:
    """The model that writes the Overpass queries (`QUERY_MODEL`)."""
    return os.environ.get("QUERY_MODEL", DEFAULT_MODEL)


def make_clients() -> list[OpenAI]:
    load_dotenv()
    key = os.environ.get("LLM_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("LLM_API_KEY (or GEMINI_API_KEY) is not set (expected in .env)")
    timeout = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    base_url = os.environ.get("LLM_BASE_URL", GEMINI_BASE_URL)
    return [OpenAI(base_url=base_url, api_key=key, timeout=timeout, max_retries=MAX_RETRIES)]


class FallbackClient:
    """Minimal chat-completions wrapper that moves to the next client on 401/403/429."""

    def __init__(self, clients: list[OpenAI]):
        self._clients = clients

    def complete(self, messages: list[dict[str, Any]]) -> str:
        last: Exception | None = None
        for client in self._clients:
            try:
                response = client.chat.completions.create(
                    model=model_name(), messages=cast(Any, messages), temperature=0
                )
                text = response.choices[0].message.content or ""
                logger.info("OSM query LLM response (model=%s): %r", model_name(), text)
                return text
            except APIStatusError as exc:
                if exc.status_code not in (401, 403, 429):
                    raise
                last = exc
        assert last is not None
        raise last
