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

## Endpoints

- `POST /api/v1/chat` — `{"messages": [{"role": "user", "content": "..."}]}` → `{"reply": "..."}`
  (stateless: send the full history each call)
- `POST /api/v1/summarize` — same body → `{"summary": "<single paragraph>"}`
- `GET /health`

## Checks

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy app tests && uv run pytest
```
