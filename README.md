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

## Place search tool (OpenStreetMap)

During `/api/v1/chat`, Betty can call a `find_places` tool to look up real restaurants, cafes,
museums, etc. The tool takes one plain-text `request`: a short summary of what the traveller
wants to do, including the destination city and country. `osm/` is a pipeline: an LLM turns that
text into an Overpass QL query (tags are limited to the catalog in `osm/schemas.py`),
deterministic code runs it, and if Overpass rejects the syntax the error is sent back to the model
to fix it. If the text doesn't say where, the model replies `NO_QUERY: ...` and Betty is told what
is missing, so she can ask the traveller (`osm/pipeline.py`). Betty only sees a trimmed result list
(max 20).

- Needs `NVIDIA_API_KEY_1` (falls back to `NVIDIA_API_KEY_2`) for the query-writing model;
  `NVIDIA_MODEL` selects it. `BETTY_PLACES_TOOL=false` disables the tool.
- Places whose OSM `wikipedia=lang:Title` tag is set are looked up on Wikipedia by that exact
  title (`osm/wikipedia.py`, batched per language, redirects followed, disambiguation and missing
  pages skipped). Each gets a short summary + URL in **Polish and English** (`wikipedia.pl` / `wikipedia.en`;
  the other language comes from Wikipedia's language links, and is absent if no translation exists). Lookup failures are
  ignored; the place is returned without it. No API key needed.
- Try the pipeline alone: `uv run python -m osm sample_request.txt` or
  `uv run python -m osm "Coffee and castles in Kraków, Poland"`

## Endpoints

- `POST /api/v1/chat` — `{"messages": [{"role": "user", "content": "..."}]}` → `{"reply": "..."}`
  (stateless: send the full history each call)
- `POST /api/v1/summarize` — same body → `{"summary": "<single paragraph>"}`
- `GET /health`

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app tests ui && uv run pytest
```
