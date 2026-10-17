# Big City Life — Betty AI server

FastAPI service for **Betty**, a friendly local-guide assistant that gathers trip details
and produces a one-paragraph summary for itinerary generation. Uses the OpenAI SDK against
NVIDIA's Integrate API.

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

Chat with Betty and click **Summarize trip** in the sidebar. The API URL defaults to
`http://localhost:8000` (override with `BETTY_API_URL` or the sidebar field).

The page has two columns. **Left:** the chat with Betty, with a small **Copy** button next to her
name that copies the history as JSON. **Right:** the two API functions side by side (Summarize,
Readiness check). Each has an input box (request JSON, a bare message list, or plain text sent as
one user message) and two buttons: **Run with input** and **Run with chat history**, which uses the
live conversation. The output appears under each function, with the request sent and the raw
response. (The readiness check calls `/api/v1/chat`, so with chat history it re-checks the latest
user turn.)

## Logging

The API logs to the console (`LOG_LEVEL`, default `INFO`): incoming requests, every LLM response
text and tool call, tool results, the generated Overpass queries, and the final reply.
`LOG_LEVEL=DEBUG` also logs the full request payload sent to the model. Logs include user text,
so keep them local.

## Place search tool (OpenStreetMap)

During `/api/v1/chat`, Betty can call a `find_places` tool to look up real restaurants, cafes,
museums, etc. The tool takes a `PlacesInput` (see `osm/schemas.py`: category, tag filters,
`area` + `country`). `osm/` is a pipeline: an LLM writes an Overpass QL query, deterministic code
runs it, and if Overpass rejects the syntax the error is sent back to the model to fix
(`osm/pipeline.py`). Betty only sees a trimmed result list (max 20).

- Needs `NVIDIA_API_KEY_1` (falls back to `NVIDIA_API_KEY_2`) for the query-writing model;
  `NVIDIA_MODEL` selects it. `BETTY_PLACES_TOOL=false` disables the tool.
- Try the pipeline alone: `uv run python -m osm sample_places.json`

## Readiness: when is the info "enough"?

Betty calls the `finalize_trip_context` tool once the hard minimum is met: a destination
narrowed to **one city or district** (not a region/country) and **timing** (dates or a number
of days). Everything else (interests, free hours, companions, budget, notes) is optional.
The tool arguments are validated by `TripContext` in `app/schemas.py`; a rejected call returns
the problems to the model so it asks the traveller for what is missing. The goal is that a later
OpenStreetMap query for this context returns a bounded amount of data. `/api/v1/chat` then
responds with `"ready": true` and the structured `trip_context`.

## Endpoints

- `POST /api/v1/chat` — `{"messages": [{"role": "user", "content": "..."}]}` → `{"reply": "...", "ready": false, "trip_context": null}`
  (stateless: send the full history each call)
- `POST /api/v1/summarize` — same body (optionally plus the `trip_context` from `/chat`) → `{"summary": "<single paragraph>"}`
- `GET /health`

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app tests ui && uv run pytest
```
