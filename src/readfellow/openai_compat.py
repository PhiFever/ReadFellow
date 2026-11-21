from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock
from typing import Any

from openai import OpenAI, RateLimitError

from .config import read_secret
from .models import DerivationSettings

API_KEY_ENV = "LLM_API_KEY"


class OpenAICompatError(RuntimeError):
    pass


class GenerationRejected(OpenAICompatError):
    """A terminal response or exhausted HTTP retry budget."""


class OpenAICompatGenerator:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        settings: DerivationSettings,
        response_format: str = "json_schema",
        requests_per_minute: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        client: OpenAI | None = None,
    ) -> None:
        if client is None:
            api_key = read_secret(API_KEY_ENV)
            if not api_key:
                raise OpenAICompatError(f"set {API_KEY_ENV} in the environment or .env")
            client = OpenAI(
                base_url=base_url, api_key=api_key, timeout=600, max_retries=0
            )
        self.sleep = sleep
        self.interval = 60 / requests_per_minute if requests_per_minute else 0
        self.next_request = 0.0
        self.lock = Lock()
        self.client = client
        self.model = model
        self.settings = settings
        self.response_format = response_format

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> str:
        settings = self.settings
        if self.response_format == "json_object":
            response_format = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "readfellow", "strict": True, "schema": schema},
            }
        for attempt in range(16):
            if self.interval:
                with self.lock:
                    delay = self.next_request - time.monotonic()
                    if delay > 0:
                        self.sleep(delay)
                    self.next_request = time.monotonic() + self.interval
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format=response_format,
                    temperature=settings.temperature,
                    top_p=settings.top_p,
                    max_tokens=settings.num_predict,
                )
                break
            except RateLimitError as exc:
                if attempt == 15:
                    raise GenerationRejected("rate limit retries exhausted") from exc
                header = exc.response.headers.get("retry-after")
                self.sleep(
                    float(header) if header is not None else min(2 ** (attempt + 1), 30)
                )
        message = response.choices[0].message
        details = response.usage.completion_tokens_details if response.usage else None
        if (message.model_extra or {}).get("reasoning_content") or (
            details and details.reasoning_tokens
        ):
            raise GenerationRejected(
                "response contains reasoning output; thinking was not disabled"
            )
        if not message.content:
            raise OpenAICompatError("response has no content")
        return message.content
