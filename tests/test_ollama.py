from __future__ import annotations

import pytest

from readfellow.ollama import normalize_vector


def test_normalize_vector() -> None:
    assert normalize_vector([3.0, 4.0]) == pytest.approx([0.6, 0.8])


def test_normalize_zero_vector() -> None:
    assert normalize_vector([0.0, 0.0]) == [0.0, 0.0]
