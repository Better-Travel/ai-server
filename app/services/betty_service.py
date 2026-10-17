"""Betty: the trip-planning assistant backed by NVIDIA's OpenAI-compatible API."""

from collections.abc import Callable, Sequence
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.config import Settings
from app.prompts import CHAT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from app.schemas import Message
from app.services.places_tool import TOOL_NAME, TOOL_SPEC, PlacesTool


class BettyError(RuntimeError):
    """Raised when Betty cannot produce a response."""


class BettyService:
    """Wraps the OpenAI SDK client to provide chat and summarisation."""

    def __init__(
        self,
        settings: Settings,
        client: OpenAI | None = None,
        places_tool: Callable[[str], str] | None = None,
    ) -> None:
        self._settings = settings
        self._places_tool: Callable[[str], str] | None = (
            (places_tool if places_tool is not None else PlacesTool())
            if settings.betty_places_tool
            else None
        )
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
        return self._complete(CHAT_SYSTEM_PROMPT, messages, with_tools=True)

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
        self,
        system_prompt: str,
        messages: Sequence[Message],
        temperature: float | None = None,
        with_tools: bool = False,
    ) -> str:
        payload: list[ChatCompletionMessageParam] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role == "user":
                payload.append({"role": "user", "content": m.content})
            else:
                payload.append({"role": "assistant", "content": m.content})
        use_tools = with_tools and self._places_tool is not None
        for _ in range(self._settings.betty_max_tool_rounds + 1):
            kwargs: dict[str, Any] = {}
            if use_tools:
                kwargs["tools"] = [TOOL_SPEC]
            try:
                response = self._client.chat.completions.create(
                    model=self._settings.betty_model,
                    messages=payload,
                    temperature=self._settings.betty_temperature
                    if temperature is None
                    else temperature,
                    max_tokens=self._settings.betty_max_tokens,
                    **kwargs,
                )
            except Exception as exc:  # openai raises a broad family of API/connection errors
                raise BettyError(f"Upstream model request failed: {exc}") from exc
            message = response.choices[0].message if response.choices else None
            tool_calls = getattr(message, "tool_calls", None) if use_tools else None
            if not tool_calls:
                content = message.content if message else None
                if not content or not content.strip():
                    raise BettyError("Model returned an empty response.")
                return str(content).strip()
            assert self._places_tool is not None and message is not None
            payload.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                if call.function.name == TOOL_NAME:
                    result = self._places_tool(call.function.arguments)
                else:
                    result = f'{{"error": "Unknown tool {call.function.name}"}}'
                payload.append({"role": "tool", "tool_call_id": call.id, "content": result})
            # After the allowed rounds, stop offering the tool so the model must answer.
            if _ == self._settings.betty_max_tool_rounds - 1:
                use_tools = False
        raise BettyError("Model did not produce a final answer.")
