from __future__ import annotations

from typing import Any

from openai import OpenAI

from .config import read_secret
from .models import DerivationSettings

API_KEY_ENV = "LLM_API_KEY"


class OpenAICompatError(RuntimeError):
    pass


class OpenAICompatGenerator:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        settings: DerivationSettings,
        client: OpenAI | None = None,
    ) -> None:
        if client is None:
            api_key = read_secret(API_KEY_ENV)
            if not api_key:
                raise OpenAICompatError(f"set {API_KEY_ENV} in the environment or .env")
            client = OpenAI(base_url=base_url, api_key=api_key, timeout=600)
        self.client = client
        self.model = model
        self.settings = settings

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> str:
        settings = self.settings
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "readfellow", "strict": True, "schema": schema},
            },
            temperature=settings.temperature,
            top_p=settings.top_p,
            presence_penalty=settings.presence_penalty,
            max_tokens=settings.num_predict,
            extra_body={
                "top_k": settings.top_k,
                "min_p": settings.min_p,
                "repetition_penalty": settings.repeat_penalty,
                # b.ai's qwen3.8-flash thinks by default. On 2026-09-14 a thinking
                # chapter took 115-177 s and 5.8k-10k reasoning tokens against ~20 s
                # without, and its lower unanchored share (3.9% vs 8.0%) was not
                # significant (Fisher p=0.18). Hardcoded like Ollama's `think`, and
                # checked below because a compatible server may ignore the flag.
                "enable_thinking": False,
            },
        )
        message = response.choices[0].message
        details = response.usage.completion_tokens_details if response.usage else None
        if (message.model_extra or {}).get("reasoning_content") or (
            details and details.reasoning_tokens
        ):
            raise OpenAICompatError(
                "response contains reasoning output; thinking was not disabled"
            )
        if not message.content:
            raise OpenAICompatError("response has no content")
        return message.content
