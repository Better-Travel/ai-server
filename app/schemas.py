"""Pydantic request/response models for the REST API."""

from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    """A single conversation turn."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """Conversation history so far; the last message must come from the user."""

    messages: list[Message] = Field(min_length=1)


class ChatResponse(BaseModel):
    """Betty's next reply."""

    reply: str


class SummarizeRequest(BaseModel):
    """Conversation to condense into a trip summary."""

    messages: list[Message] = Field(min_length=1)


class SummarizeResponse(BaseModel):
    """Single-paragraph trip summary."""

    summary: str
