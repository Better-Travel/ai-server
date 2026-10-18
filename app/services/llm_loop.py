"""The model call loop shared by Betty and the sight guide: tool rounds, empty-reply retry."""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.schemas import Message

logger = logging.getLogger(__name__)


class BettyError(RuntimeError):
    """Raised when the model cannot produce a response."""


class ToolLoop:
    """Calls a chat model, runs the tools it asks for, and returns its final text answer."""

    def __init__(
        self,
        client: OpenAI,
        model: str,
        *,
        max_rounds: int,
        temperature: float,
        max_tokens: int | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_rounds = max_rounds
        self._temperature = temperature
        self._max_tokens = max_tokens

    def complete(
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
        retried_empty = False
        for round_number in range(self._max_rounds + 1):
            kwargs: dict[str, Any] = {}
            if self._max_tokens:
                kwargs["max_tokens"] = self._max_tokens
            # On the final round stop offering tools so the model must answer in text.
            if tools and round_number < self._max_rounds:
                kwargs["tools"] = list(tools)
            logger.debug("LLM request round %d: %s", round_number, payload)
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=payload,
                    temperature=self._temperature if temperature is None else temperature,
                    **kwargs,
                )
            except Exception as exc:  # openai raises a broad family of API/connection errors
                raise BettyError(f"Upstream model request failed: {exc}") from exc
            message = response.choices[0].message if response.choices else None
            tool_calls = getattr(message, "tool_calls", None) if tools else None
            logger.info(
                "LLM response (model=%s round=%d): %r tool_calls=%s",
                self._model,
                round_number,
                message.content if message else None,
                [(c.function.name, c.function.arguments) for c in tool_calls]
                if tool_calls
                else None,
            )
            if not tool_calls:
                content = message.content if message else None
                if not content or not content.strip():
                    # The NVIDIA endpoint occasionally returns no content; one retry fixes it.
                    if not retried_empty:
                        retried_empty = True
                        logger.warning("empty model response, retrying once")
                        continue
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
