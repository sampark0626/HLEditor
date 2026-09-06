"""다중 객체 추적: supervision ByteTrack 래퍼."""
from __future__ import annotations

import numpy as np
import supervision as sv

from .config import PipelineConfig


class Tracker:
    def __init__(self, cfg: PipelineConfig, fps: float):
        self._bt = sv.ByteTrack(
            track_activation_threshold=cfg.track_activation_threshold,
            lost_track_buffer=cfg.lost_track_buffer,
            minimum_matching_threshold=cfg.minimum_matching_threshold,
            frame_rate=int(round(fps)),
        )

    def update(self, det: sv.Detections) -> sv.Detections:
        return self._bt.update_with_detections(det)


def foot_points(det: sv.Detections) -> np.ndarray:
    """각 검출의 발 위치(바운딩박스 하단 중앙) px 좌표 (N,2)."""
    if det.xyxy is None or len(det.xyxy) == 0:
        return np.zeros((0, 2), np.float32)
    xyxy = det.xyxy
    x = (xyxy[:, 0] + xyxy[:, 2]) / 2.0
    y = xyxy[:, 3]  # 하단
    return np.stack([x, y], axis=1).astype(np.float32)


def center_points(det: sv.Detections) -> np.ndarray:
    """각 검출의 중심 px 좌표 (N,2). (공 등)"""
    if det.xyxy is None or len(det.xyxy) == 0:
        return np.zeros((0, 2), np.float32)
    xyxy = det.xyxy
    x = (xyxy[:, 0] + xyxy[:, 2]) / 2.0
    y = (xyxy[:, 1] + xyxy[:, 3]) / 2.0
    return np.stack([x, y], axis=1).astype(np.float32)
