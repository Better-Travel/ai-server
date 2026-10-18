"""find_events: backend client, tool validation and chat wiring (no network)."""

import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.prompts import build_chat_prompt
from app.schemas import Event, EventOccurrence, EventSearch, Message, Trip
from app.services.betty_service import BettyService
from app.services.event_finder import (
    CANDIDATES,
    MAX_EVENTS,
    MAX_RANGE_DAYS,
    TIMEZONE,
    EventFinder,
    FoundEvents,
    describe_candidates,
    parse_picks,
    select_events,
    trip_dates,
)
from app.services.place_finder import FoundPlaces
from app.services.search_places_tool import search_result
from tests.test_search_places import (
    CAFE,
    END,
    START,
    FakeFinder,
    Script,
    args,
    offline_routes,
    text,
    tool_call,
    tool_message,
)

FROM = date.today() + timedelta(days=200)
TO = FROM + timedelta(days=1)


def raw_event(title: str, runs: list[tuple[str, str]], **extra: Any) -> dict[str, Any]:
    return {
        "title": title,
        "description": None,
        "url": f"https://x/{title}",
        "locations": [],
        "locationNames": ["Hall"],
        "eventCategories": ["Kultura"],
        "addresses": ["ul. Prosta 1"],
        "occurrences": [{"from": a, "to": b} for a, b in runs],
        "isRepeatable": False,
        "image": None,
        **extra,
    }


def finder_for(
    events: list[dict[str, Any]] | Callable[[httpx.Request], httpx.Response],
) -> EventFinder:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=events)

    transport = httpx.MockTransport(events if callable(events) else handler)
    return EventFinder("http://backend:8080/", client=httpx.Client(transport=transport))


def utc(day: date, hour: int, minute: int = 0) -> str:
    """ISO timestamp of a Warsaw local time (the offset is part of the string)."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TIMEZONE).isoformat()


# ---- the backend client


def test_only_warsaw_is_supported() -> None:
    found = finder_for([])("Kraków", FROM, TO)
    assert found.status == "unsupported" and found.message is not None
    assert "Warsaw" in found.message


@pytest.mark.parametrize("city", ["Warsaw", "warszawa", " WARSAW "])
def test_warsaw_spellings_are_accepted(city: str) -> None:
    assert finder_for([])(city, FROM, TO).status == "ok"


def test_request_goes_to_the_backend_with_a_widened_window() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    finder_for(handler)("Warsaw", FROM, TO)
    request = seen[0]
    assert request.url.path == "/api/better-travel/warsaw/events"
    assert request.url.params["from"] == f"{FROM - timedelta(days=1)}T00:00:00"
    assert request.url.params["to"] == f"{TO + timedelta(days=1)}T23:59:59"


def test_only_runs_inside_the_dates_are_kept_in_warsaw_time() -> None:
    weekly = [(FROM + timedelta(days=7 * i)) for i in range(-30, 30)]  # recurring all year
    event = raw_event(
        "Training",
        [(utc(d, 19), utc(d, 20)) for d in weekly] + [(utc(FROM, 19), utc(FROM, 20))],
    )
    found = finder_for([event])("Warsaw", FROM, TO)
    [result] = found.events
    assert [(o.start.date(), o.start.hour) for o in result.occurrences] == [(FROM, 19), (FROM, 19)]
    assert result.occurrences_in_range == 2
    assert result.occurrences[0].start.utcoffset() is not None
    assert found.total == 1


def test_times_from_the_backend_are_converted_to_warsaw_time() -> None:
    summer = date(FROM.year + 1, 7, 10)
    event = raw_event("Concert", [(f"{summer}T17:00:00+00:00", f"{summer}T19:00:00+00:00")])
    found = finder_for([event])("Warsaw", summer, summer)
    assert found.events[0].occurrences[0].start.hour == 19  # CEST is UTC+2


def test_long_exhibitions_that_span_the_dates_are_included() -> None:
    event = raw_event(
        "Exhibition", [(utc(FROM - timedelta(days=20), 10), utc(TO + timedelta(days=20), 18))]
    )
    assert finder_for([event])("Warsaw", FROM, TO).total == 1


def test_events_outside_the_dates_or_without_runs_are_dropped() -> None:
    far = FROM + timedelta(days=30)
    events = [
        raw_event("Far", [(utc(far, 10), utc(far, 11))]),
        raw_event("NoRuns", []),
        raw_event("Near", [(utc(FROM, 10), utc(FROM, 11))]),
    ]
    found = finder_for(events)("Warsaw", FROM, TO)
    assert [e.title for e in found.events] == ["Near"]


def test_occurrences_are_capped_and_counted() -> None:
    runs = [(utc(FROM, h), utc(FROM, h, 30)) for h in range(8, 20)]
    [event] = finder_for([raw_event("Many", runs)])("Warsaw", FROM, TO).events
    assert len(event.occurrences) == 5 and event.occurrences_in_range == 12


def test_coordinates_description_and_duplicates() -> None:
    event = raw_event(
        "Gig",
        [(utc(FROM, 20), utc(FROM, 22))],
        locations=[{"latitude": 52.2, "longitude": 21.0}],
        description="word " * 200,
    )
    found = finder_for([event, event])("Warsaw", FROM, TO)
    assert found.total == 1  # same url twice
    [gig] = found.events
    assert (gig.lat, gig.lon) == (52.2, 21.0)
    assert gig.description is not None and gig.description.endswith("…")


def make_event(
    i: int, categories: list[str], hour: int, ongoing: bool = False, day: date = FROM
) -> Event:
    start = datetime(day.year, day.month, day.day, hour, tzinfo=TIMEZONE)
    return Event(
        title=f"E{i}",
        url=f"u{i}",
        categories=categories,
        occurrences=[EventOccurrence(start=start, end=start + timedelta(hours=1))],
        ongoing=ongoing,
    )


def test_selection_takes_turns_between_categories() -> None:
    sport = [make_event(i, ["Sport"], 8 + i % 10) for i in range(20)]
    culture = [make_event(100 + i, ["Kultura"], 8 + i) for i in range(2)]
    picked = select_events([*sport, *culture], 6)
    assert [e.categories[0] for e in picked].count("Kultura") == 2  # not crowded out
    assert (MAX_EVENTS, CANDIDATES) == (5, 15)


def test_selection_puts_dated_events_before_uncategorised_and_ongoing_ones() -> None:
    old = FROM - timedelta(days=200)
    ongoing = [make_event(i, [], 0, ongoing=True, day=old) for i in range(5)]
    uncategorised = [make_event(10 + i, [], 12) for i in range(2)]
    dated = [make_event(20 + i, ["Kultura"], 18) for i in range(2)]
    titles = [e.title for e in select_events([*ongoing, *uncategorised, *dated], 6)]
    assert titles[:2] == ["E20", "E21"]  # dated and categorised first
    assert titles[2:4] == ["E10", "E11"]  # then dated without a category
    assert len(titles) == 6 and all(t in {f"E{i}" for i in range(5)} for t in titles[4:])


def test_ongoing_flag_marks_runs_that_began_before_the_dates() -> None:
    running = raw_event(
        "Expo", [(utc(FROM - timedelta(days=20), 10), utc(TO + timedelta(days=5), 18))]
    )
    dated = raw_event("Gig", [(utc(FROM, 20), utc(FROM, 22))])
    found = finder_for([running, dated])("Warsaw", FROM, TO)
    assert {e.title: e.ongoing for e in found.events} == {"Expo": True, "Gig": False}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"unexpected": "object"}),
        httpx.Response(
            200, json=[{"occurrences": [{"from": utc(FROM, 10), "to": utc(FROM, 11)}]}]
        ),  # an event in range with no title
    ],
)
def test_backend_failures_become_an_error_status(response: httpx.Response) -> None:
    found = finder_for(lambda request: response)("Warsaw", FROM, TO)
    assert found.status == "error" and found.message and "unavailable" in found.message


def test_connection_failure_becomes_an_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert finder_for(handler)("Warsaw", FROM, TO).status == "error"


# ---- which dates to look up


def a_trip(**overrides: Any) -> Trip:
    data: dict[str, Any] = {"city": "Warsaw", "country": "PL", "scope": "city", **overrides}
    return Trip.model_validate(data)


def test_start_and_end_dates_are_used_as_given() -> None:
    assert trip_dates(a_trip(start_date=START, end_date=END)) == (START, END)


def test_start_date_with_a_duration_gives_the_end_date() -> None:
    assert trip_dates(a_trip(start_date=START, duration_days=3)) == (
        START,
        START + timedelta(days=2),
    )


def test_a_lone_start_date_is_a_single_day() -> None:
    assert trip_dates(a_trip(start_date=START)) == (START, START)


def test_a_trip_with_only_a_duration_has_no_specific_dates() -> None:
    assert trip_dates(a_trip(duration_days=3)) is None


def test_long_trips_are_cut_to_the_first_two_weeks() -> None:
    start, end = trip_dates(a_trip(start_date=START, end_date=START + timedelta(days=40))) or (
        START,
        START,
    )
    assert (end - start).days + 1 == MAX_RANGE_DAYS


# ---- chat wiring: the events come with the search, when there are specific dates


class FakeEvents:
    def __init__(self, found: FoundEvents) -> None:
        self.found = found
        self.calls: list[tuple[str, date, date]] = []

    def __call__(self, city: str, from_date: date, to_date: date) -> FoundEvents:
        self.calls.append((city, from_date, to_date))
        return self.found


def a_found_event(title: str = "Concert") -> Event:
    start = datetime(FROM.year, FROM.month, FROM.day, 18, tzinfo=TIMEZONE)
    return Event(
        title=title,
        url="u",
        categories=["Kultura"],
        venues=["Hall"],
        occurrences=[EventOccurrence(start=start, end=start + timedelta(hours=2))],
        occurrences_in_range=1,
    )


def chat(
    arguments: str,
    events: FakeEvents | None,
    places: FoundPlaces | None = None,
    enabled: bool = True,
    pick: str | Exception | None = None,
) -> tuple[Any, Script, FakeEvents | None]:
    """One chat turn. `pick` is the model's reply to the event-relevance question, if asked."""
    replies: list[Any] = [tool_call(arguments), text("The traveller likes culture.")]
    if isinstance(pick, Exception):
        replies.append(pick)
    elif pick is not None:
        replies.append(text(pick))
    script = Script(*replies, text("Anything interesting?"))
    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=script))
    settings = Settings(llm_api_key="x", betty_events_tool=enabled, _env_file=None)
    finder = FakeFinder(places or FoundPlaces(status="ok", total=1, places=[CAFE]))
    service = BettyService(
        settings,
        client=client,
        place_finder=finder,
        event_finder=events,
        route_planner=offline_routes(),
    )
    result = service.chat([Message(role="user", content="Warsaw on those dates")])
    return result, script, events


def test_events_are_fetched_for_the_trip_dates_and_returned_to_the_client() -> None:
    events = FakeEvents(FoundEvents(status="ok", total=40, events=[a_found_event()]))
    result, script, _ = chat(args(), events)

    assert events.calls == [("Warsaw", START, END)]
    assert result.events is not None
    assert (result.events.city, result.events.from_date, result.events.to_date) == (
        "Warsaw",
        START,
        END,
    )
    assert (result.events.total, len(result.events.events), result.events.error) == (40, 1, None)
    assert result.search is not None and result.search.places == [CAFE]  # places are unaffected
    seen = tool_message(script)
    assert seen["status"] == "ok" and seen["places"][0]["name"] == "Cafe A"
    assert seen["events"]["total"] == 40
    assert seen["events"]["examples"][0] == {
        "title": "Concert",
        "when": f"{FROM} 18:00-20:00",
        "venue": "Hall",
        "category": "Kultura",
    }


def candidates(n: int = 12) -> list[Event]:
    return [a_found_event(f"E{i}") for i in range(n)]


def test_at_most_five_events_reach_betty_and_the_client() -> None:
    found = FakeEvents(FoundEvents(status="ok", total=40, events=candidates(12)))
    result, script, _ = chat(args(), found, pick="[1, 2, 3, 4, 5, 6, 7]")
    assert result.events is not None
    assert [e.title for e in result.events.events] == ["E0", "E1", "E2", "E3", "E4"]
    assert result.events.total == 40  # how many events there were, before the pick
    seen = tool_message(script)["events"]
    assert (seen["total"], seen["shown"], len(seen["examples"])) == (40, 5, 5)


def test_the_model_picks_the_relevant_events_in_its_own_order() -> None:
    found = FakeEvents(FoundEvents(status="ok", total=12, events=candidates(12)))
    result, script, _ = chat(args(), found, pick="Best fits: [7, 3, 11]")
    assert result.events is not None
    assert [e.title for e in result.events.events] == ["E6", "E2", "E10"]
    # the model saw the traveller summary and the numbered candidates
    ask = script.calls[2]["messages"][-1]["content"]
    assert "The traveller likes culture." in ask and "\n1. E0 | Kultura" in ask


@pytest.mark.parametrize("bad", ["I cannot decide", "[1, 2"])
def test_unusable_picks_fall_back_to_the_first_ranked_five(bad: str) -> None:
    found = FakeEvents(FoundEvents(status="ok", total=12, events=candidates(12)))
    result, _, _ = chat(args(), found, pick=bad)
    assert result.events is not None
    assert [e.title for e in result.events.events] == ["E0", "E1", "E2", "E3", "E4"]


def test_a_failing_model_falls_back_to_the_first_ranked_five() -> None:
    found = FakeEvents(FoundEvents(status="ok", total=12, events=candidates(12)))
    result, _, _ = chat(args(), found, pick=RuntimeError("model down"))
    assert result.events is not None and len(result.events.events) == 5


def test_none_relevant_means_no_events_for_betty_but_the_client_still_gets_the_count() -> None:
    found = FakeEvents(FoundEvents(status="ok", total=12, events=candidates(12)))
    result, script, _ = chat(args(), found, pick="[]")
    assert result.events is not None
    assert (result.events.total, result.events.events) == (12, [])
    assert "events" not in tool_message(script)


def test_a_single_candidate_needs_no_model_call() -> None:
    found = FakeEvents(FoundEvents(status="ok", total=1, events=candidates(1)))
    result, script, _ = chat(args(), found)  # no pick reply queued: asking would fail
    assert result.events is not None and len(result.events.events) == 1
    assert len(script.calls) == 3  # tool call, summary, final answer


def test_parse_picks() -> None:
    assert parse_picks("[3, 1, 7]", 10) == [2, 0, 6]
    assert parse_picks("[1, 1, 99, 0, 2]", 5) == [0, 1]  # repeats and out-of-range dropped
    assert parse_picks("[1,2,3,4,5,6,7]", 10) == [0, 1, 2, 3, 4]  # at most five
    assert parse_picks("reasoning [9] then [2, 4]", 10) == [1, 3]  # the last list wins
    assert parse_picks("[]", 10) == []
    assert parse_picks("no list here", 10) is None


def test_candidate_list_is_numbered_from_one() -> None:
    text_ = describe_candidates(candidates(2))
    assert text_.splitlines()[0].startswith("1. E0 | Kultura |")
    assert text_.splitlines()[1].startswith("2. E1 |")


def test_traveller_friendly_categories_come_first() -> None:
    services = [make_event(i, ["Zdrowie"], 8) for i in range(3)]
    culture = make_event(50, ["Kultura"], 20)
    leisure = make_event(51, ["Czas wolny"], 21)
    titles = [e.title for e in select_events([*services, culture, leisure], 2)]
    assert sorted(titles) == ["E50", "E51"]


def test_no_lookup_without_specific_dates() -> None:
    events = FakeEvents(FoundEvents(status="ok", total=1, events=[a_found_event()]))
    result, script, _ = chat(args(start_date=None, end_date=None, duration_days=3), events)

    assert events.calls == [] and result.events is None
    assert "events" not in tool_message(script)


def test_no_events_section_for_betty_when_nothing_was_found() -> None:
    result, script, _ = chat(args(), FakeEvents(FoundEvents(status="ok", total=0)))
    assert "events" not in tool_message(script)
    assert result.events is not None and result.events.total == 0  # the client still learns it


def test_a_failed_lookup_is_kept_for_the_client_and_hidden_from_betty() -> None:
    result, script, _ = chat(args(), FakeEvents(FoundEvents(status="error", message="down")))
    assert result.events is not None and result.events.error == "down"
    assert "events" not in tool_message(script)
    assert tool_message(script)["status"] == "ok"  # the place search is unaffected


def test_other_cities_get_no_events() -> None:
    events = FakeEvents(FoundEvents(status="unsupported", message="only Warsaw"))
    result, script, _ = chat(args(city="Kraków"), events)
    assert result.events is None and "events" not in tool_message(script)


def test_events_are_still_looked_up_when_the_place_search_fails() -> None:
    events = FakeEvents(FoundEvents(status="ok", total=1, events=[a_found_event()]))
    result, script, _ = chat(args(), events, FoundPlaces(status="error", message="osm down"))
    assert result.events is not None and result.events.total == 1
    seen = tool_message(script)
    assert seen["status"] == "error" and seen["events"]["total"] == 1


def test_the_lookup_can_be_disabled() -> None:
    events = FakeEvents(FoundEvents(status="ok", total=1, events=[a_found_event()]))
    result, script, _ = chat(args(), events, enabled=False)
    assert events.calls == [] and result.events is None


def test_betty_has_a_single_tool_and_a_prompt_rule_for_events() -> None:
    unsupported = FakeEvents(FoundEvents(status="unsupported"))
    _, script, _ = chat(args(), unsupported)
    assert [t["function"]["name"] for t in script.calls[0]["tools"]] == ["search_places"]
    prompt = build_chat_prompt(date.today())
    assert '"events" in the result' in prompt and "find_events" not in prompt
    assert "ask whether they are interested" in prompt


def test_search_result_envelope() -> None:
    search = EventSearch(
        city="Warsaw", from_date=FROM, to_date=TO, total=1, events=[a_found_event()]
    )
    found = FoundPlaces(status="ok", total=1, places=[CAFE])
    assert "events" not in json.loads(search_result("s", found))
    assert json.loads(search_result("s", found, search))["events"]["total"] == 1


def test_a_multi_category_event_counts_under_its_most_traveller_friendly_category() -> None:
    mixed = make_event(1, ["Społeczeństwo", "Czas wolny"], 10)
    services = [make_event(10 + i, ["Zdrowie"], 8) for i in range(3)]
    assert select_events([*services, mixed], 1) == [mixed]


def test_the_fallback_is_the_best_ranked_events_not_the_earliest() -> None:
    early_services = [make_event(i, ["Zdrowie"], 8) for i in range(10)]
    late_culture = [make_event(100 + i, ["Kultura"], 21) for i in range(5)]
    ranked = select_events([*early_services, *late_culture], 15)
    assert [e.title for e in ranked[:5]][0].startswith("E100")  # culture leads despite the hour
    found = FakeEvents(FoundEvents(status="ok", total=15, events=ranked))
    result, _, _ = chat(args(), found, pick="no idea")
    assert result.events is not None
    assert [e.title for e in result.events.events] == [e.title for e in ranked[:5]]


def test_no_output_cap_is_sent_by_default() -> None:
    """The reasoning model returns nothing if a cap is used up by its reasoning."""
    found = FakeEvents(FoundEvents(status="ok", total=12, events=candidates(12)))
    _, script, _ = chat(args(), found, pick="[1]")
    assert len(script.calls) == 4 and all("max_tokens" not in call for call in script.calls)


def test_a_configured_cap_is_still_honoured() -> None:
    script = Script(text("Hello"))
    client: Any = SimpleNamespace(chat=SimpleNamespace(completions=script))
    settings = Settings(llm_api_key="x", betty_max_tokens=300, _env_file=None)
    BettyService(settings, client=client).chat([Message(role="user", content="hi")])
    assert script.calls[0]["max_tokens"] == 300
