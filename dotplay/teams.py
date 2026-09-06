"""팀 분류: 유니폼 컬러 기반 (SigLIP 임베딩 + UMAP + KMeans).

roboflow/sports의 TeamClassifier를 지연 로드해 사용한다.
프레임 단위가 아닌 '트랙 단위 다수결'로 최종 팀을 확정해 안정화한다.
"""
from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np


def crop_boxes(frame_bgr: np.ndarray, xyxy: np.ndarray) -> list[np.ndarray]:
    """바운딩박스 영역들을 잘라 이미지 리스트로 반환(빈 박스는 제외)."""
    crops = []
    h, w = frame_bgr.shape[:2]
    for x1, y1, x2, y2 in xyxy.astype(int):
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 - x1 < 3 or y2 - y1 < 3:
            crops.append(None)
            continue
        crops.append(frame_bgr[y1:y2, x1:x2])
    return crops


class TeamClassifier:
    """SigLIP 기반 2팀 분류기 래퍼."""

    def __init__(self, device: str = "cpu"):
        try:
            from sports.common.team import TeamClassifier as _TC
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "팀 분류에는 roboflow 'sports' 패키지가 필요합니다.\n"
                "  uv pip install 'git+https://github.com/roboflow/sports.git'"
            ) from e
        self._clf = _TC(device=device)
        self._fitted = False

    def fit(self, crops: list[np.ndarray]) -> None:
        crops = [c for c in crops if c is not None and c.size > 0]
        if len(crops) < 8:
            raise ValueError(f"팀 분류기 학습에 크롭이 부족합니다: {len(crops)}")
        self._clf.fit(crops)
        self._fitted = True

    def predict(self, crops: list[np.ndarray]) -> list[int | None]:
        """각 크롭의 팀 라벨(0/1). None 크롭은 None 반환."""
        if not self._fitted:
            raise RuntimeError("fit()을 먼저 호출하세요.")
        valid_idx = [i for i, c in enumerate(crops) if c is not None and c.size > 0]
        out: list[int | None] = [None] * len(crops)
        if not valid_idx:
            return out
        preds = self._clf.predict([crops[i] for i in valid_idx])
        for i, p in zip(valid_idx, preds):
            out[i] = int(p)
        return out


class TrackTeamVoter:
    """트랙별 팀 라벨 다수결 누적기."""

    def __init__(self):
        self._votes: dict[int, Counter] = defaultdict(Counter)

    def add(self, track_id: int, team: int | None) -> None:
        if team is not None:
            self._votes[int(track_id)][int(team)] += 1

    def resolve(self) -> dict[int, int]:
        """트랙별 최종 팀(다수결). 표가 없으면 0."""
        return {
            tid: (c.most_common(1)[0][0] if c else 0)
            for tid, c in self._votes.items()
        }
