"""pan_signal.py의 순수 신호 처리·후보 주석 로직 유닛 테스트 (ffmpeg 실호출 없음)."""

import numpy as np

import jobs
import pan_signal as ps
import soccer_highlights as sh


def make_frames(shifts, w=ps.PAN_W, h=ps.PAN_H, seed=0):
    """프레임 i+1이 프레임 i를 shifts[i]px 만큼 수평 이동한 합성 프레임 시퀀스."""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=w, dtype=np.uint8)  # 수평 패턴 (세로로 동일)
    frames, offset = [], 0
    for s in [0] + list(shifts):
        offset += s
        row = np.roll(base, offset)
        frames.append(np.tile(row, (h, 1)))
    return np.stack(frames)


class TestPanSeries:
    def test_recovers_known_pan_motion(self):
        # 3초 정지 → 3초 동안 프레임당 3px 팬 → 3초 정지 (10fps)
        shifts = [0] * 30 + [3] * 30 + [0] * 29
        series = ps.pan_series_from_frames(make_frames(shifts))
        assert series["reliable"]
        # 팬 구간(중앙)의 평균 |속도| ≈ 3px/frame * 10fps = 30px/s
        mid_speed = np.abs(series["vel"][35:55]).mean()
        assert 20 <= mid_speed <= 40
        # 정지 구간은 거의 0
        assert np.abs(series["vel"][5:25]).mean() < 2
        # 위치는 한쪽 극단에서 시작해 반대쪽 극단으로 (정규화 [-1,1])
        assert abs(series["pos"][5]) > 0.9
        assert abs(series["pos"][-5]) > 0.9
        assert np.sign(series["pos"][5]) != np.sign(series["pos"][-5])

    def test_static_camera_is_unreliable(self):
        # 팬이 전혀 없으면 이동폭 게이트에 걸려 신호 무효
        series = ps.pan_series_from_frames(make_frames([0] * 99))
        assert not series["reliable"]
        assert series["range_px"] < ps.MIN_RANGE_PX

    def test_too_short_clip_is_unreliable(self):
        series = ps.pan_series_from_frames(make_frames([1] * 5))
        assert not series["reliable"]


def fake_series(t_end=100.0, fps=10):
    """pan_state_at 테스트용: 원하는 구간에 위치/속도를 심을 수 있는 시계열 골격."""
    n = int(t_end * fps)
    return dict(t=np.arange(n) / fps, pos=np.zeros(n), vel=np.zeros(n - 1),
                corr=np.ones(n - 1), range_px=200.0, reliable=True)


class TestPanStateAndAnnotate:
    def test_goal_end_dwell_gets_bonus(self):
        series = fake_series()
        series["pos"][:] = -1.0  # 골대 진영 극단에서 정지
        cands = [{"peak": 50.0}]
        assert ps.annotate_candidates(cands, series) == 1
        assert cands[0]["pan"]["state"] == "goal_end_dwell"
        assert cands[0]["pan_bonus"] == ps.PAN_BONUS

    def test_fast_sweep_gets_bonus(self):
        series = fake_series()
        series["vel"][:] = ps.PAN_FAST_SPEED + 10
        cands = [{"peak": 50.0}]
        ps.annotate_candidates(cands, series)
        assert cands[0]["pan"]["state"] == "fast_sweep"
        assert cands[0]["pan_bonus"] == ps.PAN_BONUS

    def test_center_idle_gets_no_bonus(self):
        series = fake_series()  # pos=0, vel=0 → 중앙 정지
        cands = [{"peak": 50.0}]
        assert ps.annotate_candidates(cands, series) == 0
        assert cands[0]["pan"]["state"] == "center_idle"
        assert cands[0]["pan_bonus"] == 0.0

    def test_unreliable_series_annotates_nothing(self):
        series = fake_series()
        series["reliable"] = False
        cands = [{"peak": 50.0}]
        assert ps.annotate_candidates(cands, series) == 0
        assert "pan" not in cands[0]
        assert "pan_bonus" not in cands[0]


class TestEffectiveConfIntegration:
    def test_effective_conf_adds_bonus_capped_at_1(self):
        assert sh.effective_conf({"confidence": 0.65, "pan_bonus": 0.10}) == 0.75
        assert sh.effective_conf({"confidence": 0.65}) == 0.65
        assert sh.effective_conf({"confidence": 0.98, "pan_bonus": 0.10}) == 1.0

    def test_select_segments_promotes_boosted_maybe(self):
        cands = [
            {"peak": 1.0, "highlight": True, "confidence": 0.65, "pan_bonus": 0.10},
            {"peak": 2.0, "highlight": True, "confidence": 0.65},
        ]
        selected, maybe = sh.select_segments(cands, conf_auto=0.70, use_vision=True)
        assert cands[0] in selected   # 0.65 + 0.10 = 0.75 → 승격
        assert cands[1] in maybe      # 보정 없음 → 확인필요 유지

    def test_flag_promotes_boosted_maybe(self):
        c = {"highlight": True, "confidence": 0.65, "pan_bonus": 0.10}
        assert jobs.flag(c, conf_auto=0.70, vision_used=True) == "auto"

    def test_serialize_exposes_pan_fields(self):
        cands = [{"peak": 3.0, "highlight": True, "confidence": 0.65,
                  "pan_bonus": 0.10, "pan": {"pos": -0.9, "speed": 2.0,
                                             "state": "goal_end_dwell"}}]
        out = jobs.serialize_candidates(cands, conf_auto=0.70, vision_used=True)[0]
        assert out["conf_eff"] == 0.75
        assert out["pan_bonus"] == 0.10
        assert out["pan_label"] == ps.STATE_LABELS["goal_end_dwell"]
        assert out["flag"] == "auto"
