"""Application configuration loaded from environment variables / `.env`."""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from osm.llm import DEFAULT_MODEL, GEMINI_BASE_URL


class Settings(BaseSettings):
    """Runtime settings. Every field can be overridden through the environment."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "GEMINI_API_KEY"),
        description="API key for the OpenAI-compatible chat endpoint (Gemini by default).",
    )
    llm_base_url: str = Field(
        default=GEMINI_BASE_URL, description="OpenAI-compatible chat endpoint (Gemini by default)."
    )
    betty_model: str = Field(default=DEFAULT_MODEL, description="Chat model name.")
    betty_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    betty_max_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Optional cap on output tokens per model call. Unset means no cap: the model "
        "reasons before it answers and returns nothing if a cap is used up by that reasoning.",
    )
    betty_places_tool: bool = Field(
        default=True, description="Run the OpenStreetMap search inside search_places."
    )
    betty_events_tool: bool = Field(
        default=True, description="Look up events on the trip's dates (Warsaw) when a search runs."
    )
    backend_url: str = Field(
        default="http://localhost:8080", description="Base URL of the BetterTravel backend."
    )
    betty_max_tool_rounds: int = Field(default=3, gt=0)
    guide_model: str | None = Field(
        default=None, description="Model for the sight guide; defaults to BETTY_MODEL."
    )
    guide_max_searches: int = Field(default=2, ge=1, le=5, description="Web searches per answer.")
    tavily_api_key: str | None = Field(
        default=None, description="Tavily key; without it the guide cannot search the web."
    )
    log_level: str = Field(default="INFO", description="DEBUG, INFO, WARNING, ...")
    request_timeout_seconds: float = Field(default=60.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
