# Big City Life — Betty AI server

FastAPI service for **Betty**, a friendly local-guide assistant that gathers trip details and
finds real places for the trip in OpenStreetMap. Uses the OpenAI SDK against NVIDIA's Integrate
API.

## Setup

```bash
uv sync
cp .env.example .env   # set NVIDIA_API_KEY_2
uv run uvicorn app.main:app --reload
```

`BETTY_MODEL` (default `nemotron-3.5`) selects the model.

## Local web UI (Streamlit)

With the API running, in a second terminal:

```bash
uv run streamlit run ui/streamlit_app.py
```

Requests wait up to 600 s (`BETTY_API_TIMEOUT`), since a turn that searches often takes 1-3
minutes. The API URL defaults to `http://localhost:8000` (override with `BETTY_API_URL` or the sidebar
field).

The page has two columns. **Left:** the chat with Betty, with a small **Copy** button next to her
name that copies the history as JSON, and the last search (summary sent to `find_places`, places,
route, trip) under the chat. The route map has a **Build step** slider that replays how the route
was built (`routes[0].steps`): the LLM's must-visits first (red), then each place added on the way
(orange; the one added at that step in blue, with its detour), then the final reorder. **Right:** the two API functions side by side (Summarize, Search check). Each has an input box (request JSON, a bare message list, or plain text sent as
one user message) and two buttons: **Run with input** and **Run with chat history**, which uses the
live conversation. The output appears under each function, with the request sent and the raw
response. (Search check calls `/api/v1/chat`, so with chat history it re-runs the latest user turn;
Summarize with chat history also sends the last search's `trip`.)

## Logging

The API logs to the console (`LOG_LEVEL`, default `INFO`): incoming requests, every LLM response
text and tool call, tool results, the generated Overpass queries, and the final reply.
`LOG_LEVEL=DEBUG` also logs the full request payload sent to the model. Logs include user text,
so keep them local.

## How Betty finds places

```
traveller <-> Betty --search_places(trip)--> validate trip --> summarize(history, trip) --> find_places(summary)
                ^                                                                               |
                +----------------------------- places (max 30) --------------------------------+
```

1. **`search_places`** (`app/services/search_places_tool.py`) is Betty's only tool, and calling it
   is her "I have enough information" signal. She passes only the confirmed minimum, validated by
   `Trip` in `app/schemas.py`: one **city or district** with its ISO country code, and the
   **timing** (dates or a number of days). Regions/countries, past dates and durations that
   contradict the dates are rejected, and the problems go back to Betty so she asks the
   traveller. The goal is that every search covers one bounded area.
2. **Summary** (`BettyService.summarize`): the server summarises the whole conversation. The
   first sentence is built by code from the validated trip (`Trip.describe()`), so destination
   and dates can't drift; the model only adds what the traveller stated (interests, companions,
   budget, free hours, needs). Plain English, one paragraph, unknowns left out. If the summary
   call fails, the trip sentence alone is used.
3. **`find_places`** (`app/services/place_finder.py` + `osm/`) takes that plain text: an LLM
   turns it into an Overpass QL query (tags limited to the catalog in `osm/schemas.py`),
   deterministic code runs it and sends syntax errors back to the model to fix. If the text
   doesn't say where, the model replies `NO_QUERY: ...` (`needs_info`). Duplicates are dropped
   first: elements that share a `wikidata` tag, or have the same name less than 100 m apart (the
   Royal Castle is mapped as a building and as a museum point), keep only the one with more tags.
   At most 30 sights and attractions go back to Betty and the client (food and drink are excluded): Wikipedia-tagged ones first, then a mix of kinds;
   cropping logs a warning (interim). The prompt also tells the model to leave memorial plaques
   and stolpersteine out of `historic=memorial`, since Warsaw has thousands.
4. **Route** (`app/services/route_planner.py`): one LLM call (same model and keys as the query
   writer) picks 2-5 must-visit places for this traveller and scores the others 1-3 (`why` for
   each). Code orders the must-visits into the shortest open path (nearest neighbour from every
   start + 2-opt, straight-line distances), then adds the scored places that lie on the way, best
   score per metre of detour first: at most 800 m extra each (relaxed until the route has 5
   stops), a repeated category halves a place's value, 10 stops max. If the LLM fails, the first
   3 places in find_places order are the must-visits. Places without coordinates are skipped.
5. Betty gets one result envelope (`ok` / `rejected` / `needs_info` / `error`, with `route` = the
   stops' names and reasons in order) and suggests only returned places.

- The query-writing model needs `NVIDIA_API_KEY_1` (falls back to `NVIDIA_API_KEY_2`);
  `NVIDIA_MODEL` selects it. Each call times out after `REQUEST_TIMEOUT_SECONDS` (default 60)
  and is retried once. `BETTY_PLACES_TOOL=false` skips the OSM search (the trip is still
  validated and summarised).
- Places whose OSM `wikipedia=lang:Title` tag is set are looked up on Wikipedia by that exact
  title (`osm/wikipedia.py`, batched per language, redirects followed, disambiguation and missing
  pages skipped). Each gets a short summary + URL in **Polish and English** (`wikipedia.pl` / `wikipedia.en`;
  the other language comes from Wikipedia's language links, and is absent if no translation exists). Lookup failures are
  ignored; the place is returned without it. No API key needed.
- **Local Overpass (Mazowieckie, includes Warsaw):** `docker compose -f compose.overpass.yaml up -d`
  (the first start downloads and indexes the extract; watch `docker logs -f overpass-mazowieckie`
  and check `curl localhost:12345/api/status`), then set
  `OVERPASS_URLS=http://localhost:12345/api/interpreter` in `.env`. Places outside Mazowieckie
  return empty results there. Without `OVERPASS_URLS` the public servers are used.
- Try `find_places` alone: `uv run python -m osm sample_request.txt` or
  `uv run python -m osm "Coffee and castles in Kraków, Poland"`

## Events on the trip's dates (BetterTravel backend)

When Betty runs `search_places` for a trip with **specific dates** (a start date, plus an end
date or a number of days) in Warsaw, the server also asks our backend
(`backend-server`, `GET /api/better-travel/warsaw/events?from=&to=`) for events on those dates
(`app/services/event_finder.py`). There is no separate tool for Betty to call, and a trip with
only a number of days has no dates, so nothing is looked up. Other cities are skipped, because
the backend only covers Warsaw. A start date alone is one day, and long trips use their first
14 days.

- The backend returns every run of an event (often a whole year), so only runs overlapping the
  dates are kept, in Warsaw local time. The finder ranks them (dated, categorised events first,
  Kultura / Czas wolny / Sport before residents' services such as health or business, taking
  turns between categories; events already running before the dates are flagged `ongoing` and
  come last) and keeps 15 candidates. The model then picks the **at most 5** that fit the
  traveller's summary best (`EVENT_PICK_SYSTEM_PROMPT`); if that call fails or its answer is
  unusable, the first 5 ranked candidates are used.
- The frontend gets those events as structured data in `events` (`city`, `from_date`,
  `to_date`, `total` = all events on the dates, `events` = at most 5, `error`), shown under the
  chat in the Streamlit UI. Betty sees the same 5 as short examples; she says she found a few,
  names up to two and asks whether the traveller is interested in any (they are on screen).
  Failures never break the chat: `events.error` is set and Betty says nothing about events.
- `BACKEND_URL` (default `http://localhost:8080`; start it with `docker compose up` in
  `backend-server`). `BETTY_EVENTS_TOOL=false` turns the lookup off.

## Sight narration and the guide (interruptions)

For a traveller standing at a sight, the server provides a fixed narration and answers questions
asked while it is spoken. The **client reads the text aloud and keeps the cursor**; the server is
stateless.

1. `GET /api/v1/narration?lang=en&title=Warsaw Barbican` returns the Wikipedia intro cleaned
   for speech (pronunciation guides, citation marks and foreign-name parentheses removed) as
   numbered `segments` of whole sentences (each up to about 250 characters, never across a
   paragraph), plus the licence `attribution` to show or speak. It is the same text for every
   user. 404: no such article (or a disambiguation page); 422: bad language.
2. While segment `n` is being spoken the traveller asks something:
   `POST /api/v1/narration/ask` with `{place: {lang, title}, cursor: n, question, history}`
   (`history` = earlier questions and answers at this place; `cursor` = the number of segments
   if the narration had already finished). The response is
   `{answer, used_web, sources, resume: {from_segment, bridge}, finished}`. The client speaks the
   answer, then `bridge` (a short phrase in the narration's language, for example "Back to
   Warsaw Barbican."), then continues from `resume.from_segment` (the interrupted segment, so
   no sentence is cut off). After the end `resume` is `null`. Each question is independent, so
   interrupting several times works.
3. The guide (`app/services/guide_service.py`) is shown the last 3 segments before the cursor
   (the interrupted one marked) and the whole Wikipedia article, and answers in plain spoken
   prose in the language of the question, with the length fitted to the question (no fixed limit). It only goes to the web, through a
   `search_web` tool backed by Tavily (`app/services/web_search.py`, at most
   `GUIDE_MAX_SEARCHES` = 2 searches, results treated as untrusted text), when the article does
   not contain the answer; without `TAVILY_API_KEY` it answers from the article only. It uses
   `GUIDE_MODEL` (default `BETTY_MODEL`) on the NVIDIA API and no output cap.
4. The Streamlit page has a **Guide** panel that stands in for the audio client: load a place,
   step through the segments, interrupt with a question and see the answer, sources and where
   the narration resumes.

Answers can take a while: the NVIDIA endpoint is slow (tens of seconds per model call), and a
question that needs a web search makes two calls. Clients should play a short filler while waiting.

## Endpoints

The server is stateless: send the full history on each call.

- `POST /api/v1/chat`: `{"messages": [{"role": "user"|"assistant", "content": "..."}]}` →
  `{"reply": "...", "search": null | {...}}`. `search` is set only on a turn where Betty had enough
  information and searched:
  `{"trip": {city, district, country, scope, start_date, end_date, duration_days}, "summary": "<text sent to find_places>", "total": 57, "places": [{osm_id, name, type, category, lat, lon, tags, wikipedia}], "routes": [{"stops": [{osm_id, name, category, lat, lon, role, why}], "distance_km": 3.4}], "error": null}`.
  `error` is set when the trip was accepted but the search failed. Keep the last `search` on the
  client; Betty searches again when the trip changes.
  - `osm_id` is the stable OSM id (`"node/123"`, `"way/456"`): use it to dedupe, save favourites
    or refresh a place. `category` is the place's kind as one tag (`"tourism=museum"`, or
    `"other"`). `total` counts distinct places, after duplicates are dropped.
  - `routes` holds at most one route for now (a list so alternatives can be added). Draw
    `stops` in order; `role` is `"must"` or `"on_the_way"`, `why` is a short reason (null in
    the fallback). `distance_km` is straight-line, so real walking is longer.
  - `tags` keeps only: `amenity`, `tourism`, `historic`, `leisure`, `cuisine`, `opening_hours`,
    `website`, `contact:website`, `phone`, `contact:phone`, `name:en`, `addr:street`,
    `addr:housenumber`, `addr:postcode`, `wikidata`, `wheelchair`, `diet:vegetarian`,
    `diet:vegan`, `fee`. Any of them may be missing.
- `POST /api/v1/chat/stream`: same request; the reply streams as Server-Sent Events: `start`
  (`turn_id`), then `delta` (text), `status` (`searching_places`), `search` and `events` as they
  happen, then one `done` (the `/chat` response body) or one `error` (`code`, `message`,
  `retryable`). A `: ping` comment is sent after 15 s without output. Details for clients:
  [docs/mobile-chat-api.md](docs/mobile-chat-api.md).
- `POST /api/v1/summarize`: `{"messages": [...], "trip": {...}?}` → `{"summary": "..."}`. The same
  function `search_places` uses; with `trip` the first sentence is `Trip.describe()`.
- `GET /health`

Mobile app integration: [docs/mobile-chat-api.md](docs/mobile-chat-api.md) for the contract (what
the server does today), [docs/mobile-integration-plan.md](docs/mobile-integration-plan.md) for the gap
analysis and plan.

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app tests ui osm && uv run pytest
```
