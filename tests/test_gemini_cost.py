"""Gemini 비용 어림(estimate_cost_usd) — 모델별 단가, 오디오 단가, 생각 토큰."""
import pytest

import soccer_highlights as sh


def test_price_lookup_by_longest_prefix():
    assert sh.gemini_price("gemini-2.5-flash-lite") == sh.GEMINI_PRICES["gemini-2.5-flash-lite"]
    assert sh.gemini_price("gemini-2.5-flash") == sh.GEMINI_PRICES["gemini-2.5-flash"]
    assert sh.gemini_price("gemini-3.1-flash-lite-preview") == sh.GEMINI_PRICES["gemini-3.1-flash-lite"]
    assert sh.gemini_price("unknown-model") == sh.GEMINI_PRICES["gemini-2.5-flash"]


def test_cost_counts_thoughts_and_audio():
    # 2026-11-25 실측: 2.5 Flash 16초 클립 1개 = 입력 5,863(오디오 512) / 답변 36 / 생각 711
    usage = {"in": 5863, "audio": 512, "out": 36, "thoughts": 711}
    expected = ((5863 - 512) * 0.30 + 512 * 1.00 + (36 + 711) * 2.50) / 1e6
    assert sh.estimate_cost_usd(usage, "gemini-2.5-flash") == pytest.approx(expected)


def test_cost_accepts_old_usage_shape():
    # 예전 상태 파일·목(mock)은 in/out만 가진다
    assert sh.estimate_cost_usd({"in": 1_000_000, "out": 0}, "gemini-3.1-flash-lite") == pytest.approx(0.25)
