"""Betty: the trip-planning assistant backed by NVIDIA's OpenAI-compatible API."""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.config import Settings
from app.prompts import CHAT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from app.schemas import Message, TripContext
from app.services.places_tool import TOOL_NAME as PLACES_TOOL_NAME
from app.services.places_tool import TOOL_SPEC as PLACES_TOOL_SPEC
from app.services.places_tool import PlacesTool
from app.services.trip_context_tool import TOOL_NAME as TRIP_TOOL_NAME
from app.services.trip_context_tool import TOOL_SPEC as TRIP_TOOL_SPEC
from app.services.trip_context_tool import parse_trip_context


class BettyError(RuntimeError):
    """Raised when Betty cannot produce a response."""


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatResult:
    """Outcome of one chat turn."""

    reply: str
    trip_context: TripContext | None = None


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

    def chat(self, messages: Sequence[Message]) -> ChatResult:
        """Return Betty's next reply; `trip_context` is set when she judged the info sufficient."""
        captured: list[TripContext] = []

        def finalize(arguments: str) -> str:
            context, feedback = parse_trip_context(arguments)
            if context is not None:
                captured.append(context)
            return feedback

        tools: list[dict[str, Any]] = []
        handlers: dict[str, Callable[[str], str]] = {TRIP_TOOL_NAME: finalize}
        if self._places_tool is not None:
            tools.append(PLACES_TOOL_SPEC)
            handlers[PLACES_TOOL_NAME] = self._places_tool
        tools.append(TRIP_TOOL_SPEC)
        reply = self._complete(CHAT_SYSTEM_PROMPT, messages, tools=tools, handlers=handlers)
        return ChatResult(reply=reply, trip_context=captured[-1] if captured else None)

    def summarize(
        self, messages: Sequence[Message], trip_context: TripContext | None = None
    ) -> str:
        """Condense the conversation into one structured paragraph."""
        transcript = "\n".join(
            f"{'Traveller' if m.role == 'user' else 'Betty'}: {m.content}" for m in messages
        )
        structured = (
            f"Confirmed trip details (authoritative):\n{trip_context.model_dump_json()}\n\n"
            if trip_context
            else ""
        )
        request = Message(
            role="user",
            content=f"{structured}Conversation:\n{transcript}\n\n"
            "Write the trip summary paragraph now.",
        )
        # Collapse any stray line breaks so the output is guaranteed to be one paragraph.
        return " ".join(self._complete(SUMMARY_SYSTEM_PROMPT, [request], temperature=0.2).split())

    def _complete(
        self,
        system_prompt: str,
        messages: Sequence[Message],
        temperature: float | None = None,
        tools: Sequence[dict[str, Any]] = (),
        handlers: Mapping[str, Callable[[str], str]] | None = None,
    ) -> str:
        payload: list[ChatCompletionMessageParam] = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role == "user":
                payload.append({"role": "user", "content": m.content})
            else:
                payload.append({"role": "assistant", "content": m.content})
        handlers = handlers or {}
        max_rounds = self._settings.betty_max_tool_rounds
        for round_number in range(max_rounds + 1):
            kwargs: dict[str, Any] = {}
            # On the final round stop offering tools so the model must answer in text.
            if tools and round_number < max_rounds:
                kwargs["tools"] = list(tools)
            logger.debug("LLM request round %d: %s", round_number, payload)
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
            tool_calls = getattr(message, "tool_calls", None) if tools else None
            logger.info(
                "LLM response (model=%s round=%d): %r tool_calls=%s",
                self._settings.betty_model,
                round_number,
                message.content if message else None,
                [(c.function.name, c.function.arguments) for c in tool_calls]
                if tool_calls
                else None,
            )
            if not tool_calls:
                content = message.content if message else None
                if not content or not content.strip():
                    raise BettyError("Model returned an empty response.")
                return str(content).strip()
            assert message is not None
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
                handler = handlers.get(call.function.name)
                if handler is None:
                    result = json.dumps({"error": f"Unknown tool {call.function.name}"})
                else:
                    result = handler(call.function.arguments)
                logger.info("tool %s -> %s", call.function.name, result[:500])
                payload.append({"role": "tool", "tool_call_id": call.id, "content": result})
        raise BettyError("Model did not produce a final answer.")
