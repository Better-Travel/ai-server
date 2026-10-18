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
    betty_places_tool: bool = Field(
        default=True, description="Run the OpenStreetMap search inside search_places."
    )
    betty_max_tool_rounds: int = Field(default=3, gt=0)
    log_level: str = Field(default="INFO", description="DEBUG, INFO, WARNING, ...")
    request_timeout_seconds: float = Field(default=60.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return Settings()
