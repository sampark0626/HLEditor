"""합성 self-test: ML 모델/실영상 없이 스테이지 4-6(좌표->스무딩->렌더)을 검증.

가상의 22명 선수 + 공이 피치 위를 움직이는 좌표를 생성해
스무딩·렌더 파이프라인을 실제로 실행하고 dot-play mp4/PNG를 저장한다.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from .config import PITCH_LENGTH_CM, PITCH_WIDTH_CM, PipelineConfig
from .smoothing import smooth_tracks
from .render import render_video
from . import pitch as pitchmod


def _synthetic_tracks(n_frames: int = 150, fps: float = 30.0, seed: int = 7):
    rng = np.random.default_rng(seed)
    rows = []
    # 4-4-2 대형 두 팀 (각 11명) + 심판 1
    def formation(team: int):
        xs = ([1500] + [3500] * 4 + [6000] * 4 + [8500] * 2 if team == 0
              else [10500] + [8500] * 4 + [6000] * 4 + [3500] * 2)
        ys = ([3500] + list(np.linspace(1200, 5800, 4)) + list(np.linspace(1200, 5800, 4))
              + [2600, 4400])
        return list(zip(xs, ys))

    players = []
    tid = 1
    for team in (0, 1):
        for (x0, y0) in formation(team):
            players.append((tid, team, "player", float(x0), float(y0)))
            tid += 1
    # 심판
    players.append((tid, 0, "referee", 6000.0, 3500.0))

    # 공: 중앙에서 지그재그
    ball_rows = []

    for f in range(n_frames):
        t = f / fps
        for (pid, team, cls, x0, y0) in players:
            # 대형 유지 + 완만한 이동 + 노이즈
            drift = 900 * np.sin(0.5 * t + pid)
            x = x0 + drift * (0.6 if cls == "player" else 0.2) + rng.normal(0, 40)
            y = y0 + 500 * np.sin(0.7 * t + pid * 0.3) + rng.normal(0, 40)
            x = float(np.clip(x, 100, PITCH_LENGTH_CM - 100))
            y = float(np.clip(y, 100, PITCH_WIDTH_CM - 100))
            rows.append({"frame": f, "track_id": pid, "team": team, "cls": cls, "x": x, "y": y})
        bx = PITCH_LENGTH_CM / 2 + 3500 * np.sin(0.9 * t)
        by = PITCH_WIDTH_CM / 2 + 2000 * np.sin(1.3 * t)
        ball_rows.append({"frame": f, "x": float(bx), "y": float(by)})

    return pd.DataFrame(rows), pd.DataFrame(ball_rows), fps


def run_selftest(out_dir: str | Path = "outputs") -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = PipelineConfig()

    raw, ball, fps = _synthetic_tracks()
    smoothed = smooth_tracks(raw, fps, cfg)

    # 정적 프리뷰 PNG (중간 프레임)
    mid = int(smoothed["frame"].median())
    base = pitchmod.draw_pitch(cfg.render_scale, cfg.render_padding, cfg.palette)
    g = smoothed[smoothed["frame"] == mid]
    for _, r in g.iterrows():
        color = (cfg.palette.team_a if r["cls"] == "player" and r["team"] == 0
                 else cfg.palette.team_b if r["cls"] == "player"
                 else cfg.palette.referee)
        pitchmod.draw_points(base, np.array([[r["x"], r["y"]]]), color,
                             cfg.dot_radius, cfg.render_scale, cfg.render_padding)
    bpng = out_dir / "selftest_preview.png"
    cv2.imwrite(str(bpng), base)

    # dot-play mp4
    mp4 = out_dir / "selftest.mp4"
    render_video(smoothed, fps, cfg, mp4, ball_df=ball)

    coords_out = out_dir / "selftest_coords.parquet"
    smoothed.to_parquet(coords_out)

    return {
        "frames": int(smoothed["frame"].nunique()),
        "tracks": int(smoothed["track_id"].nunique()),
        "rows_raw": len(raw),
        "rows_smoothed": len(smoothed),
        "preview_png": str(bpng),
        "video_mp4": str(mp4),
        "coords_parquet": str(coords_out),
        "video_bytes": mp4.stat().st_size if mp4.exists() else 0,
    }
