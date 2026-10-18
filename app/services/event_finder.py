"""Fetch city events from the BetterTravel backend for the dates of a trip.

The backend (`GET /api/better-travel/warsaw/events?from=&to=`) only knows Warsaw and returns every
occurrence of each event, often a whole year of them, so this trims them to the requested days.
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from app.schemas import Event, EventOccurrence, Trip

logger = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("Europe/Warsaw")
SUPPORTED_CITIES = frozenset({"warsaw", "warszawa"})
EVENTS_PATH = "/api/better-travel/warsaw/events"
MAX_RANGE_DAYS = 14
MAX_EVENTS = 5  # what the frontend gets
CANDIDATES = 15  # ranked pool the relevance pick chooses from
# Backend categories a visitor is most likely to want; the rest (health, safety, business, ...)
# are residents' services and only fill what is left.
TRAVELLER_CATEGORIES = ("Kultura", "Czas wolny", "Sport")
MAX_OCCURRENCES = 5
MAX_DESCRIPTION_CHARS = 300


@dataclass(frozen=True)
class FoundEvents:
    """Result of one lookup. `message` explains an `unsupported` or `error` status."""

    status: Literal["ok", "unsupported", "error"]
    total: int = 0
    events: list[Event] = field(default_factory=list)
    message: str | None = None


EventSearcher = Callable[[str, date, date], FoundEvents]


def trip_dates(trip: Trip) -> tuple[date, date] | None:
    """The calendar dates to look events up for, or None if the trip has no specific dates.

    A trip with only a number of days has no dates, so there is nothing to look up. With a start
    date alone it is a single day. Long trips are cut to their first MAX_RANGE_DAYS days.
    """
    if trip.start_date is None:
        return None
    end = trip.end_date
    if end is None:
        end = trip.start_date + timedelta(days=(trip.duration_days or 1) - 1)
    return trip.start_date, min(end, trip.start_date + timedelta(days=MAX_RANGE_DAYS - 1))


def _local(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TIMEZONE)


def _trim(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    return text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"


def to_event(raw: dict[str, Any], start: datetime, end: datetime) -> Event | None:
    """Convert one backend event; None if none of its runs overlaps [start, end)."""
    runs = [
        EventOccurrence(start=_local(o["from"]), end=_local(o["to"]))
        for o in raw.get("occurrences") or []
    ]
    in_range = [o for o in runs if o.start < end and o.end >= start]
    if not in_range:
        return None
    locations = raw.get("locations") or []
    return Event(
        title=raw["title"],
        url=raw["url"],
        categories=raw.get("eventCategories") or [],
        venues=raw.get("locationNames") or [],
        addresses=raw.get("addresses") or [],
        lat=locations[0]["latitude"] if locations else None,
        lon=locations[0]["longitude"] if locations else None,
        description=_trim(raw.get("description")),
        repeatable=bool(raw.get("isRepeatable")),
        occurrences=sorted(in_range, key=lambda o: o.start)[:MAX_OCCURRENCES],
        occurrences_in_range=len(in_range),
        ongoing=all(o.start < start for o in in_range),
    )


def _rank(category: str) -> int:
    return (
        TRAVELLER_CATEGORIES.index(category)
        if category in TRAVELLER_CATEGORIES
        else len(TRAVELLER_CATEGORIES)
    )


def _best_category(event: Event) -> str:
    """The event's most traveller-friendly category ("other" if it has none)."""
    return min(event.categories, key=_rank) if event.categories else "other"


def _take_turns(events: list[Event], limit: int) -> list[Event]:
    """Up to `limit` events, one per category in turn (traveller-friendly categories first).

    The result is in ranking order, not time order: the first events are the best ones.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for event in sorted(events, key=lambda e: e.occurrences[0].start):
        groups[_best_category(event)].append(event)
    ordered = sorted(groups, key=_rank)
    picked: list[Event] = []
    while len(picked) < limit and any(groups.values()):
        for category in ordered:
            if groups[category] and len(picked) < limit:
                picked.append(groups[category].pop(0))
    return picked


def select_events(events: list[Event], limit: int) -> list[Event]:
    """Up to `limit` events in ranking order: dated happenings inside the dates before ongoing ones.

    The backend also lists long-running programmes and open calls (no category, a run lasting
    months); they only fill what is left after categorised, dated events and uncategorised ones.
    Within each tier events take turns between categories so no category crowds out the rest.
    """
    dated = [e for e in events if not e.ongoing]
    tiers = [
        [e for e in dated if e.categories],
        [e for e in dated if not e.categories],
        [e for e in events if e.ongoing],
    ]
    picked: list[Event] = []
    for tier in tiers:
        picked += _take_turns(tier, limit - len(picked))
    return picked


def describe_candidates(events: list[Event]) -> str:
    """The numbered list (from 1) shown to the model that picks the relevant events."""
    lines = []
    for number, event in enumerate(events, start=1):
        run = event.occurrences[0]
        when = f"{run.start:%Y-%m-%d %H:%M}"
        lines.append(
            f"{number}. {event.title} | {', '.join(event.categories) or '-'} | {when} | "
            f"{', '.join(event.venues) or '-'}"
        )
    return "\n".join(lines)


def parse_picks(reply: str, count: int, limit: int = MAX_EVENTS) -> list[int] | None:
    """Zero-based indexes from a reply like `[3, 1, 7]`; None if the reply is not such a list.

    Out-of-range numbers and repeats are dropped; at most `limit` are kept, in the given order.
    """
    lists = re.findall(r"\[[\d,\s]*\]", reply)
    if not lists:
        return None
    picks: list[int] = []
    for number in json.loads(lists[-1]):
        if 1 <= number <= count and number - 1 not in picks:
            picks.append(number - 1)
    return picks[:limit]


class EventFinder:
    """Calls the backend; failures become a status, never an exception."""

    def __init__(
        self, base_url: str, timeout: float = 30.0, client: httpx.Client | None = None
    ) -> None:
        self._url = base_url.rstrip("/") + EVENTS_PATH
        self._timeout = timeout
        self._client = client

    def __call__(self, city: str, from_date: date, to_date: date) -> FoundEvents:
        if city.strip().casefold() not in SUPPORTED_CITIES:
            return FoundEvents(
                status="unsupported", message=f"Events are only available for Warsaw, not {city}."
            )
        start = datetime.combine(from_date, time.min, TIMEZONE)
        end = datetime.combine(to_date + timedelta(days=1), time.min, TIMEZONE)
        # Ask for a day more on each side: the backend's date handling is not time-zone aware,
        # and the exact window is applied here.
        params = {
            "from": f"{(from_date - timedelta(days=1)).isoformat()}T00:00:00",
            "to": f"{(to_date + timedelta(days=1)).isoformat()}T23:59:59",
        }
        try:
            client = self._client or httpx.Client(timeout=self._timeout)
            try:
                response = client.get(self._url, params=params)
            finally:
                if self._client is None:
                    client.close()
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or not all(isinstance(raw, dict) for raw in data):
                raise ValueError("unexpected response shape (expected a list of events)")
            found = [e for raw in data if (e := to_event(raw, start, end)) is not None]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("events lookup failed: %s", exc)
            return FoundEvents(status="error", message=f"Events are unavailable right now: {exc}")
        unique = list({e.url: e for e in found}.values())
        logger.info("events %s..%s: %d overlap the dates", from_date, to_date, len(unique))
        return FoundEvents(status="ok", total=len(unique), events=select_events(unique, CANDIDATES))
