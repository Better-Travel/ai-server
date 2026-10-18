"""The query-writing LLM client: endpoint, key, bounded timeout and retries (no network)."""

import pytest

from osm.llm import (
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    GEMINI_BASE_URL,
    make_clients,
    model_name,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("osm.llm.load_dotenv", lambda: None)  # ignore a local .env
    for name in ("LLM_API_KEY", "GEMINI_API_KEY", "LLM_BASE_URL", "QUERY_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REQUEST_TIMEOUT_SECONDS", raising=False)


def test_defaults_to_gemini_with_a_bounded_timeout_and_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    [client] = make_clients()
    assert str(client.base_url) == GEMINI_BASE_URL
    assert client.api_key == "gem-key"
    assert client.timeout == DEFAULT_TIMEOUT_SECONDS == 60.0
    assert client.max_retries == 1
    assert model_name() == DEFAULT_MODEL == "gemini-3.1-flash-lite"


def test_llm_settings_override_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("LLM_API_KEY", "other-key")  # wins over GEMINI_API_KEY
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1/")
    monkeypatch.setenv("QUERY_MODEL", "some-model")
    [client] = make_clients()
    assert (client.api_key, str(client.base_url)) == ("other-key", "https://llm.example/v1/")
    assert model_name() == "some-model"


def test_timeout_follows_request_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "15")
    assert [client.timeout for client in make_clients()] == [15.0]


def test_a_missing_key_is_reported() -> None:
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        make_clients()
