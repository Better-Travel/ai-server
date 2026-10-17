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

The API URL defaults to `http://localhost:8000` (override with `BETTY_API_URL` or the sidebar
field).

The page has two columns. **Left:** the chat with Betty, with a small **Copy** button next to her
name that copies the history as JSON, and the last search (summary sent to `find_places`, places,
trip) under the chat. **Right:** the two API functions side by side (Summarize, Search check). Each has an input box (request JSON, a bare message list, or plain text sent as
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
                +----------------------------- places (max 20) --------------------------------+
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
   doesn't say where, the model replies `NO_QUERY: ...` (`needs_info`). At most 20 places go back to Betty
   and the client: a mix of kinds, Wikipedia-tagged ones first; cropping logs a warning (interim).
4. Betty gets one result envelope (`ok` / `rejected` / `needs_info` / `error`) and suggests only
   returned places.

- The query-writing model needs `NVIDIA_API_KEY_1` (falls back to `NVIDIA_API_KEY_2`);
  `NVIDIA_MODEL` selects it. `BETTY_PLACES_TOOL=false` skips the OSM search (the trip is still
  validated and summarised).
- Places whose OSM `wikipedia=lang:Title` tag is set are looked up on Wikipedia by that exact
  title (`osm/wikipedia.py`, batched per language, redirects followed, disambiguation and missing
  pages skipped). Each gets a short summary + URL in **Polish and English** (`wikipedia.pl` / `wikipedia.en`;
  the other language comes from Wikipedia's language links, and is absent if no translation exists). Lookup failures are
  ignored; the place is returned without it. No API key needed.
- Try `find_places` alone: `uv run python -m osm sample_request.txt` or
  `uv run python -m osm "Coffee and castles in Kraków, Poland"`

## Endpoints

The server is stateless: send the full history on each call.

- `POST /api/v1/chat`: `{"messages": [{"role": "user"|"assistant", "content": "..."}]}` →
  `{"reply": "...", "search": null | {...}}`. `search` is set only on a turn where Betty had enough
  information and searched:
  `{"trip": {city, district, country, scope, start_date, end_date, duration_days}, "summary": "<text sent to find_places>", "total": 57, "places": [{name, type, lat, lon, tags, wikipedia}], "error": null}`.
  `error` is set when the trip was accepted but the search failed. Keep the last `search` on the
  client; Betty searches again when the trip changes.
- `POST /api/v1/summarize`: `{"messages": [...], "trip": {...}?}` → `{"summary": "..."}`. The same
  function `search_places` uses; with `trip` the first sentence is `Trip.describe()`.
- `GET /health`

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app tests ui osm && uv run pytest
```
