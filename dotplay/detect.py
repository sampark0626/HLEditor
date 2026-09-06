"""객체 검출: 선수/골키퍼/심판/공 + 피치 키포인트.

두 백엔드 지원:
  - ultralytics: 로컬 .pt 가중치 (오프라인, 이 프로젝트 기본)
  - roboflow:    REST API 직접 호출(roboflow_client) + 호스팅 모델 ID (API 키 필요)
                 (`inference` 패키지는 Python 3.14 휠이 없어 REST로 우회)
무거운 import(torch/ultralytics)는 실제 로드 시점까지 지연한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import supervision as sv

from . import roboflow_client

# roboflow/sports football-players-detection 클래스 규약
BALL_ID = 0
GOALKEEPER_ID = 1
PLAYER_ID = 2
REFEREE_ID = 3

CLASS_NAME = {
    BALL_ID: "ball",
    GOALKEEPER_ID: "goalkeeper",
    PLAYER_ID: "player",
    REFEREE_ID: "referee",
}


class PlayerDetector:
    """선수/골키퍼/심판/공 검출기."""

    def __init__(
        self,
        weights: str | None = None,
        model_id: str | None = None,
        api_key: str | None = None,
        device: str = "cpu",
    ):
        self.device = device
        self._backend = None
        self._model = None
        if weights:
            self._backend = "ultralytics"
            from ultralytics import YOLO
            self._model = YOLO(weights)
        elif model_id:
            self._backend = "roboflow"
            self._model_id = model_id
            self._api_key = api_key
        else:
            raise ValueError("weights(로컬) 또는 model_id(roboflow) 중 하나가 필요합니다.")

    def detect(self, frame_bgr: np.ndarray, conf: float = 0.3) -> sv.Detections:
        if self._backend == "ultralytics":
            res = self._model.predict(
                frame_bgr, conf=conf, device=self.device, verbose=False
            )[0]
            return sv.Detections.from_ultralytics(res)
        data = roboflow_client.infer(frame_bgr, self._model_id, self._api_key, confidence=conf)
        return sv.Detections.from_inference(data)


class FieldDetector:
    """피치 키포인트(라인 교차점 등) 검출기. 호모그래피용."""

    def __init__(
        self,
        weights: str | None = None,
        model_id: str | None = None,
        api_key: str | None = None,
        device: str = "cpu",
    ):
        self.device = device
        self._backend = None
        self._model = None
        if weights:
            self._backend = "ultralytics"
            from ultralytics import YOLO
            self._model = YOLO(weights)
        elif model_id:
            self._backend = "roboflow"
            self._model_id = model_id
            self._api_key = api_key
        else:
            raise ValueError("weights 또는 model_id 중 하나가 필요합니다.")

    def detect(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(keypoints_xy (K,2), conf (K,)) 반환."""
        if self._backend == "ultralytics":
            res = self._model.predict(frame_bgr, device=self.device, verbose=False)[0]
            kp = res.keypoints
            if kp is None or kp.xy is None or len(kp.xy) == 0:
                return np.zeros((0, 2), np.float32), np.zeros((0,), np.float32)
            xy = kp.xy[0].cpu().numpy().astype(np.float32)
            cf = (
                kp.conf[0].cpu().numpy().astype(np.float32)
                if kp.conf is not None else np.ones(len(xy), np.float32)
            )
            return xy, cf
        # roboflow
        data = roboflow_client.infer(frame_bgr, self._model_id, self._api_key)
        kps = sv.KeyPoints.from_inference(data)
        if len(kps.xy) == 0:
            return np.zeros((0, 2), np.float32), np.zeros((0,), np.float32)
        xy = kps.xy[0].astype(np.float32)
        cf = (
            kps.confidence[0].astype(np.float32)
            if kps.confidence is not None else np.ones(len(xy), np.float32)
        )
        return xy, cf


@dataclass
class SplitDetections:
    players: sv.Detections   # player + goalkeeper
    goalkeepers: sv.Detections
    referees: sv.Detections
    ball: sv.Detections


def split_detections(det: sv.Detections) -> SplitDetections:
    """클래스별로 검출을 분리."""
    cid = det.class_id if det.class_id is not None else np.array([], dtype=int)
    return SplitDetections(
        players=det[np.isin(cid, [PLAYER_ID, GOALKEEPER_ID])],
        goalkeepers=det[cid == GOALKEEPER_ID],
        referees=det[cid == REFEREE_ID],
        ball=det[cid == BALL_ID],
    )
