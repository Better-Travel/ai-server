"""API tests using a fake OpenAI client (no network)."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_betty_service
from app.services.betty_service import BettyService


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        text = "Sounds lovely!\n\nWhere to?"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.fixture
def fake() -> FakeCompletions:
    return FakeCompletions()


@pytest.fixture
def client(fake: FakeCompletions) -> Iterator[TestClient]:
    openai_like = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    settings = Settings(nvidia_api_key_2="x", betty_model="test-model", _env_file=None)
    service = BettyService(settings, client=openai_like)  # type: ignore[arg-type]
    app.dependency_overrides[get_betty_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_chat(client: TestClient, fake: FakeCompletions) -> None:
    r = client.post(
        "/api/v1/chat", json={"messages": [{"role": "user", "content": "I want to visit Warsaw"}]}
    )
    assert r.status_code == 200
    assert r.json()["reply"].startswith("Sounds lovely")
    sent = fake.calls[0]
    assert sent["model"] == "test-model"
    assert sent["messages"][0]["role"] == "system"


def test_chat_rejects_assistant_last(client: TestClient) -> None:
    r = client.post("/api/v1/chat", json={"messages": [{"role": "assistant", "content": "Hi"}]})
    assert r.status_code == 422


def test_summarize_is_single_paragraph(client: TestClient) -> None:
    r = client.post(
        "/api/v1/summarize", json={"messages": [{"role": "user", "content": "Warsaw, 3 days"}]}
    )
    assert r.status_code == 200
    assert "\n" not in r.json()["summary"]


def test_missing_api_key_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    app.dependency_overrides.clear()
    from app.config import get_settings
    from app.main import _build_service

    _build_service.cache_clear()
    get_settings.cache_clear()
    monkeypatch.delenv("NVIDIA_API_KEY_2", raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    try:
        r = TestClient(app).post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        assert r.status_code == 503
    finally:
        get_settings.cache_clear()
        _build_service.cache_clear()
