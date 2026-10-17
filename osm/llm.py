import os

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"


def model_name() -> str:
    return os.environ.get("NVIDIA_MODEL", DEFAULT_MODEL)


def make_clients() -> list[OpenAI]:
    load_dotenv()
    keys = [os.environ[name] for name in ("NVIDIA_API_KEY_1", "NVIDIA_API_KEY_2") if os.environ.get(name)]
    if not keys:
        raise RuntimeError("NVIDIA_API_KEY_1 is not set (expected in .env)")
    return [OpenAI(base_url=NVIDIA_BASE_URL, api_key=key) for key in keys]


class FallbackClient:
    """Minimal chat-completions wrapper that moves to the next key on 401/403/429."""

    def __init__(self, clients: list[OpenAI]):
        self._clients = clients

    def complete(self, messages: list[dict]) -> str:
        last: Exception | None = None
        for client in self._clients:
            try:
                response = client.chat.completions.create(
                    model=model_name(), messages=messages, temperature=0
                )
                return response.choices[0].message.content or ""
            except APIStatusError as exc:
                if exc.status_code not in (401, 403, 429):
                    raise
                last = exc
        assert last is not None
        raise last
