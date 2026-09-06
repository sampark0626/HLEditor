"""FM 스타일 2D dot-play 영상 렌더링."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pandas as pd
from tqdm import tqdm

from .config import PipelineConfig
from . import pitch as pitchmod


def _color_for(row, cfg: PipelineConfig) -> tuple[int, int, int]:
    p = cfg.palette
    cls = row.get("cls", "player")
    if cls == "referee":
        return p.referee
    if cls == "goalkeeper":
        return p.goalkeeper
    return p.team_a if int(row.get("team", 0)) == 0 else p.team_b


def render_video(
    df: pd.DataFrame,
    fps: float,
    cfg: PipelineConfig,
    out_path: str | Path,
    ball_df: pd.DataFrame | None = None,
    frame_range: tuple[int, int] | None = None,
) -> Path:
    """스무딩된 좌표 테이블을 dot-play mp4로 렌더링한다.

    df: columns=[frame, track_id, team, cls, x, y] (cm)
    ball_df: columns=[frame, x, y] (선택)
    frame_range: (fmin, fmax) — 지정 시 좌표 유무와 무관하게 이 범위를 전부
        렌더링한다. PiP 합성처럼 출력 길이를 다른 영상과 정확히 맞춰야 할 때
        사용(좌표가 없는 프레임은 빈 피치가 그려짐).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base = pitchmod.draw_pitch(cfg.render_scale, cfg.render_padding, cfg.palette)
    h, w = base.shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter를 열 수 없습니다: {out_path}")

    if df.empty and frame_range is None:
        writer.release()
        return out_path

    if frame_range is not None:
        fmin, fmax = int(frame_range[0]), int(frame_range[1])
    else:
        fmin, fmax = int(df["frame"].min()), int(df["frame"].max())
    by_frame = {f: g for f, g in df.groupby("frame")} if not df.empty else {}
    ball_by_frame = (
        {f: g for f, g in ball_df.groupby("frame")} if ball_df is not None else {}
    )

    # 잔상용 최근 프레임 버퍼
    for f in tqdm(range(fmin, fmax + 1), desc="렌더링", unit="frame"):
        img = base.copy()

        # FM 잔상: 직전 trail_length 프레임을 옅게
        for k in range(cfg.trail_length, 0, -1):
            pf = f - k
            g = by_frame.get(pf)
            if g is None:
                continue
            alpha = cfg.trail_alpha * (1.0 - k / (cfg.trail_length + 1))
            if alpha <= 0.02:
                continue
            overlay = img.copy()
            for _, row in g.iterrows():
                color = _color_for(row, cfg)
                pitchmod.draw_points(
                    overlay, np.array([[row["x"], row["y"]]]), color,
                    max(3, cfg.dot_radius - 4), cfg.render_scale, cfg.render_padding,
                    edge=None,
                )
            cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)

        # 현재 프레임 점
        g = by_frame.get(f)
        if g is not None:
            for _, row in g.iterrows():
                color = _color_for(row, cfg)
                label = str(int(row["track_id"])) if cfg.show_track_id else None
                pitchmod.draw_points(
                    img, np.array([[row["x"], row["y"]]]), color,
                    cfg.dot_radius, cfg.render_scale, cfg.render_padding,
                    labels=[label] if label else None,
                )

        # 공
        bg = ball_by_frame.get(f)
        if bg is not None and len(bg):
            pts = bg[["x", "y"]].to_numpy()
            pitchmod.draw_points(
                img, pts, cfg.palette.ball, cfg.ball_radius,
                cfg.render_scale, cfg.render_padding, edge=cfg.palette.ball_edge,
            )

        writer.write(img)

    writer.release()
    return out_path
