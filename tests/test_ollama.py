from __future__ import annotations

import pytest

from readfellow.ollama import OllamaError, normalize_vector, parse_generate_response


def test_normalize_vector() -> None:
    assert normalize_vector([3.0, 4.0]) == pytest.approx([0.6, 0.8])


def test_normalize_zero_vector() -> None:
    assert normalize_vector([0.0, 0.0]) == [0.0, 0.0]


def test_parse_generate_response_accepts_metadata_fields() -> None:
    raw = (
        '{"model":"qwen3:8b","created_at":"2026-07-05T00:00:00Z",'
        '"response":"{\\"entities\\":[]}", "done":true}'
    )

    assert parse_generate_response(raw) == '{"entities":[]}'


def test_parse_generate_response_concatenates_streaming_lines() -> None:
    raw = (
        '{"response":"{\\"entities\\":", "done":false}\n{"response":"[]}", "done":true}'
    )

    assert parse_generate_response(raw) == '{"entities":[]}'


def test_parse_generate_response_raises_ollama_error() -> None:
    with pytest.raises(OllamaError, match="model not found"):
        parse_generate_response('{"error":"model not found"}')
