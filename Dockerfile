# syntax=docker/dockerfile:1
# Two images from one file:
#   API (default):  docker build -t betty-api .
#   Streamlit UI:   docker build --target ui -t betty-ui .
# Usually both are built and started with docker compose (see compose.yaml and the README).

FROM python:3.13-slim AS deps-api
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies only, from the lock file, so code changes don't re-install them. No dev tools and
# no Streamlit (the `ui` group) in the API image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --no-install-project

FROM deps-api AS deps-ui
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --group ui --no-install-project

FROM python:3.13-slim AS runtime
RUN useradd --create-home --uid 10001 betty
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

FROM runtime AS ui
COPY --from=deps-ui /app/.venv /app/.venv
COPY ui ./ui
USER betty
EXPOSE 8501
CMD ["streamlit", "run", "ui/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]

FROM runtime AS api
COPY --from=deps-api /app/.venv /app/.venv
COPY app ./app
COPY osm ./osm
USER betty
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
# One worker: streamed turns run in threads inside the process; scale with more containers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
