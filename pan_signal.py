#!/usr/bin/env python3
"""
pan_signal.py — XbotGo 팬(pan) 궤적 기반 보조 신호.

XbotGo는 공/액션을 따라 좌우로 계속 패닝하는 카메라다. 카메라의 팬 움직임
자체가 "액션이 어느 진영에 있는지"를 알려주므로, 이를 오디오·비전에 이은
3차 신호로 쓴다 (2026-07-11 실측 검증: 팬 극단 정지=골문 앞, 빠른 스위프=역습).

파이프라인:
    저해상 그레이 프레임 추출(ffmpeg) → 프레임별 세로평균 프로파일
    → 인접 프레임 1-D phase correlation → 수평 팬 속도 → 누적합 = 팬 위치

오디오 검출(detect_spikes_from_signal)과 같은 구조로, 신호 처리부는
순수 함수(pan_series_from_frames)로 분리해 합성 프레임으로 유닛 테스트한다.

보수적 설계: 팬 신호가 하이라이트를 지지할 때만 신뢰도를 올린다(승격 전용).
감점은 하지 않는다 — 실측에서 "골대 앞 세트피스"와 "종료 함성"의 팬 상태가
구분되지 않는 사례가 있었고, 기각은 어차피 Gemini가 프레임을 보고 한다.
"""

import subprocess
from pathlib import Path

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

# ── 조절 파라미터 ────────────────────────────────────────────
PAN_W, PAN_H = 192, 108   # 분석용 저해상도 (원본 화면비 16:9 유지)
PAN_FPS = 10              # 분석 프레임레이트 — 팬 속도 추정엔 충분
PAN_WIN_SEC = 3.0         # peak 주변 팬 상태 판단 윈도 (±초)
PAN_EXTREME = 0.55        # 정규화 위치 |pos| 이 값 이상 = 골대 진영
PAN_DWELL_SPEED = 8.0     # 평균 |속도| px/s 이 값 이하 = 카메라 정지(dwell)
PAN_FAST_SPEED = 35.0     # 평균 |속도| px/s 이 값 이상 = 빠른 스위프(역습)
PAN_BONUS = 0.10          # 지지 상태일 때 신뢰도 가산치 (승격 전용)
MIN_RANGE_PX = 40.0       # 전체 팬 이동폭이 이보다 작으면 사실상 고정 카메라 → 신호 무효
MIN_MEDIAN_CORR = 0.5     # phase correlation 피크 중앙값 게이트 — 낮으면 추정 불신

# 후보에 기록되는 팬 상태와 사람이 읽는 라벨 (CLI/웹 UI 공용)
STATE_LABELS = {
    "goal_end_dwell": "골문 앞 정지",
    "fast_sweep": "빠른 전개",
    "center_idle": "중앙 정지",
    "moving": "이동 중",
}
BOOST_STATES = ("goal_end_dwell", "fast_sweep")


def _run(cmd):
    subprocess.run(cmd, check=True)


def extract_gray_frames(video, workdir, run=None):
    """영상에서 저해상 그레이스케일 프레임 배열 (n, H, W) uint8을 추출한다.

    run: ffmpeg 실행 함수 (soccer_highlights.run 주입 가능 — WinError 5 재시도 재사용).
    """
    run = run or _run
    raw = Path(workdir) / "pan_gray.raw"
    run(["ffmpeg", "-y", "-i", str(video),
         "-vf", f"fps={PAN_FPS},scale={PAN_W}:{PAN_H}",
         "-pix_fmt", "gray", "-f", "rawvideo", str(raw), "-loglevel", "error"])
    data = np.fromfile(raw, dtype=np.uint8)
    raw.unlink(missing_ok=True)  # 30분 영상이면 ~370MB — 읽었으면 바로 정리
    n = len(data) // (PAN_W * PAN_H)
    return data[: n * PAN_W * PAN_H].reshape(n, PAN_H, PAN_W)


def pan_series_from_frames(frames, fps=PAN_FPS):
    """순수 신호 처리부: 프레임 배열에서 팬 속도/위치 시계열을 계산한다.

    frames: (n, H, W) 배열. ffmpeg/파일 I/O가 없어 합성 프레임으로 테스트하기 쉽다.
    반환 dict:
        t        (n,)   초 단위 시각
        vel      (n-1,) 수평 팬 속도 px/s (부호는 좌/우 — 절대 기준 아님, 상대 방향만 의미)
        pos      (n,)   정규화 팬 위치 [-1, 1] (1%/99% 백분위 기준, 약간의 초과 허용)
        corr     (n-1,) phase correlation 피크 (0~1, 추정 신뢰도)
        range_px float   전체 팬 이동폭 (백분위 기준, 원시 px)
        reliable bool    이 시계열을 신호로 써도 되는지
    """
    n = len(frames)
    t = np.arange(n) / fps
    if n < int(fps * 2):  # 2초 미만이면 판단 불가
        return dict(t=t, vel=np.zeros(max(n - 1, 0)), pos=np.zeros(n),
                    corr=np.zeros(max(n - 1, 0)), range_px=0.0, reliable=False)

    # 세로 평균 프로파일 — 수평 이동만 남기고 세로 잡음(선수 이동 등)을 뭉갠다
    prof = frames.mean(axis=1)
    prof = prof - prof.mean(axis=1, keepdims=True)

    # 인접 프레임 1-D phase correlation → 프레임당 수평 shift
    w = prof.shape[1]
    fft = np.fft.rfft(prof, axis=1)
    cross = fft[:-1] * np.conj(fft[1:])
    cross /= np.abs(cross) + 1e-9
    corr = np.fft.irfft(cross, n=w, axis=1)
    shift = corr.argmax(axis=1).astype(int)
    shift[shift > w // 2] -= w  # wrap-around → 부호 있는 shift
    peak = corr.max(axis=1)

    # 이상치 제거(median) 후 평활(moving average) → px/s
    vel = median_filter(shift.astype(float), size=5)
    vel = uniform_filter1d(vel, size=9) * fps

    # 누적합 = 팬 위치. 백분위로 정규화해 [-1, 1] 스케일로
    pos_px = np.concatenate([[0.0], np.cumsum(vel / fps)])
    p_lo, p_hi = np.percentile(pos_px, 1), np.percentile(pos_px, 99)
    range_px = float(p_hi - p_lo)
    if range_px > 1e-6:
        pos = np.clip((pos_px - p_lo) / range_px * 2 - 1, -1.2, 1.2)
    else:
        pos = np.zeros(n)

    reliable = range_px >= MIN_RANGE_PX and float(np.median(peak)) >= MIN_MEDIAN_CORR
    return dict(t=t, vel=vel, pos=pos, corr=peak, range_px=range_px, reliable=reliable)


def compute_pan_series(video, workdir, run=None):
    """영상에서 팬 시계열을 계산한다 (extract + pan_series_from_frames)."""
    frames = extract_gray_frames(video, workdir, run=run)
    return pan_series_from_frames(frames)


def pan_state_at(series, peak_sec, win_sec=PAN_WIN_SEC):
    """peak 시각 주변의 팬 상태를 판정해 dict(pos, speed, speed_max, state)를 반환한다.

    정지(dwell)는 윈도 평균 속도로, 스위프는 윈도 내 순간 최고 속도로 판정한다 —
    역습 스위프는 짧고 급격해서 ±3초 평균으로는 묻힌다 (2_2경기 38.2s 실측).
    """
    t, pos, vel = series["t"], series["pos"], series["vel"]
    i = int(np.clip(np.searchsorted(t, peak_sec), 0, len(pos) - 1))
    p = float(pos[i])
    m = (t[:-1] > peak_sec - win_sec) & (t[:-1] < peak_sec + win_sec)
    speed = float(np.abs(vel[m]).mean()) if m.any() else 0.0
    speed_max = float(np.abs(vel[m]).max()) if m.any() else 0.0

    if abs(p) >= PAN_EXTREME and speed <= PAN_DWELL_SPEED:
        state = "goal_end_dwell"   # 골대 진영에서 카메라 정지 → 골문 앞 상황
    elif speed_max >= PAN_FAST_SPEED:
        state = "fast_sweep"       # 빠른 팬 스위프 → 역습/급전개
    elif speed <= PAN_DWELL_SPEED:
        state = "center_idle"      # 중앙 정지 → 킥오프 대기/중단 가능성
    else:
        state = "moving"
    return dict(pos=round(p, 2), speed=round(speed, 1),
                speed_max=round(speed_max, 1), state=state)


def annotate_candidates(cands, series):
    """각 후보에 팬 상태(c["pan"])와 가산치(c["pan_bonus"])를 기록한다.

    시계열이 신뢰 불가(reliable=False)면 아무것도 기록하지 않는다.
    반환: 가산치가 부여된 후보 수.
    """
    if not series.get("reliable"):
        return 0
    boosted = 0
    for c in cands:
        st = pan_state_at(series, float(c["peak"]))
        c["pan"] = st
        c["pan_bonus"] = PAN_BONUS if st["state"] in BOOST_STATES else 0.0
        if c["pan_bonus"]:
            boosted += 1
    return boosted
