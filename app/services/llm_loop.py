"""The model call loop shared by Betty and the sight guide: tool rounds, empty-reply retry."""

import json
import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.schemas import Message

logger = logging.getLogger(__name__)


class BettyError(RuntimeError):
    """Raised when the model cannot produce a response."""


@dataclass(frozen=True)
class TextDelta:
    """The next piece of the answer text, as the model writes it."""

    text: str


@dataclass(frozen=True)
class ToolStarted:
    """The model asked for a tool; its handler runs next."""

    name: str


@dataclass(frozen=True)
class ToolFinished:
    """A tool's handler returned; its result went back to the model."""

    name: str


@dataclass(frozen=True)
class Finished:
    """The final answer: all the deltas joined, stripped."""

    reply: str


LoopEvent = TextDelta | ToolStarted | ToolFinished | Finished


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
        payload = _payload(system_prompt, messages)
        handlers = handlers or {}
        retried_empty = False
        for round_number in range(self._max_rounds + 1):
            kwargs = self._kwargs(temperature, tools, round_number)
            logger.debug("LLM request round %d: %s", round_number, payload)
            try:
                response = self._client.chat.completions.create(
                    model=self._model, messages=payload, **kwargs
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
                result = _run_tool(handlers, call.function.name, call.function.arguments)
                payload.append({"role": "tool", "tool_call_id": call.id, "content": result})
        raise BettyError("Model did not produce a final answer.")

    def stream(
        self,
        system_prompt: str,
        messages: Sequence[Message],
        temperature: float | None = None,
        tools: Sequence[dict[str, Any]] = (),
        handlers: Mapping[str, Callable[[str], str]] | None = None,
    ) -> Generator[LoopEvent]:
        """Like `complete`, but yields the answer as it is written, and each tool call.

        Ends with one `Finished`. Text is never taken back: text written before a tool call
        stays part of the answer, and a later round starts after a blank line. Reasoning the
        model streams in other fields is skipped. Closing the generator closes the model stream.
        """
        payload = _payload(system_prompt, messages)
        handlers = handlers or {}
        reply = ""
        retried_empty = False
        for round_number in range(self._max_rounds + 1):
            kwargs = self._kwargs(temperature, tools, round_number)
            logger.debug("LLM stream request round %d: %s", round_number, payload)
            try:
                stream = self._client.chat.completions.create(
                    model=self._model, messages=payload, stream=True, **kwargs
                )
            except Exception as exc:  # openai raises a broad family of API/connection errors
                raise BettyError(f"Upstream model request failed: {exc}") from exc
            round_text = ""
            calls: dict[int, dict[str, str]] = {}
            try:
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue
                    # A call's id and name come in its first piece, the arguments in pieces.
                    for piece in getattr(delta, "tool_calls", None) or []:
                        call = calls.setdefault(
                            piece.index, {"id": "", "name": "", "arguments": ""}
                        )
                        call["id"] = piece.id or call["id"]
                        if piece.function is not None:
                            call["name"] += piece.function.name or ""
                            call["arguments"] += piece.function.arguments or ""
                    text = getattr(delta, "content", None) or ""
                    if not round_text:
                        text = text.lstrip()
                        if text and reply:
                            text = "\n\n" + text
                    if text:
                        round_text += text
                        yield TextDelta(text)
            except Exception as exc:
                raise BettyError(f"Upstream model stream failed: {exc}") from exc
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            logger.info(
                "LLM stream (model=%s round=%d): %r tool_calls=%s",
                self._model,
                round_number,
                round_text,
                [(c["name"], c["arguments"]) for c in calls.values()] or None,
            )
            reply += round_text
            if not calls:
                if not round_text.strip():
                    # The NVIDIA endpoint occasionally returns no content; one retry fixes it.
                    if not retried_empty:
                        retried_empty = True
                        logger.warning("empty model response, retrying once")
                        continue
                    raise BettyError("Model returned an empty response.")
                yield Finished(reply.strip())
                return
            ordered = [calls[index] for index in sorted(calls)]
            payload.append(
                {
                    "role": "assistant",
                    "content": round_text or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                        for call in ordered
                    ],
                }
            )
            for call in ordered:
                yield ToolStarted(call["name"])
                result = _run_tool(handlers, call["name"], call["arguments"])
                payload.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                yield ToolFinished(call["name"])
        raise BettyError("Model did not produce a final answer.")

    def _kwargs(
        self, temperature: float | None, tools: Sequence[dict[str, Any]], round_number: int
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "temperature": self._temperature if temperature is None else temperature
        }
        if self._max_tokens:
            kwargs["max_tokens"] = self._max_tokens
        # On the final round stop offering tools so the model must answer in text.
        if tools and round_number < self._max_rounds:
            kwargs["tools"] = list(tools)
        return kwargs


def _payload(system_prompt: str, messages: Sequence[Message]) -> list[ChatCompletionMessageParam]:
    payload: list[ChatCompletionMessageParam] = [{"role": "system", "content": system_prompt}]
    for m in messages:
        if m.role == "user":
            payload.append({"role": "user", "content": m.content})
        else:
            payload.append({"role": "assistant", "content": m.content})
    return payload


def _run_tool(handlers: Mapping[str, Callable[[str], str]], name: str, arguments: str) -> str:
    handler = handlers.get(name)
    result = (
        json.dumps({"error": f"Unknown tool {name}"}) if handler is None else handler(arguments)
    )
    logger.info("tool %s -> %s", name, result[:500])
    return result
