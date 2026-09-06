"""2D 버드뷰 피치 렌더러 (OpenCV, 외부 의존 없음).

좌표계: cm 단위, 원점(0,0)=피치 좌상단 코너.
  x: 길이 방향 0..PITCH_LENGTH_CM (골라인=0/L, 하프라인=L/2)
  y: 폭  방향 0..PITCH_WIDTH_CM
roboflow/sports SoccerPitchConfiguration과 동일한 cm 스케일이라
호모그래피 결과 좌표를 그대로 넣을 수 있다.
"""
from __future__ import annotations

import cv2
import numpy as np

from .config import PITCH_LENGTH_CM, PITCH_WIDTH_CM, Palette

# 표준 마킹(cm) — 12000x7000 모델 피치에 맞춘 절대값
_PENALTY_BOX_DEPTH = 1650
_PENALTY_BOX_WIDTH = 4032
_GOAL_BOX_DEPTH = 550
_GOAL_BOX_WIDTH = 1832
_CENTER_CIRCLE_R = 915
_PENALTY_SPOT_DIST = 1100
_PENALTY_ARC_R = 915
_CORNER_ARC_R = 100


def _cm_to_px(x_cm: float, y_cm: float, scale: float, pad: int) -> tuple[int, int]:
    return int(round(pad + x_cm * scale)), int(round(pad + y_cm * scale))


def canvas_size(scale: float, pad: int) -> tuple[int, int]:
    """(height, width) 픽셀."""
    w = int(round(PITCH_LENGTH_CM * scale)) + 2 * pad
    h = int(round(PITCH_WIDTH_CM * scale)) + 2 * pad
    return h, w


def draw_pitch(scale: float, pad: int, palette: Palette | None = None) -> np.ndarray:
    """빈 피치 베이스 이미지를 생성한다."""
    palette = palette or Palette()
    h, w = canvas_size(scale, pad)
    img = np.full((h, w, 3), palette.field, dtype=np.uint8)
    line = palette.line
    cy = PITCH_WIDTH_CM / 2.0
    t = max(1, int(round(2.0 * scale * 100)))  # 라인 두께 ~ scale 비례

    def pt(x, y):
        return _cm_to_px(x, y, scale, pad)

    # 외곽 경계
    cv2.rectangle(img, pt(0, 0), pt(PITCH_LENGTH_CM, PITCH_WIDTH_CM), line, t)
    # 하프라인
    cv2.line(img, pt(PITCH_LENGTH_CM / 2, 0), pt(PITCH_LENGTH_CM / 2, PITCH_WIDTH_CM), line, t)
    # 센터 서클 + 스팟
    cv2.circle(img, pt(PITCH_LENGTH_CM / 2, cy), int(round(_CENTER_CIRCLE_R * scale)), line, t)
    cv2.circle(img, pt(PITCH_LENGTH_CM / 2, cy), max(2, t + 1), line, -1)

    for goal_x, direction in ((0, 1), (PITCH_LENGTH_CM, -1)):
        # 페널티 박스
        cv2.rectangle(
            img,
            pt(goal_x, cy - _PENALTY_BOX_WIDTH / 2),
            pt(goal_x + direction * _PENALTY_BOX_DEPTH, cy + _PENALTY_BOX_WIDTH / 2),
            line, t,
        )
        # 골 박스
        cv2.rectangle(
            img,
            pt(goal_x, cy - _GOAL_BOX_WIDTH / 2),
            pt(goal_x + direction * _GOAL_BOX_DEPTH, cy + _GOAL_BOX_WIDTH / 2),
            line, t,
        )
        # 페널티 스팟
        spot_x = goal_x + direction * _PENALTY_SPOT_DIST
        cv2.circle(img, pt(spot_x, cy), max(2, t + 1), line, -1)
        # 페널티 아크 (박스 밖 부분만)
        arc_center = pt(spot_x, cy)
        arc_r = int(round(_PENALTY_ARC_R * scale))
        box_edge_x = goal_x + direction * _PENALTY_BOX_DEPTH
        # 아크가 박스 라인과 만나는 각도
        dx = abs(box_edge_x - spot_x) / _PENALTY_ARC_R
        dx = min(1.0, dx)
        theta = np.degrees(np.arccos(dx))
        if direction == 1:
            cv2.ellipse(img, arc_center, (arc_r, arc_r), 0, -theta, theta, line, t)
        else:
            cv2.ellipse(img, arc_center, (arc_r, arc_r), 180, -theta, theta, line, t)

    return img


def draw_points(
    img: np.ndarray,
    points_cm: np.ndarray,
    color: tuple[int, int, int],
    radius: int,
    scale: float,
    pad: int,
    edge: tuple[int, int, int] | None = (20, 20, 20),
    labels: list[str] | None = None,
) -> np.ndarray:
    """피치 위에 점(cm 좌표)들을 그린다. img를 in-place 수정하고 반환."""
    if points_cm is None or len(points_cm) == 0:
        return img
    for i, (x, y) in enumerate(points_cm):
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        px, py = _cm_to_px(float(x), float(y), scale, pad)
        cv2.circle(img, (px, py), radius, color, -1, lineType=cv2.LINE_AA)
        if edge is not None:
            cv2.circle(img, (px, py), radius, edge, max(1, radius // 7), lineType=cv2.LINE_AA)
        if labels is not None and i < len(labels) and labels[i]:
            cv2.putText(
                img, str(labels[i]), (px - radius // 2, py + radius // 3),
                cv2.FONT_HERSHEY_SIMPLEX, radius / 30.0, (255, 255, 255), 1, cv2.LINE_AA,
            )
    return img
