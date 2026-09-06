"""피치 캘리브레이션: 키포인트 -> 호모그래피(이미지 px -> 피치 cm).

카메라 팬 대응:
  - 키포인트가 충분하면(>= min_keypoints) 프레임마다 재계산.
  - 부족하면 직전 유효 H를 옵티컬 플로우로 전파, 그마저 실패 시 직전 H 재사용.

전파/재사용은 오차가 누적되므로 두 겹의 안전장치를 둔다(2026-07-20 실측에서
좌표의 25%가 피치 밖으로 튀고 일부는 km 단위로 폭주한 원인):
  1. 타당성 검사 — 이미지 네 귀퉁이를 피치로 보냈을 때 나오는 영역이
     상식적인 범위인지(면적·위치) 확인하고, 아니면 그 H를 버린다.
  2. 스테일 한도 — 키포인트 없이 전파/재사용으로 버틴 프레임이
     max_stale를 넘으면 None을 반환해 좌표 생성을 중단한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .config import PITCH_LENGTH_CM, PITCH_WIDTH_CM


"""검증 임계값.

주의 — 이미지 '네 귀퉁이'로 검증하면 안 된다. XbotGo처럼 낮게 설치된 카메라는
화면 위쪽이 하늘·건물(지면 평면 밖)이라, 지면 호모그래피로 귀퉁이를 보내면
정상적인 변환에서도 반드시 터무니없는 좌표가 나온다. 실제로 그렇게 검증했더니
선수의 92%를 피치 안에 제대로 올려놓는 올바른 호모그래피까지 탈락시켜,
프레임의 69%가 전파(드리프트)로 밀려났다(2026-08-01 진단).

대신 두 가지로 검증한다:
  - 키포인트로 추정한 경우: 그 키포인트를 되쏘아 실제 피치 좌표와의
    재투영 오차를 잰다(자기 근거에 대한 직접 검증).
  - 전파로 얻은 경우: 지면이 확실히 찍히는 화면 하단 영역만 골라
    피치 근처로 가는지 확인한다.
"""
MAX_REPROJ_ERR_CM = 1000.0   # 키포인트 재투영 오차 중앙값 상한(10m)
_GROUND_BAND = (0.45, 0.95)  # 지면으로 간주할 화면 세로 구간(비율)


def _finite_transform(m: np.ndarray, pts: np.ndarray) -> np.ndarray | None:
    try:
        out = cv2.perspectiveTransform(
            np.asarray(pts, dtype=np.float32).reshape(-1, 1, 2), m).reshape(-1, 2)
    except cv2.error:
        return None
    return out if np.all(np.isfinite(out)) else None


def _reproj_error_cm(m: np.ndarray, src_px: np.ndarray, dst_cm: np.ndarray) -> float:
    """검출 키포인트를 변환해 알려진 피치 좌표와의 오차 중앙값(cm)."""
    pred = _finite_transform(m, src_px)
    if pred is None:
        return float("inf")
    return float(np.median(np.linalg.norm(pred - dst_cm, axis=1)))


def _ground_maps_plausibly(m: np.ndarray | None, frame_shape: tuple[int, int]) -> bool:
    """화면 하단(지면) 격자점이 피치 근처로 가는지 확인한다.

    키포인트가 없어 재투영 검증을 못 하는 전파 호모그래피용 안전망. 귀퉁이가
    아니라 지면이 확실한 영역만 보므로 정상 변환을 탈락시키지 않는다.
    """
    if m is None or not np.all(np.isfinite(m)):
        return False
    h, w = frame_shape
    ys = np.linspace(h * _GROUND_BAND[0], h * _GROUND_BAND[1], 4)
    xs = np.linspace(w * 0.1, w * 0.9, 4)
    grid = np.array([[x, y] for y in ys for x in xs], dtype=np.float32)
    pts = _finite_transform(m, grid)
    if pts is None:
        return False
    # 지면 격자의 과반이 피치를 크게 벗어나지 않아야 한다
    near = ((np.abs(pts[:, 0]) <= PITCH_LENGTH_CM * 2)
            & (np.abs(pts[:, 1]) <= PITCH_WIDTH_CM * 2))
    return bool(near.mean() >= 0.5)


class ViewTransformer:
    """평면 호모그래피로 점 집합을 소스->타깃 좌표로 변환."""

    def __init__(self, source: np.ndarray, target: np.ndarray, reproj_thresh: float = 8.0):
        source = np.asarray(source, dtype=np.float32)
        target = np.asarray(target, dtype=np.float32)
        if source.shape != target.shape or source.shape[0] < 4:
            raise ValueError("source/target must be matching (>=4, 2) arrays")
        self.m, _ = cv2.findHomography(source, target, cv2.RANSAC, reproj_thresh)
        if self.m is None:
            raise ValueError("homography estimation failed")

    def transform_points(self, points: np.ndarray) -> np.ndarray:
        points = np.asarray(points, dtype=np.float32)
        if points.size == 0:
            return points.reshape(-1, 2)
        reshaped = points.reshape(-1, 1, 2)
        out = cv2.perspectiveTransform(reshaped, self.m)
        return out.reshape(-1, 2)


@dataclass
class _State:
    m: np.ndarray | None = None            # 마지막 유효 이미지->피치 호모그래피
    prev_gray: np.ndarray | None = None
    prev_feats: np.ndarray | None = None   # 옵티컬 플로우 추적용 특징점(px)
    stale: int = 0                         # 키포인트 없이 버틴 연속 프레임 수


class HomographyEstimator:
    """프레임별 이미지->피치(cm) 호모그래피를 관리한다.

    target_vertices: (K,2) 피치 키포인트의 cm 좌표. 키포인트 검출 모델의
    출력 순서와 동일해야 한다(roboflow SoccerPitchConfiguration.vertices).
    """

    def __init__(
        self,
        target_vertices: np.ndarray,
        keypoint_conf: float = 0.5,
        min_keypoints: int = 4,
        reproj_thresh: float = 8.0,
        max_stale: int = 45,
    ):
        self.target = np.asarray(target_vertices, dtype=np.float32)
        self.keypoint_conf = keypoint_conf
        self.min_keypoints = min_keypoints
        self.reproj_thresh = reproj_thresh
        self.max_stale = max_stale
        self._st = _State()
        # 진단용 카운터 — 파이프라인이 로그로 노출한다
        self.n_keypoint = 0    # 키포인트로 직접 추정한 프레임
        self.n_propagated = 0  # 옵티컬플로우 전파로 버틴 프레임
        self.n_dropped = 0     # 타당성 검사/스테일 한도로 좌표를 포기한 프레임

    def update(
        self,
        frame_bgr: np.ndarray,
        keypoints_xy: np.ndarray | None,
        keypoints_conf: np.ndarray | None,
    ) -> np.ndarray | None:
        """이 프레임의 이미지->피치 호모그래피(3x3) 또는 None 반환."""
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        shape = frame_bgr.shape[:2]
        m: np.ndarray | None = None
        from_keypoints = False

        # 1) 키포인트로 직접 추정 — 재투영 오차로 검증
        if keypoints_xy is not None and keypoints_conf is not None:
            kp = np.asarray(keypoints_xy, dtype=np.float32).reshape(-1, 2)
            cf = np.asarray(keypoints_conf, dtype=np.float32).reshape(-1)
            n = min(len(kp), len(cf), len(self.target))
            mask = cf[:n] >= self.keypoint_conf
            if mask.sum() >= self.min_keypoints:
                src = kp[:n][mask]
                dst = self.target[:n][mask]
                cand, _ = cv2.findHomography(src, dst, cv2.RANSAC, self.reproj_thresh)
                if cand is not None and _reproj_error_cm(cand, src, dst) <= MAX_REPROJ_ERR_CM:
                    m, from_keypoints = cand, True

        # 2) 키포인트 부족 -> 옵티컬 플로우로 직전 H 전파 (지면 격자로 검증)
        if m is None and self._st.m is not None and self._st.prev_gray is not None:
            cand = self._propagate(gray)
            if _ground_maps_plausibly(cand, shape):
                m = cand

        # 3) 그래도 없으면 직전 H 재사용 (이미 타당성 통과한 H)
        if m is None:
            m = self._st.m

        # 스테일 한도: 키포인트 없이 너무 오래 버티면 좌표를 만들지 않는다.
        # 팬 촬영(XbotGo)에서 오래된 H는 실제 화면과 무관해지기 때문.
        if from_keypoints:
            self._st.stale = 0
            self.n_keypoint += 1
        elif m is not None:
            self._st.stale += 1
            if self._st.stale > self.max_stale:
                m = None
                self.n_dropped += 1
            else:
                self.n_propagated += 1
        else:
            self.n_dropped += 1

        # 상태 갱신: 다음 프레임 전파용 특징점 재추출
        if m is not None:
            self._st.m = m
        self._st.prev_gray = gray
        self._st.prev_feats = cv2.goodFeaturesToTrack(
            gray, maxCorners=200, qualityLevel=0.01, minDistance=12
        )
        return m

    def stats(self) -> dict:
        total = self.n_keypoint + self.n_propagated + self.n_dropped
        return {
            "keypoint": self.n_keypoint,
            "propagated": self.n_propagated,
            "dropped": self.n_dropped,
            "keypoint_ratio": (self.n_keypoint / total) if total else 0.0,
        }

    def _propagate(self, gray: np.ndarray) -> np.ndarray | None:
        feats = self._st.prev_feats
        if feats is None or len(feats) < 8:
            return None
        cur, status, _ = cv2.calcOpticalFlowPyrLK(self._st.prev_gray, gray, feats, None)
        if cur is None:
            return None
        status = status.reshape(-1).astype(bool)
        prev_ok = feats.reshape(-1, 2)[status]
        cur_ok = cur.reshape(-1, 2)[status]
        if len(cur_ok) < 8:
            return None
        # cur -> prev 이미지 호모그래피
        inc, _ = cv2.findHomography(cur_ok, prev_ok, cv2.RANSAC, 3.0)
        if inc is None:
            return None
        # 이미지_t -> 이미지_{t-1} -> 피치
        return self._st.m @ inc

    @staticmethod
    def transform(m: np.ndarray, points_px: np.ndarray) -> np.ndarray:
        """이미지 px 점들을 피치 cm로 변환."""
        points_px = np.asarray(points_px, dtype=np.float32)
        if points_px.size == 0:
            return points_px.reshape(-1, 2)
        out = cv2.perspectiveTransform(points_px.reshape(-1, 1, 2), m)
        return out.reshape(-1, 2)


def soccer_pitch_vertices_cm() -> np.ndarray:
    """roboflow/sports SoccerPitchConfiguration.vertices(cm)를 지연 로드.

    실제 필드 키포인트 모델(football-field-detection)과 순서/스케일이 일치.
    'sports' 패키지 미설치 시 ImportError를 명확히 안내.
    """
    try:
        from sports.configs.soccer import SoccerPitchConfiguration
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "실제 피치 캘리브레이션에는 roboflow 'sports' 패키지가 필요합니다.\n"
            "  uv pip install 'git+https://github.com/roboflow/sports.git'"
        ) from e
    cfg = SoccerPitchConfiguration()
    return np.asarray(cfg.vertices, dtype=np.float32)
