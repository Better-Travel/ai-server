"""The query-writing LLM client: bounded timeout and retries (no network)."""

import pytest

from osm.llm import DEFAULT_TIMEOUT_SECONDS, make_clients


@pytest.fixture(autouse=True)
def keys_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("osm.llm.load_dotenv", lambda: None)  # ignore a local .env
    monkeypatch.setenv("NVIDIA_API_KEY_1", "key-1")
    monkeypatch.setenv("NVIDIA_API_KEY_2", "key-2")
    monkeypatch.delenv("REQUEST_TIMEOUT_SECONDS", raising=False)


def test_clients_have_a_bounded_timeout_and_one_retry() -> None:
    clients = make_clients()
    assert len(clients) == 2
    for client in clients:
        assert client.timeout == DEFAULT_TIMEOUT_SECONDS == 60.0
        assert client.max_retries == 1


def test_timeout_follows_request_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "15")
    assert [client.timeout for client in make_clients()] == [15.0, 15.0]
