from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion

from readfellow.models import DerivationSettings
from readfellow.openai_compat import OpenAICompatError, OpenAICompatGenerator


class FakeClient:
    def __init__(self, *, reasoning_content=None, reasoning_tokens=None):
        message = {"role": "assistant", "content": '{"summary":"正文"}'}
        if reasoning_content is not None:
            message["reasoning_content"] = reasoning_content
        payload = {
            "id": "test-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "cloud-test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        }
        if reasoning_tokens is not None:
            payload["usage"] = {
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
                "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
            }
        self.response = ChatCompletion.model_validate(payload)
        self.kwargs = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_generation_sends_schema_and_sampling_settings():
    client = FakeClient()
    settings = DerivationSettings(
        temperature=0.6,
        top_p=0.8,
        presence_penalty=0.5,
        num_predict=512,
        top_k=30,
        min_p=0.1,
        repeat_penalty=1.1,
    )
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    generator = OpenAICompatGenerator(
        base_url="https://example.test/v1",
        model="cloud-test",
        settings=settings,
        client=client,
    )

    assert generator.generate_json("原文", schema) == '{"summary":"正文"}'
    assert client.kwargs == {
        "model": "cloud-test",
        "messages": [{"role": "user", "content": "原文"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "readfellow", "strict": True, "schema": schema},
        },
        "temperature": 0.6,
        "top_p": 0.8,
        "presence_penalty": 0.5,
        "max_tokens": 512,
        "extra_body": {
            "top_k": 30,
            "min_p": 0.1,
            "repetition_penalty": 1.1,
            "enable_thinking": False,
        },
    }


@pytest.mark.parametrize(
    "response_fields",
    [{"reasoning_content": "思考内容"}, {"reasoning_tokens": 1}],
)
def test_generation_rejects_thinking(response_fields):
    generator = OpenAICompatGenerator(
        base_url="https://example.test/v1",
        model="cloud-test",
        settings=DerivationSettings(),
        client=FakeClient(**response_fields),
    )

    with pytest.raises(OpenAICompatError, match="thinking was not disabled"):
        generator.generate_json("原文", {"type": "object"})


@pytest.mark.parametrize("has_dotenv", [False, True])
def test_api_key_from_dotenv_or_missing(tmp_path, monkeypatch, has_dotenv):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    if has_dotenv:
        (tmp_path / ".env").write_text("LLM_API_KEY=fake-key\n", encoding="utf-8")
        generator = OpenAICompatGenerator(
            base_url="https://example.test/v1",
            model="cloud-test",
            settings=DerivationSettings(),
        )
        try:
            assert generator.client.api_key == "fake-key"
        finally:
            generator.client.close()
    else:
        with pytest.raises(
            OpenAICompatError, match=r"set LLM_API_KEY in the environment or \.env"
        ):
            OpenAICompatGenerator(
                base_url="https://example.test/v1",
                model="cloud-test",
                settings=DerivationSettings(),
            )
