"""Pydantic request/response models for the REST API."""

import contextlib
import json
from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class Message(BaseModel):
    """A single conversation turn."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


_EMPTY_WORDS = {"", "none", "null", "n/a", "na", "unknown", "not specified", "unspecified"}


class Destination(BaseModel):
    """Where the trip happens; must be small enough for one bounded OpenStreetMap query."""

    city: str = Field(min_length=1, description="City or town, e.g. 'Warsaw'.")
    district: str | None = Field(default=None, description="Neighbourhood/district if narrowed.")
    country: str = Field(min_length=1, description="Country, e.g. 'Poland'.")
    scope: Literal["city", "district", "region", "country"] = Field(
        description="Size of the area the traveller wants to plan. Use 'region' or 'country' "
        "if they have not narrowed it to one city or district."
    )


class TripContext(BaseModel):
    """Validated trip details; the hard minimum is a narrow destination plus timing."""

    destination: Destination
    start_date: date | None = Field(default=None, description="ISO date YYYY-MM-DD.")
    end_date: date | None = Field(default=None, description="ISO date YYYY-MM-DD.")
    duration_days: int | None = Field(default=None, ge=1, le=60)
    interests: list[str] = Field(default_factory=list)
    free_hours: str | None = Field(default=None, description="Daily free hours, if mentioned.")
    companions: str | None = None
    budget: str | None = None
    notes: str | None = Field(default=None, description="Pace, diet, accessibility, transport...")

    @model_validator(mode="before")
    @classmethod
    def _tolerate_llm_quirks(cls, data: Any) -> Any:
        """Models sometimes JSON-encode nested objects or write "none" for unknown values."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if isinstance(data.get("destination"), str):
            # On bad JSON the string is left as-is so validation reports it.
            with contextlib.suppress(json.JSONDecodeError):
                data["destination"] = json.loads(data["destination"])
        for key in (
            "start_date",
            "end_date",
            "duration_days",
            "free_hours",
            "companions",
            "budget",
            "notes",
        ):
            value = data.get(key)
            if isinstance(value, str) and value.strip().lower() in _EMPTY_WORDS:
                data[key] = None
        if isinstance(data.get("interests"), str):
            data["interests"] = [data["interests"]]
        return data

    @field_validator("interests", mode="after")
    @classmethod
    def _drop_empty_interests(cls, value: list[str]) -> list[str]:
        return [v for v in value if v.strip().lower() not in _EMPTY_WORDS]

    @model_validator(mode="after")
    def _meets_minimum_requirements(self) -> Self:
        if self.destination.scope not in ("city", "district"):
            raise ValueError(
                "destination is too broad: ask the traveller which single city or district "
                "to plan, then call the tool again"
            )
        if self.start_date is None and self.duration_days is None:
            raise ValueError("timing is missing: need start_date or duration_days")
        today = date.today()
        for name in ("start_date", "end_date"):
            value: date | None = getattr(self, name)
            if value is not None and value < today:
                raise ValueError(
                    f"{name} {value} is in the past (today is {today}): ask the traveller "
                    "for the correct date, do not guess"
                )
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValueError("end_date is before start_date")
            days = (self.end_date - self.start_date).days + 1
            if self.duration_days is not None and self.duration_days != days:
                raise ValueError(
                    f"duration_days={self.duration_days} contradicts the dates ({days} days): "
                    "set only what the traveller said"
                )
        return self


class ChatRequest(BaseModel):
    """Conversation history so far; the last message must come from the user."""

    messages: list[Message] = Field(min_length=1)


class ChatResponse(BaseModel):
    """Betty's next reply, plus the trip context once she judged the info sufficient."""

    reply: str
    ready: bool = False
    trip_context: TripContext | None = None


class SummarizeRequest(BaseModel):
    """Conversation to condense into a trip summary."""

    messages: list[Message] = Field(min_length=1)
    trip_context: TripContext | None = Field(
        default=None, description="Structured context from /chat; used as the source of truth."
    )


class SummarizeResponse(BaseModel):
    """Single-paragraph trip summary."""

    summary: str
