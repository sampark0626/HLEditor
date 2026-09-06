"""soccer_highlights.py의 순수 로직(오디오 분석·구간 선택) 유닛 테스트."""

import numpy as np
import pytest

import soccer_highlights as sh


def _synthetic_signal(sr=16000, duration_sec=40.0, spike_times=(10.0, 25.0)):
    """조용한 배경 노이즈 위에 특정 시각에 큰 볼륨 스파이크를 심은 합성 PCM 신호 생성."""
    n = int(sr * duration_sec)
    rng = np.random.default_rng(42)
    data = rng.normal(0, 200, n).astype(np.float64)  # 조용한 배경 노이즈
    for t in spike_times:
        center = int(t * sr)
        width = int(0.4 * sr)
        lo, hi = max(0, center - width // 2), min(n, center + width // 2)
        data[lo:hi] += rng.normal(0, 8000, hi - lo)  # 스파이크 구간은 진폭 훨씬 큼
    return sr, data.astype(np.int16)


class TestDetectSpikesFromSignal:
    def test_detects_spikes_near_expected_times(self):
        sr, data = _synthetic_signal(spike_times=(10.0, 25.0))
        cands = sh.detect_spikes_from_signal(sr, data, percentile=95, min_db=6.0)
        assert len(cands) >= 2
        peaks = sorted(c["peak"] for c in cands)
        # 합성 스파이크 시각(10s, 25s) 근처에서 검출돼야 함 (±1초 허용)
        assert any(abs(p - 10.0) < 1.0 for p in peaks)
        assert any(abs(p - 25.0) < 1.0 for p in peaks)

    def test_quiet_signal_has_no_spikes(self):
        sr, data = _synthetic_signal(spike_times=())
        cands = sh.detect_spikes_from_signal(sr, data, percentile=95, min_db=6.0)
        assert cands == []

    def test_stricter_min_db_reduces_candidates(self):
        sr, data = _synthetic_signal(spike_times=(10.0, 11.5, 25.0))
        loose = sh.detect_spikes_from_signal(sr, data, percentile=90, min_db=3.0)
        strict = sh.detect_spikes_from_signal(sr, data, percentile=99, min_db=20.0)
        assert len(strict) <= len(loose)

    def test_candidate_shape(self):
        sr, data = _synthetic_signal(spike_times=(10.0,))
        cands = sh.detect_spikes_from_signal(sr, data, percentile=95, min_db=6.0)
        assert cands
        c = cands[0]
        assert set(c.keys()) == {"start", "end", "peak", "delta_db"}
        assert c["start"] <= c["peak"] <= c["end"]
        assert c["delta_db"] > 0

    def test_overlapping_candidates_deduped(self):
        # 매우 가까운 두 스파이크(0.3초 간격)는 인접 병합 및 탈중복 대상.
        sr, data = _synthetic_signal(spike_times=(10.0, 10.3))
        cands = sh.detect_spikes_from_signal(
            sr, data, percentile=95, min_db=6.0, pre_sec=8.0, post_sec=5.0)
        # 겹치는 구간은 하나로 합쳐지거나 더 강한 쪽만 남아야 함
        assert len(cands) <= 2


class TestSelectSegments:
    def test_no_vision_returns_all_as_selected(self):
        cands = [{"peak": 1.0}, {"peak": 2.0}]
        selected, maybe = sh.select_segments(cands, conf_auto=0.7, use_vision=False)
        assert selected == cands
        assert maybe == []

    def test_splits_by_confidence_threshold(self):
        cands = [
            {"peak": 1.0, "highlight": True, "confidence": 0.9},   # 자동 채택
            {"peak": 2.0, "highlight": True, "confidence": 0.5},   # 확인필요 (CONF_MAYBE=0.4 이상)
            {"peak": 3.0, "highlight": True, "confidence": 0.1},   # 제외
            {"peak": 4.0, "highlight": False, "confidence": 0.99}, # highlight=False → 제외
        ]
        selected, maybe = sh.select_segments(cands, conf_auto=0.7, use_vision=True)
        assert [c["peak"] for c in selected] == [1.0]
        assert [c["peak"] for c in maybe] == [2.0]

    def test_custom_conf_auto_threshold(self):
        cands = [{"peak": 1.0, "highlight": True, "confidence": 0.55}]
        selected, _ = sh.select_segments(cands, conf_auto=0.5, use_vision=True)
        assert len(selected) == 1
        selected, _ = sh.select_segments(cands, conf_auto=0.6, use_vision=True)
        assert len(selected) == 0


@pytest.mark.parametrize("sensitivity,expected_keys", [
    ("more", {"percentile", "min_db", "label"}),
    ("normal", {"percentile", "min_db", "label"}),
    ("strict", {"percentile", "min_db", "label"}),
])
def test_sensitivity_presets_shape(sensitivity, expected_keys):
    preset = sh.SENSITIVITY_PRESETS[sensitivity]
    assert set(preset.keys()) == expected_keys


def test_sensitivity_presets_ordered_by_strictness():
    # more < normal < strict 순으로 percentile이 커야 한다(더 엄선일수록 후보가 적어짐)
    presets = sh.SENSITIVITY_PRESETS
    assert presets["more"]["percentile"] < presets["normal"]["percentile"] < presets["strict"]["percentile"]
    assert presets["more"]["min_db"] < presets["normal"]["min_db"] < presets["strict"]["min_db"]


# ─── 비전 응답 형태 방어 ──────────────────────────────────────────────────────
# 실제 사고: Gemini가 JSON 배열을 돌려주자 cand.update(res)가
# "dictionary update sequence element #0 has length 4"로 터지면서
# 후보 하나 때문에 30분짜리 잡 전체가 실패했다.
class TestCoerceClassification:
    def test_dict_passes_through(self):
        res = {"highlight": True, "type": "goal", "confidence": 0.9, "reason": "골"}
        assert sh._coerce_classification(res) is res

    def test_object_wrapped_in_array_is_unwrapped(self):
        inner = {"highlight": True, "type": "goal", "confidence": 0.8, "reason": "골"}
        assert sh._coerce_classification([inner]) == inner

    @pytest.mark.parametrize("bad", [
        ["goal", "shot"],   # 문자열 배열 — 실제로 터졌던 형태
        "goal",
        42,
        None,
        [],
    ])
    def test_unusable_shapes_become_safe_parse_error(self, bad):
        out = sh._coerce_classification(bad)
        assert isinstance(out, dict)
        assert out["highlight"] is False
        assert out["confidence"] == 0.0
        assert out["reason"].startswith("parse_error:")

    def test_result_is_always_usable_by_dict_update(self):
        """반환값은 항상 cand.update()에 넣을 수 있어야 한다."""
        cand = {"peak": 10.0}
        cand.update(sh._coerce_classification(["goal", "shot"]))
        assert cand["peak"] == 10.0
        assert cand["type"] == "other"
