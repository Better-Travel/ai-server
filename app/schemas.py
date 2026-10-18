"""Pydantic request/response models for the REST API."""

from datetime import date, datetime
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


class RouteStop(BaseModel):
    """One stop of a route; full details are in `places` under the same `osm_id`."""

    osm_id: str
    name: str | None = None
    category: str
    lat: float
    lon: float
    role: Literal["must", "on_the_way"] = Field(
        description="'must': a key place for this traveller; 'on_the_way': added because it is "
        "close to the path between them."
    )
    why: str | None = Field(default=None, description="Short reason shown to the traveller.")


class RouteBuildStep(BaseModel):
    """One step of building a route (debugging/visualisation only)."""

    action: Literal["anchors", "add", "reorder"] = Field(
        description="'anchors': the must-visits in shortest order; 'add': one place inserted on "
        "the way; 'reorder': the final shortest order of all stops."
    )
    osm_id: str | None = Field(default=None, description="The place added ('add' only).")
    detour_m: float | None = Field(default=None, description="Extra metres it cost ('add' only).")
    order: list[str] = Field(description="osm_ids of the route after this step, in order.")


class Route(BaseModel):
    """An ordered walk through some of the found places."""

    stops: list[RouteStop]
    distance_km: float = Field(
        description="Straight-line length through the stops in order; real walking is longer."
    )
    steps: list[RouteBuildStep] = Field(
        default_factory=list,
        description="How the route was built, for debugging; clients can ignore it.",
    )


class PlaceSearch(BaseModel):
    """Outcome of a search_places call that passed validation (the 'enough info' signal)."""

    trip: Trip
    summary: str = Field(description="Exact text passed to find_places.")
    query: str | None = Field(
        default=None, description="The Overpass QL the model generated for this search."
    )
    query_attempts: int = Field(
        default=0, description="Queries the model wrote (more than 1 means syntax fixes)."
    )
    fetched: int = Field(
        default=0, description="Raw elements Overpass returned, before duplicates were dropped."
    )
    total: int = Field(default=0, description="Number of distinct places OpenStreetMap returned.")
    places: list[Place] = Field(
        default_factory=list,
        description="Sights and attractions only (no food), max 30, notable ones first.",
    )
    routes: list[Route] = Field(
        default_factory=list,
        description="Suggested routes through `places`, best first (currently at most one).",
    )
    error: str | None = Field(
        default=None, description="Set when the trip was accepted but the search failed."
    )


class EventOccurrence(BaseModel):
    """One run of an event, in Warsaw local time."""

    start: datetime
    end: datetime


class Event(BaseModel):
    """One city event from the BetterTravel backend, trimmed to the requested dates."""

    title: str
    url: str
    categories: list[str] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list, description="Location names.")
    addresses: list[str] = Field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    description: str | None = None
    repeatable: bool = False
    occurrences: list[EventOccurrence] = Field(
        default_factory=list, description="Only runs that overlap the requested dates (max 5)."
    )
    occurrences_in_range: int = Field(default=0, description="How many runs overlap the dates.")
    ongoing: bool = Field(
        default=False,
        description="True if every run overlapping the dates began before them (e.g. an "
        "exhibition or programme that is already running), not a dated happening inside them.",
    )


class EventSearch(BaseModel):
    """Events found on a trip's specific dates (shown to the traveller by the client)."""

    city: str
    from_date: date
    to_date: date
    total: int = Field(default=0, description="Events overlapping the dates, before the cap.")
    events: list[Event] = Field(
        default_factory=list, description="At most 5, the ones most relevant to the traveller."
    )
    error: str | None = Field(default=None, description="Set when the lookup failed.")


class ChatRequest(BaseModel):
    """Conversation history so far; the last message must come from the user."""

    messages: list[Message] = Field(min_length=1)


class ChatResponse(BaseModel):
    """Betty's next reply, plus the place search and event lookup if she ran them this turn."""

    reply: str
    events: EventSearch | None = Field(
        default=None, description="Set only on a turn where Betty looked up city events."
    )
    search: PlaceSearch | None = Field(
        default=None,
        description="Set only on a turn where Betty had enough information and searched.",
    )


class StreamStart(BaseModel):
    """First event of a streamed turn (`event: start`)."""

    turn_id: str = Field(description="Id of this turn in the server logs; quote it in bug reports.")


class StreamStatus(BaseModel):
    """What the server is doing while no text arrives (`event: status`)."""

    stage: Literal["searching_places"] = Field(
        description="'searching_places': looking up places, events and the route (can take a "
        "while). Clients should ignore stages they don't know."
    )


class StreamDelta(BaseModel):
    """The next piece of Betty's reply (`event: delta`); append it to the text so far."""

    text: str


class StreamError(BaseModel):
    """The turn failed after the stream started (`event: error`); nothing else follows."""

    code: Literal["upstream_unavailable", "internal"] = Field(
        description="'upstream_unavailable': the language model failed or timed out; "
        "'internal': unexpected server error."
    )
    message: str
    retryable: bool = Field(default=True, description="Sending the same request again may work.")


class SummarizeRequest(BaseModel):
    """Conversation to condense into a trip summary."""

    messages: list[Message] = Field(min_length=1)
    trip: Trip | None = Field(
        default=None, description="Validated trip (e.g. `search.trip` from /chat); authoritative."
    )


class SummarizeResponse(BaseModel):
    """Single-paragraph trip summary (the same text search_places passes to find_places)."""

    summary: str
