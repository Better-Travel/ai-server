"""Pydantic request/response models for the REST API."""

from datetime import date
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class Message(BaseModel):
    """A single conversation turn."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


_EMPTY_WORDS = {"", "none", "null", "n/a", "na", "unknown", "not specified", "unspecified"}


class Trip(BaseModel):
    """The confirmed minimum for a place search: one city or district, plus the timing.

    Everything else the traveller said (interests, companions, budget, ...) is taken from the
    conversation by the summariser, so it is not part of this model.
    """

    city: str = Field(min_length=1, description="City or town as the traveller named it.")
    district: str | None = Field(
        default=None, description="Neighbourhood or district, only if the traveller narrowed it."
    )
    country: str = Field(description="ISO 3166-1 alpha-2 country code, e.g. 'PL' for Poland.")
    scope: Literal["city", "district", "region", "country"] = Field(
        description="Size of the area the traveller wants. Use 'region' or 'country' if they "
        "have not narrowed it to one city or district."
    )
    start_date: date | None = Field(
        default=None, description="ISO date YYYY-MM-DD, only if the traveller gave it."
    )
    end_date: date | None = Field(
        default=None, description="ISO date YYYY-MM-DD, only if the traveller gave it."
    )
    duration_days: int | None = Field(
        default=None, ge=1, le=60, description="Number of days, only if the traveller gave it."
    )

    @model_validator(mode="before")
    @classmethod
    def _tolerate_llm_quirks(cls, data: Any) -> Any:
        """Models sometimes write "none" (or similar) instead of leaving a field out."""
        if not isinstance(data, dict):
            return data
        return {
            key: None if isinstance(value, str) and value.strip().lower() in _EMPTY_WORDS else value
            for key, value in data.items()
        }

    @field_validator("country")
    @classmethod
    def _country_is_alpha2(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("country must be an ISO 3166-1 alpha-2 code such as 'PL'")
        return value

    @model_validator(mode="after")
    def _meets_minimum_requirements(self) -> Self:
        if self.scope not in ("city", "district"):
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

    def describe(self) -> str:
        """Deterministic text of the validated facts; leads the summary sent to find_places."""
        where = f"{self.district}, {self.city}" if self.district else self.city
        text = f"Destination: {where}, country {self.country}."
        when: list[str] = []
        if self.start_date and self.end_date:
            when.append(f"{self.start_date.isoformat()} to {self.end_date.isoformat()}")
        elif self.start_date:
            when.append(f"from {self.start_date.isoformat()}")
        if self.duration_days:
            when.append(f"{self.duration_days} day{'s' if self.duration_days != 1 else ''}")
        if when:
            text += f" Dates: {', '.join(when)}."
        return text


class WikipediaSnippet(BaseModel):
    """Intro of a place's Wikipedia article in one language."""

    title: str
    url: str
    extract: str


class Place(BaseModel):
    """One OpenStreetMap result as shown to Betty and returned to the client."""

    osm_id: str = Field(
        description="Stable OSM id, '<type>/<id>' (e.g. 'node/123'), for deduping and favourites."
    )
    name: str | None = None
    type: Literal["node", "way", "relation"]
    category: str = Field(
        description="What kind of place it is, as one OSM tag (e.g. 'tourism=museum'), or 'other'."
    )
    lat: float | None = None
    lon: float | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    wikipedia: dict[str, WikipediaSnippet] | None = Field(
        default=None, description="Keyed by language code ('pl', 'en')."
    )


class PlaceSearch(BaseModel):
    """Outcome of a search_places call that passed validation (the 'enough info' signal)."""

    trip: Trip
    summary: str = Field(description="Exact text passed to find_places.")
    total: int = Field(default=0, description="Number of distinct places OpenStreetMap returned.")
    places: list[Place] = Field(
        default_factory=list, description="The best results (max 20), a mix of kinds."
    )
    error: str | None = Field(
        default=None, description="Set when the trip was accepted but the search failed."
    )


class ChatRequest(BaseModel):
    """Conversation history so far; the last message must come from the user."""

    messages: list[Message] = Field(min_length=1)


class ChatResponse(BaseModel):
    """Betty's next reply, plus the place search if she ran one this turn."""

    reply: str
    search: PlaceSearch | None = Field(
        default=None,
        description="Set only on a turn where Betty had enough information and searched.",
    )


class SummarizeRequest(BaseModel):
    """Conversation to condense into a trip summary."""

    messages: list[Message] = Field(min_length=1)
    trip: Trip | None = Field(
        default=None, description="Validated trip (e.g. `search.trip` from /chat); authoritative."
    )


class SummarizeResponse(BaseModel):
    """Single-paragraph trip summary (the same text search_places passes to find_places)."""

    summary: str
