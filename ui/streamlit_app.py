"""Streamlit UI for trying Betty locally: chat on the left, function debugger on the right."""

import json
import math
import os
from collections.abc import Sequence
from typing import Any

import httpx
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

DEFAULT_API_URL = os.getenv("BETTY_API_URL", "http://localhost:8000")
# A chat turn that searches makes several slow LLM calls (query, route, reply): often 1-3 minutes.
API_TIMEOUT_SECONDS = float(os.getenv("BETTY_API_TIMEOUT", "600"))
GREETING = (
    "Hi, I'm Betty, your local guide! Tell me where you'd like to go "
    "and I'll help shape the perfect trip."
)
CHAT_HEIGHT = 560
CHAT_MAP_HEIGHT = 320
# Debug functions: key -> (title, endpoint)
FUNCTIONS = {
    "summarize": ("Summarize", "/api/v1/summarize"),
    "check": ("Search check", "/api/v1/chat"),
}

# Route map colours (RGBA): must-visits, places added on the way, the place added at this step,
# and found places that are not in the route.
ROLE_COLOURS = {"must": [220, 50, 50, 230], "on_the_way": [245, 150, 30, 230]}
NEW_COLOUR = [40, 120, 255, 255]
OTHER_COLOUR = [170, 170, 170, 150]
PATH_COLOUR = [0, 190, 210, 230]

st.set_page_config(
    page_title="Betty Trip Planner",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="collapsed",
)

api_url: str = st.sidebar.text_input("API URL", DEFAULT_API_URL).rstrip("/")

for key, default in (
    ("messages", []),
    ("search", None),
    ("events", None),
    ("debug_output", {}),
    ("chat_routes", {}),  # index of an assistant message -> the route returned with it
):
    if key not in st.session_state:
        st.session_state[key] = default


def post(path: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST `body` to the backend; show an error and return None on failure."""
    try:
        response = httpx.post(f"{api_url}{path}", json=body, timeout=API_TIMEOUT_SECONDS)
        response.raise_for_status()
        return dict(response.json())
    except httpx.HTTPStatusError as exc:
        st.error(f"API error {exc.response.status_code}: {exc.response.text}")
    except httpx.HTTPError as exc:
        st.error(f"Could not reach the API at {api_url}: {exc}")
    return None


def history_request_body(tool: str) -> dict[str, Any]:
    """The current chat in the exact shape the function's endpoint expects."""
    messages = list(st.session_state.messages)
    body: dict[str, Any] = {"messages": messages}
    if tool == "summarize":
        if st.session_state.search:
            body["trip"] = st.session_state.search["trip"]
    else:
        # /chat needs the last message to come from the user, so re-check the latest user turn.
        while messages and messages[-1]["role"] == "assistant":
            messages.pop()
    return body


def parse_input(text: str) -> dict[str, Any] | None:
    """Accept request JSON, a bare message list, or plain text (sent as one user message)."""
    text = text.strip()
    if not text:
        st.warning("Input is empty.")
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"messages": [{"role": "user", "content": text}]}
    if isinstance(parsed, list):
        return {"messages": parsed}
    if isinstance(parsed, dict):
        return parsed
    return {"messages": [{"role": "user", "content": text}]}


def copy_button(text: str) -> None:
    """Small button that copies `text` to the clipboard (Streamlit has no native one)."""
    # The history contains user/LLM text and the iframe has same-origin access, so escape
    # everything that could terminate the script or open a tag.
    payload = (
        json.dumps(text).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    st.iframe(
        f"""
        <button id="b" title="Copy chat history as JSON" style="cursor:pointer;
          border:1px solid rgba(128,128,128,.5); background:transparent; color:#888;
          border-radius:6px; padding:3px 10px; font:13px sans-serif;">📋 Copy</button>
        <script>
        const text = {payload};
        const b = document.getElementById("b");
        b.onclick = async () => {{
          try {{ await navigator.clipboard.writeText(text); }}
          catch (e) {{
            const t = document.createElement("textarea"); t.value = text;
            document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove();
          }}
          b.textContent = "✓ Copied";
          setTimeout(() => (b.textContent = "📋 Copy"), 1500);
        }};
        </script>
        """,
        height=40,
    )


def run_function(tool: str, body: dict[str, Any] | None) -> None:
    """Call the function's endpoint and remember request + response for display."""
    if body is None:
        return
    _, path = FUNCTIONS[tool]
    with st.spinner(f"Calling {path}..."):
        response = post(path, body)
    st.session_state.debug_output[tool] = {"request": body, "response": response}


def place_row(place: dict[str, Any]) -> dict[str, Any]:
    """One table row for a place from `search.places`."""
    tags = place.get("tags", {})
    wikipedia = place.get("wikipedia") or {}
    street = " ".join(filter(None, (tags.get("addr:street"), tags.get("addr:housenumber"))))
    link = next((w["url"] for lang in ("en", "pl") if (w := wikipedia.get(lang))), None)
    return {
        "name": place.get("name"),
        "kind": place.get("category"),
        "address": street,
        "website": tags.get("website") or tags.get("contact:website"),
        "wikipedia": link,
        "langs": ", ".join(wikipedia),
        "lat": place.get("lat"),
        "lon": place.get("lon"),
    }


def describe_step(step: dict[str, Any], stops: dict[str, dict[str, Any]]) -> str:
    """One line saying what a route build step did."""

    def name(osm_id: str) -> str:
        return str(stops.get(osm_id, {}).get("name") or osm_id)

    if step["action"] == "anchors":
        return "LLM must-visits, in shortest order: " + " → ".join(map(name, step["order"]))
    if step["action"] == "add":
        stop = stops.get(step["osm_id"], {})
        why = f" ({stop['why']})" if stop.get("why") else ""
        return f"Added **{name(step['osm_id'])}** on the way, +{step['detour_m']:.0f} m{why}"
    return "All stops re-ordered into the shortest path"


def route_deck(
    order: list[dict[str, Any]],
    all_stops: list[dict[str, Any]],
    others: Sequence[dict[str, Any]] = (),
    highlight: str | None = None,
    size_px: int = 500,
) -> pdk.Deck:
    """Map of `order` as numbered dots joined by a line; the view fits `all_stops`."""
    points = [
        {
            "lon": s["lon"],
            "lat": s["lat"],
            "number": str(i),
            "label": f"{i}. {s.get('name') or s['osm_id']} ({s['role']})",
            "colour": NEW_COLOUR if s["osm_id"] == highlight else ROLE_COLOURS[s["role"]],
            "radius": 150 if s["osm_id"] == highlight else 90,
        }
        for i, s in enumerate(order, start=1)
    ]
    background = [
        {"lon": p["lon"], "lat": p["lat"], "label": p.get("name") or p.get("osm_id")}
        for p in others
    ]
    lats = [s["lat"] for s in all_stops]
    lons = [s["lon"] for s in all_stops]
    centre_lat = (min(lats) + max(lats)) / 2
    # Fit the whole route into a map about `size_px` across, with a margin; one zoom level
    # halves the degrees shown.
    span = max(max(lons) - min(lons), (max(lats) - min(lats)) / math.cos(math.radians(centre_lat)))
    zoom = min(15.0, max(9.0, math.log2(size_px * 360 / (256 * max(span, 1e-4))) - 1.6))
    view = pdk.ViewState(latitude=centre_lat, longitude=(min(lons) + max(lons)) / 2, zoom=zoom)
    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            background,
            get_position=["lon", "lat"],
            get_fill_color=OTHER_COLOUR,
            get_radius=20,
            radius_min_pixels=3,
            pickable=True,
        ),
        pdk.Layer(
            "PathLayer",
            [{"path": [[p["lon"], p["lat"]] for p in points]}],
            get_path="path",
            get_color=PATH_COLOUR,
            width_min_pixels=4,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            points,
            get_position=["lon", "lat"],
            get_fill_color="colour",
            get_radius="radius",
            radius_min_pixels=6,
            pickable=True,
        ),
        pdk.Layer(
            "TextLayer",
            points,
            get_position=["lon", "lat"],
            get_text="number",
            get_size=14,
            get_color=[255, 255, 255, 255],
        ),
    ]
    return pdk.Deck(layers=layers, initial_view_state=view, tooltip={"text": "{label}"})


def render_chat_route(route: dict[str, Any]) -> None:
    """The route Betty returned, as the app would show it: numbered stops on a map, in order."""
    stops = route.get("stops") or []
    if not stops:
        return
    st.markdown(f"**🗺️ Your route**: {len(stops)} stops, {route['distance_km']} km")
    st.pydeck_chart(route_deck(stops, stops, size_px=CHAT_MAP_HEIGHT), height=CHAT_MAP_HEIGHT)
    st.markdown(
        "\n".join(
            f"{i}. **{s.get('name') or s['osm_id']}**" + (f" – {s['why']}" if s.get("why") else "")
            for i, s in enumerate(stops, start=1)
        )
    )


def render_route(search: dict[str, Any], key: str) -> None:
    """Map of the first route; a slider replays how it was built, step by step."""
    routes = search.get("routes") or []
    if not routes or not routes[0].get("stops"):
        return
    route = routes[0]
    stops = {s["osm_id"]: s for s in route["stops"]}
    steps = route.get("steps") or [{"action": "reorder", "order": list(stops)}]
    st.markdown(
        f"**Route**: {len(stops)} stops, {route['distance_km']} km straight-line, "
        f"built in {len(steps)} step{'s' if len(steps) != 1 else ''}"
    )
    index = len(steps)
    if len(steps) > 1:
        index = st.slider("Build step", 1, len(steps), len(steps), key=f"route_step_{key}")
    step = steps[index - 1]
    st.caption(f"Step {index}: {describe_step(step, stops)}")

    order = [stops[osm_id] for osm_id in step["order"] if osm_id in stops]
    in_route = set(step["order"])
    others = [
        p
        for p in search.get("places") or []
        if p.get("lat") is not None and p.get("lon") is not None and p.get("osm_id") not in in_route
    ]
    st.pydeck_chart(route_deck(order, route["stops"], others, step.get("osm_id")))
    st.caption("🔴 must-visit · 🟠 on the way · 🔵 added in this step · ⚪ other found places")
    st.dataframe(
        [
            {"#": i, "name": s.get("name"), "role": s["role"], "why": s.get("why")}
            for i, s in enumerate(order, start=1)
        ],
        hide_index=True,
    )


def render_search(search: dict[str, Any] | None, key: str = "chat") -> None:
    """Show a `search` object from /chat: counts, the Overpass query, the results, the trip."""
    if not search:
        return
    places = search.get("places") or []
    if search.get("error"):
        st.warning(f"Enough information, but the search failed: {search['error']}")
    else:
        st.success(
            f"✅ Enough information: Overpass returned {search.get('fetched', '?')} elements, "
            f"{search['total']} distinct places, showing {len(places)}"
        )
    if search.get("query"):
        attempts = search.get("query_attempts", 1)
        fixes = (
            f" (after {attempts - 1} syntax fix{'es' if attempts > 2 else ''})"
            if attempts > 1
            else ""
        )
        st.markdown(f"**Generated Overpass query**{fixes}")
        st.code(search["query"], language="sql")
    if places:
        st.markdown(f"**Results ({len(places)})**")
        rows = [place_row(p) for p in places]
        st.dataframe(
            rows,
            hide_index=True,
            column_config={
                "website": st.column_config.LinkColumn("website"),
                "wikipedia": st.column_config.LinkColumn("wikipedia", display_text="open"),
            },
        )
        render_route(search, key)
        if not search.get("routes"):
            points = [{"lat": r["lat"], "lon": r["lon"]} for r in rows if r["lat"] and r["lon"]]
            if points:
                st.map(points, size=30)
    with st.expander("Summary sent to find_places and trip"):
        st.info(search["summary"])
        st.json(search["trip"])


def event_row(event: dict[str, Any]) -> dict[str, Any]:
    """One table row for an event from `events.events`."""
    runs = event.get("occurrences") or []
    first = runs[0]["start"][:16].replace("T", " ") if runs else ""
    more = event.get("occurrences_in_range", len(runs)) - 1
    return {
        "title": event.get("title"),
        "when": f"{first} (+{more} more)" if more > 0 else first,
        "venue": ", ".join(event.get("venues") or []),
        "address": ", ".join(event.get("addresses") or []),
        "category": ", ".join(event.get("categories") or []),
        "link": event.get("url"),
    }


def render_events(events: dict[str, Any] | None) -> None:
    """Show an `events` object from /chat: the dates asked for, and the events found."""
    if not events:
        return
    found = events.get("events") or []
    if events.get("error"):
        st.warning(f"Event lookup failed: {events['error']}")
        return
    st.success(
        f"📅 Events in {events['city']}, {events['from_date']} to {events['to_date']}: "
        f"showing {len(found)} of {events['total']}"
    )
    if found:
        st.dataframe(
            [event_row(e) for e in found],
            hide_index=True,
            column_config={"link": st.column_config.LinkColumn("link", display_text="open")},
        )


def render_output(tool: str) -> None:
    result = st.session_state.debug_output.get(tool)
    if result is None or result["response"] is None:
        return
    response = result["response"]
    if tool == "summarize":
        st.info(response.get("summary", ""))
    else:
        if response.get("search"):
            render_search(response["search"], key=tool)
        elif not response.get("events"):
            st.warning("No search this turn: Betty needs more information")
        render_events(response.get("events"))
        st.write(f"**Betty's reply:** {response.get('reply', '')}")
    with st.expander("Request sent"):
        st.json(result["request"])
    with st.expander("Raw response"):
        st.json(response)


if st.sidebar.button("Reset conversation"):
    st.session_state.messages = []
    st.session_state.search = None
    st.session_state.events = None
    st.session_state.debug_output = {}
    st.session_state.chat_routes = {}
    st.rerun()

left, right = st.columns(2, gap="large")

# ---- left: chat
with left:
    header = st.container()
    messages_box = st.container(height=CHAT_HEIGHT)
    status_box = st.container()
    prompt = st.chat_input("Where would you like to go?")

with messages_box:
    with st.chat_message("assistant"):
        st.write(GREETING)
    for index, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            st.write(message["content"])
            if index in st.session_state.chat_routes:
                render_chat_route(st.session_state.chat_routes[index])

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with messages_box:
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant"), st.spinner("Betty is typing..."):
            data = post("/api/v1/chat", {"messages": st.session_state.messages})
    if data:
        if data.get("search"):
            st.session_state.search = data["search"]
            if data["search"].get("routes"):
                index = len(st.session_state.messages)  # where Betty's reply is appended
                st.session_state.chat_routes[index] = data["search"]["routes"][0]
        if data.get("events"):
            st.session_state.events = data["events"]
        st.session_state.messages.append({"role": "assistant", "content": data["reply"]})
        st.rerun()  # redraw so the header's copy button and the right side see the new turn
    st.session_state.messages.pop()  # drop the unanswered turn so the user can retry

with header:
    title_col, copy_col = st.columns([1, 2.5], vertical_alignment="center")
    title_col.subheader("🧭 Betty")
    with copy_col:
        copy_button(json.dumps(st.session_state.messages, indent=2, ensure_ascii=False))

with status_box:
    render_search(st.session_state.search)
    render_events(st.session_state.events)

# ---- right: functions
with right:
    st.subheader("🔧 Functions")
    st.caption(
        'Input: request JSON `{"messages": [{"role": "user"|"assistant", '
        '"content": "..."}]}`, or plain text (sent as one user message). '
        "Search check calls `/api/v1/chat` (one Betty turn); with chat history it uses the latest "
        "user turn. Summarize with chat history also sends the last search's `trip`."
    )
    columns = st.columns(len(FUNCTIONS), gap="medium")
    for column, (tool, (title, path)) in zip(columns, FUNCTIONS.items(), strict=True):
        with column:
            st.markdown(f"**{title}**  \n`{path}`")
            text = st.text_area(
                f"Input for {title}", key=f"input_{tool}", height=200, label_visibility="collapsed"
            )
            if st.button("Run with input", key=f"run_input_{tool}", width="stretch"):
                run_function(tool, parse_input(text))
            if st.button(
                "Run with chat history",
                key=f"run_history_{tool}",
                type="primary",
                width="stretch",
                disabled=not st.session_state.messages,
            ):
                run_function(tool, history_request_body(tool))
            render_output(tool)
