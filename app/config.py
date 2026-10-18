"""Application configuration loaded from environment variables / `.env`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Every field can be overridden through the environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    nvidia_api_key_2: str | None = Field(default=None, description="NVIDIA Integrate API key.")
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    betty_model: str = Field(default="nemotron-3.5", description="Chat model name.")
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
    places_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Default variety of found places: 0 = strictly the best known, 1 = random "
        "among the 150 best known. A chat request can override it.",
    )
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
