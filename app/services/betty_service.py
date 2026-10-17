"""Betty: the trip-planning assistant backed by NVIDIA's OpenAI-compatible API."""

from collections.abc import Sequence

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.config import Settings
from app.prompts import CHAT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from app.schemas import Message


class BettyError(RuntimeError):
    """Raised when Betty cannot produce a response."""


class BettyService:
    """Wraps the OpenAI SDK client to provide chat and summarisation."""

    def __init__(self, settings: Settings, client: OpenAI | None = None) -> None:
        self._settings = settings
        if client is None:
            if not settings.nvidia_api_key_2:
                raise BettyError("NVIDIA_API_KEY_2 is not set.")
            client = OpenAI(
                api_key=settings.nvidia_api_key_2,
                base_url=settings.nvidia_base_url,
                timeout=settings.request_timeout_seconds,
            )
        self._client = client

    def chat(self, messages: Sequence[Message]) -> str:
        """Return Betty's next reply for the given conversation."""
        return self._complete(CHAT_SYSTEM_PROMPT, messages)

    def summarize(self, messages: Sequence[Message]) -> str:
        """Condense the conversation into one structured paragraph."""
        transcript = "\n".join(
            f"{'Traveller' if m.role == 'user' else 'Betty'}: {m.content}" for m in messages
        )
        request = Message(
            role="user",
            content=f"Conversation:\n{transcript}\n\nWrite the trip summary paragraph now.",
        )
        # Collapse any stray line breaks so the output is guaranteed to be one paragraph.
        return " ".join(self._complete(SUMMARY_SYSTEM_PROMPT, [request], temperature=0.2).split())

    def _complete(
        self, system_prompt: str, messages: Sequence[Message], temperature: float | None = None
    ) -> str:
        payload: list[ChatCompletionMessageParam] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role == "user":
                payload.append({"role": "user", "content": m.content})
            else:
                payload.append({"role": "assistant", "content": m.content})
        try:
            response = self._client.chat.completions.create(
                model=self._settings.betty_model,
                messages=payload,
                temperature=self._settings.betty_temperature
                if temperature is None
                else temperature,
                max_tokens=self._settings.betty_max_tokens,
            )
        except Exception as exc:  # openai raises a broad family of API/connection errors
            raise BettyError(f"Upstream model request failed: {exc}") from exc
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            raise BettyError("Model returned an empty response.")
        return content.strip()
