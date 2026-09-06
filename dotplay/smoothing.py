"""좌표 후처리: 결측 보간, 속도 기반 이상치 제거, Savitzky-Golay 스무딩.

입력/출력: columns=[frame, track_id, team, cls, x, y] (x,y는 cm) 인 DataFrame.
트랙 단위로 처리한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import savgol_filter

from .config import PITCH_LENGTH_CM, PITCH_WIDTH_CM, PipelineConfig


def constrain_to_pitch(df: pd.DataFrame, cfg: PipelineConfig) -> pd.DataFrame:
    """좌표를 피치 안으로 제한한다 — 화면에는 항상 그라운드 안쪽만 보여야 한다.

    두 단계로 나눈다:
      1. 여유(pitch_margin_ratio)를 크게 벗어난 좌표는 **버린다**. 호모그래피가
         깨진 프레임의 산물이라 위치 자체가 무의미하다(실측에서 km 단위 폭주).
         경계로 끌어당기면 라인 위에 가짜 선수가 줄지어 서게 된다.
      2. 여유 안쪽의 소폭 이탈은 **경계로 클램프**한다. 스로인·골키퍼처럼
         실제로 라인을 살짝 벗어난 경우와 미세 오차를 구분할 수 없고, 이때는
         점을 지우는 것보다 라인 위에 두는 편이 경기 해석에 자연스럽다.
    """
    if df.empty:
        return df
    mx = PITCH_LENGTH_CM * cfg.pitch_margin_ratio
    my = PITCH_WIDTH_CM * cfg.pitch_margin_ratio
    keep = (
        df["x"].between(-mx, PITCH_LENGTH_CM + mx)
        & df["y"].between(-my, PITCH_WIDTH_CM + my)
    )
    out = df[keep].copy()
    if out.empty:
        return out
    out["x"] = out["x"].clip(0.0, float(PITCH_LENGTH_CM))
    out["y"] = out["y"].clip(0.0, float(PITCH_WIDTH_CM))
    return out


def _smooth_track(
    g: pd.DataFrame, fps: float, cfg: PipelineConfig
) -> pd.DataFrame:
    g = g.sort_values("frame").copy()
    frames = g["frame"].to_numpy()
    if len(g) < 3:
        return g

    # 연속 프레임 격자로 재색인 (결측 보간 대상)
    full = np.arange(frames.min(), frames.max() + 1)
    gi = g.set_index("frame").reindex(full)
    # .copy(): pandas>=3.0(Copy-on-Write)에서 .to_numpy()가 읽기전용 배열을
    # 반환할 수 있어, 아래 in-place 대입(outliers 마스킹) 전에 반드시 복사한다.
    x = gi["x"].to_numpy(dtype=float).copy()
    y = gi["y"].to_numpy(dtype=float).copy()

    # 1) 속도 이상치 제거: 이웃 대비 과도 이동을 NaN 처리
    max_step_cm = cfg.max_speed_mps * 100.0 / fps  # 프레임당 최대 이동(cm)
    dx = np.abs(np.diff(x, prepend=x[0]))
    dy = np.abs(np.diff(y, prepend=y[0]))
    step = np.hypot(dx, dy)
    outliers = step > (max_step_cm * 1.5)
    x[outliers] = np.nan
    y[outliers] = np.nan

    # 2) 짧은 결측만 보간(긴 공백은 남겨 둠)
    max_gap = int(round(cfg.max_gap_interp_s * fps))
    x = _interp_short_gaps(x, max_gap)
    y = _interp_short_gaps(y, max_gap)

    # 3) Savitzky-Golay (유효 구간별)
    win = int(round(cfg.savgol_window_s * fps))
    win = max(5, win | 1)  # 홀수, 최소 5
    x = _savgol_valid(x, win, cfg.savgol_polyorder)
    y = _savgol_valid(y, win, cfg.savgol_polyorder)

    out = pd.DataFrame({"frame": full, "x": x, "y": y})
    out["track_id"] = g["track_id"].iloc[0]
    out["team"] = g["team"].iloc[0]
    out["cls"] = g["cls"].iloc[0]
    out = out.dropna(subset=["x", "y"])
    return out[["frame", "track_id", "team", "cls", "x", "y"]]


def _interp_short_gaps(a: np.ndarray, max_gap: int) -> np.ndarray:
    a = a.copy()
    isnan = np.isnan(a)
    if not isnan.any() or isnan.all():
        return a
    idx = np.arange(len(a))
    valid = ~isnan
    # 전체 선형 보간 후, max_gap 초과 공백은 되돌려 NaN
    a_interp = np.interp(idx, idx[valid], a[valid])
    # 긴 공백 마스크
    gap_start = None
    for i in range(len(a)):
        if isnan[i]:
            if gap_start is None:
                gap_start = i
        else:
            if gap_start is not None and (i - gap_start) > max_gap:
                a_interp[gap_start:i] = np.nan
            gap_start = None
    if gap_start is not None and (len(a) - gap_start) > max_gap:
        a_interp[gap_start:] = np.nan
    return a_interp


def _savgol_valid(a: np.ndarray, win: int, poly: int) -> np.ndarray:
    a = a.copy()
    valid = ~np.isnan(a)
    # 연속 유효 세그먼트별로 필터 적용
    i = 0
    n = len(a)
    while i < n:
        if not valid[i]:
            i += 1
            continue
        j = i
        while j < n and valid[j]:
            j += 1
        seg = a[i:j]
        if len(seg) >= win:
            a[i:j] = savgol_filter(seg, win, poly)
        i = j
    return a


def smooth_tracks(df: pd.DataFrame, fps: float, cfg: PipelineConfig) -> pd.DataFrame:
    """전체 좌표 테이블을 트랙 단위로 스무딩.

    스무딩 전에 피치 밖 좌표를 먼저 버린다 — 폭주한 좌표가 남아 있으면
    Savitzky-Golay 창 전체가 오염되어 정상 구간까지 끌려간다.
    """
    if df.empty:
        return df
    df = constrain_to_pitch(df, cfg)
    if df.empty:
        return df
    parts = [
        _smooth_track(g, fps, cfg)
        for _, g in df.groupby("track_id", sort=False)
    ]
    if not parts:
        return df.iloc[0:0]
    out = pd.concat(parts, ignore_index=True).sort_values(["frame", "track_id"])
    # 보간·스무딩이 만들어낸 점이 다시 밖으로 나갈 수 있으므로 한 번 더 제한
    return constrain_to_pitch(out, cfg)
