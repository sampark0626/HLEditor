"""RADAR 파이프라인 오케스트레이션: 영상 -> 좌표 -> dot-play 영상.

fm-dotplay(별도 PoC 저장소)의 pipeline.py를 HLEditor의 job 워커 패턴에 맞게
포팅: tqdm 대신 on_progress 콜백(jobs_dotplay.set_progress와 동일 시그니처),
should_cancel 콜백으로 협조적 취소를 지원한다.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import pandas as pd

from .config import PipelineConfig
from .detect import PlayerDetector, FieldDetector, split_detections, CLASS_NAME, PLAYER_ID
from .track import Tracker, foot_points, center_points
from .teams import TeamClassifier, TrackTeamVoter, crop_boxes
from .homography import HomographyEstimator, soccer_pitch_vertices_cm
from .roboflow_client import RoboflowTransientError
from .smoothing import constrain_to_pitch, smooth_tracks
from .render import render_video

log = logging.getLogger("hl")

OnProgress = Callable[[str, int, int], None]
ShouldCancel = Callable[[], bool]


def _noop_progress(stage: str, done: int, total: int) -> None:
    return None


def _never_cancel() -> bool:
    return False


def _detect_both(pool, player_det, field_det, frame, player_conf):
    """선수 검출과 피치 키포인트 검출을 동시에 호출한다.

    둘 다 roboflow REST 호출(네트워크 대기)이라 GIL과 무관하게 병렬로 돈다.
    순차 호출 시 프레임당 약 4초로 60초 영상에 68분이 걸렸다(2026-07-20 실측).
    """
    f_kp = pool.submit(field_det.detect, frame)
    try:
        det = player_det.detect(frame, conf=player_conf)
        kp_xy, kp_cf = f_kp.result()
    except BaseException:
        f_kp.cancel()
        raise
    return det, kp_xy, kp_cf


class _FrameFailureBudget:
    """프레임 단위 실패를 흡수하되, 진짜 장애면 멈추게 하는 예산 관리자.

    잠깐의 네트워크 끊김으로 수십 분치 분석이 통째로 날아가지 않도록 실패한
    프레임은 건너뛴다(900프레임 중 몇 장 빠져도 결과에 지장 없음). 다만
    연속 실패가 이어지면 회복 불가한 장애이므로 예외를 그대로 올린다.
    실측: 재분석 중 23분 지점에서 ConnectTimeout으로 잡 전체가 실패(2026-08-01).
    """

    def __init__(self, max_consecutive: int = 15, max_total_ratio: float = 0.1):
        self.max_consecutive = max_consecutive
        self.max_total_ratio = max_total_ratio
        self.consecutive = 0
        self.total = 0

    def record_failure(self, exc: Exception, processed: int) -> None:
        self.consecutive += 1
        self.total += 1
        limit_hit = self.consecutive >= self.max_consecutive
        # 초반(표본 부족)엔 비율 판정을 하지 않는다
        if processed >= 50 and self.total > processed * self.max_total_ratio:
            limit_hit = True
        if limit_hit:
            raise RoboflowTransientError(
                f"검출 실패가 계속됩니다(연속 {self.consecutive}회, 누적 {self.total}회). "
                f"네트워크 연결과 Roboflow 상태를 확인한 뒤 다시 시도하세요. 원인: {exc}"
            ) from exc
        log.warning("[dotplay] 프레임 검출 실패 — 건너뜀 (연속 %d, 누적 %d): %s",
                    self.consecutive, self.total, exc)

    def record_success(self) -> None:
        self.consecutive = 0


@dataclass
class ModelSpec:
    player_weights: str | None = None
    field_weights: str | None = None
    player_model_id: str | None = None
    field_model_id: str | None = None
    api_key: str | None = None


@dataclass
class PipelineResult:
    coords: pd.DataFrame
    ball: pd.DataFrame
    fps: float
    video_out: Path | None = None
    cancelled: bool = False


def _open_video(path: str) -> tuple[cv2.VideoCapture, float, int]:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FileNotFoundError(f"영상을 열 수 없습니다: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    return cap, fps, n


def run_radar(
    source: str,
    models: ModelSpec,
    cfg: PipelineConfig,
    device: str,
    out_video: str | None = None,
    max_crops_per_track: int = 20,
    on_progress: OnProgress | None = None,
    should_cancel: ShouldCancel | None = None,
    progress_every: int = 15,
) -> PipelineResult:
    on_progress = on_progress or _noop_progress
    should_cancel = should_cancel or _never_cancel

    cap, src_fps, n_frames = _open_video(source)
    out_fps = cfg.target_fps or (src_fps / cfg.stride)

    player_det = PlayerDetector(
        weights=models.player_weights, model_id=models.player_model_id,
        api_key=models.api_key, device=device,
    )
    field_det = FieldDetector(
        weights=models.field_weights, model_id=models.field_model_id,
        api_key=models.api_key, device=device,
    )
    tracker = Tracker(cfg, fps=out_fps)
    homog = HomographyEstimator(
        soccer_pitch_vertices_cm(), cfg.keypoint_conf, cfg.min_keypoints,
        cfg.homography_reproj_thresh, cfg.homography_max_stale,
    )

    rows: list[dict] = []
    ball_rows: list[dict] = []
    fit_crops: list[np.ndarray] = []
    track_crops: dict[int, list] = defaultdict(list)
    track_cls_votes: dict[int, Counter] = defaultdict(Counter)

    out_idx = 0
    src_idx = -1
    cancelled = False
    total_est = (n_frames // max(1, cfg.stride)) or 0
    on_progress("분석", 0, total_est)

    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dp-detect")
    budget = _FrameFailureBudget()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        src_idx += 1
        if src_idx % cfg.stride != 0:
            continue

        if out_idx % progress_every == 0:
            if should_cancel():
                cancelled = True
                break
            on_progress("분석", out_idx, total_est)

        # 1~2) 선수 검출 + 피치 키포인트 검출 (동시 호출)
        try:
            det, kp_xy, kp_cf = _detect_both(
                pool, player_det, field_det, frame, cfg.player_conf)
        except RoboflowTransientError as e:
            budget.record_failure(e, out_idx)  # 예산 초과 시 여기서 예외 전파
            out_idx += 1                       # 타임라인 유지를 위해 번호는 소비
            continue
        budget.record_success()
        parts = split_detections(det)
        m = homog.update(frame, kp_xy, kp_cf)

        # 3) 추적 (선수+골키퍼+심판)
        non_ball = det[det.class_id != 0] if det.class_id is not None else det
        tracked = tracker.update(non_ball)

        if m is not None and tracked.tracker_id is not None and len(tracked):
            feet = foot_points(tracked)
            cm = HomographyEstimator.transform(m, feet)
            for i, tid in enumerate(tracked.tracker_id):
                cid = int(tracked.class_id[i]) if tracked.class_id is not None else PLAYER_ID
                track_cls_votes[int(tid)][cid] += 1
                x, y = float(cm[i, 0]), float(cm[i, 1])
                rows.append({"frame": out_idx, "track_id": int(tid), "cls_id": cid,
                             "x": x, "y": y})
                if cid == PLAYER_ID and len(track_crops[int(tid)]) < max_crops_per_track:
                    x1, y1, x2, y2 = tracked.xyxy[i].astype(int)
                    crop = frame[max(0, y1):y2, max(0, x1):x2]
                    if crop.size:
                        track_crops[int(tid)].append(
                            cv2.resize(crop, (96, 128)) if crop.shape[0] > 4 else crop
                        )

        if out_idx % cfg.team_fit_stride == 0 and len(fit_crops) < cfg.team_fit_max_crops:
            pl = parts.players
            for c in crop_boxes(frame, pl.xyxy):
                if c is not None and c.size:
                    fit_crops.append(cv2.resize(c, (96, 128)) if c.shape[0] > 4 else c)

        # 4) 공
        if m is not None and len(parts.ball):
            bc = HomographyEstimator.transform(m, center_points(parts.ball))
            k = int(np.argmax(parts.ball.confidence)) if parts.ball.confidence is not None else 0
            ball_rows.append({"frame": out_idx, "x": float(bc[k, 0]), "y": float(bc[k, 1])})

        out_idx += 1

    pool.shutdown(wait=True)
    cap.release()
    on_progress("분석", out_idx, total_est or out_idx)

    st = homog.stats()
    log.info("[dotplay] 피치 캘리브레이션: 키포인트 %d · 전파 %d · 포기 %d (키포인트 %.0f%%)",
             st["keypoint"], st["propagated"], st["dropped"], st["keypoint_ratio"] * 100)

    coords = pd.DataFrame(rows)
    ball = constrain_to_pitch(pd.DataFrame(ball_rows), cfg)
    if coords.empty:
        return PipelineResult(coords, ball, out_fps, cancelled=cancelled)

    # 트랙별 클래스 확정
    track_cls = {tid: CLASS_NAME.get(c.most_common(1)[0][0], "player")
                 for tid, c in track_cls_votes.items()}

    # 팀 분류 학습 + 트랙 단위 다수결
    team_of: dict[int, int] = {}
    if not cancelled:
        try:
            on_progress("팀분류", 0, 1)
            clf = TeamClassifier(device=device)
            clf.fit(fit_crops)
            voter = TrackTeamVoter()
            for tid, crops in track_crops.items():
                if not crops:
                    continue
                for t in clf.predict(crops):
                    voter.add(tid, t)
            team_of = voter.resolve()
            on_progress("팀분류", 1, 1)
        except Exception:
            pass  # 팀 분류 실패해도 좌표는 살림

    coords["cls"] = coords["track_id"].map(track_cls).fillna("player")
    coords["team"] = coords["track_id"].map(team_of).fillna(0).astype(int)
    coords = coords[["frame", "track_id", "team", "cls", "x", "y"]]

    if cancelled:
        return PipelineResult(coords, ball, out_fps, cancelled=True)

    on_progress("스무딩", 0, 1)
    coords = smooth_tracks(coords, out_fps, cfg)
    on_progress("스무딩", 1, 1)

    result = PipelineResult(coords, ball, out_fps)
    if out_video:
        on_progress("렌더링", 0, 1)
        result.video_out = render_video(coords, out_fps, cfg, out_video, ball_df=ball)
        on_progress("렌더링", 1, 1)
    return result


def run_radar_segments(
    source: str,
    segments: list[tuple[float, float]],
    models: ModelSpec,
    cfg: PipelineConfig,
    device: str,
    out_video: str | None = None,
    max_crops_per_track: int = 20,
    on_progress: OnProgress | None = None,
    should_cancel: ShouldCancel | None = None,
    progress_every: int = 15,
) -> PipelineResult:
    """원본 영상의 (start, end) 구간들만 분석해 '하이라이트 편집본 타임라인'
    기준의 단일 dot-play 결과를 만든다 (하이라이트 PiP 합성용).

    편집본을 직접 분석하면 장면 전환에서 추적·옵티컬플로우 전파가 깨지므로,
    원본의 연속 구간을 구간마다 추적기·호모그래피를 리셋해 각각 분석하고,
    좌표의 frame 인덱스만 편집본 타임라인 위치로 재배치한다. 팀 분류는 전체
    구간에서 모은 크롭으로 한 번만 학습해 구간 사이에 팀 색이 뒤바뀌지 않게
    한다. segments는 build_output()과 동일한 병합 타임라인(get_merged_timeline)
    이어야 결과가 편집본과 프레임 단위로 정렬된다.
    """
    on_progress = on_progress or _noop_progress
    should_cancel = should_cancel or _never_cancel

    cap, src_fps, _ = _open_video(source)
    out_fps = cfg.target_fps or (src_fps / cfg.stride)

    player_det = PlayerDetector(
        weights=models.player_weights, model_id=models.player_model_id,
        api_key=models.api_key, device=device,
    )
    field_det = FieldDetector(
        weights=models.field_weights, model_id=models.field_model_id,
        api_key=models.api_key, device=device,
    )

    rows: list[dict] = []
    ball_rows: list[dict] = []
    fit_crops: list[np.ndarray] = []
    track_crops: dict[int, list] = defaultdict(list)
    track_cls_votes: dict[int, Counter] = defaultdict(Counter)

    # 구간별 track_id 네임스페이스 분리 — ByteTrack이 구간마다 1부터 다시 세므로
    TRACK_ID_STRIDE = 1_000_000

    seg_src_counts = [max(1, int(round((e - s) * src_fps))) for s, e in segments]
    total_est = sum(-(-n // max(1, cfg.stride)) for n in seg_src_counts)
    on_progress("분석", 0, total_est)

    cancelled = False
    done_global = 0
    cum_time = 0.0
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dp-detect")
    budget = _FrameFailureBudget()

    for si, (seg_start, seg_end) in enumerate(segments):
        if should_cancel():
            cancelled = True
            break
        # 구간 사이는 연속 영상이 아니므로 추적기·호모그래피를 리셋한다
        tracker = Tracker(cfg, fps=out_fps)
        homog = HomographyEstimator(
            soccer_pitch_vertices_cm(), cfg.keypoint_conf, cfg.min_keypoints,
            cfg.homography_reproj_thresh, cfg.homography_max_stale,
        )
        frame_offset = int(round(cum_time * out_fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(seg_start * src_fps)))

        local_src = -1
        local_out = 0
        n_src = seg_src_counts[si]
        while local_src + 1 < n_src:
            ok, frame = cap.read()
            if not ok:
                break
            local_src += 1
            if local_src % cfg.stride != 0:
                continue

            if local_out % progress_every == 0:
                if should_cancel():
                    cancelled = True
                    break
                on_progress("분석", done_global, total_est)

            gframe = frame_offset + local_out

            # 1~2) 선수 검출 + 피치 키포인트 검출 (동시 호출)
            try:
                det, kp_xy, kp_cf = _detect_both(
                    pool, player_det, field_det, frame, cfg.player_conf)
            except RoboflowTransientError as e:
                budget.record_failure(e, done_global)
                local_out += 1      # 편집본 타임라인과의 정렬을 유지
                done_global += 1
                continue
            budget.record_success()
            parts = split_detections(det)
            m = homog.update(frame, kp_xy, kp_cf)

            # 3) 추적 (선수+골키퍼+심판)
            non_ball = det[det.class_id != 0] if det.class_id is not None else det
            tracked = tracker.update(non_ball)

            if m is not None and tracked.tracker_id is not None and len(tracked):
                feet = foot_points(tracked)
                cm = HomographyEstimator.transform(m, feet)
                for i, tid in enumerate(tracked.tracker_id):
                    cid = int(tracked.class_id[i]) if tracked.class_id is not None else PLAYER_ID
                    gtid = int(tid) + si * TRACK_ID_STRIDE
                    track_cls_votes[gtid][cid] += 1
                    x, y = float(cm[i, 0]), float(cm[i, 1])
                    rows.append({"frame": gframe, "track_id": gtid, "cls_id": cid,
                                 "x": x, "y": y})
                    if cid == PLAYER_ID and len(track_crops[gtid]) < max_crops_per_track:
                        x1, y1, x2, y2 = tracked.xyxy[i].astype(int)
                        crop = frame[max(0, y1):y2, max(0, x1):x2]
                        if crop.size:
                            track_crops[gtid].append(
                                cv2.resize(crop, (96, 128)) if crop.shape[0] > 4 else crop
                            )

            if done_global % cfg.team_fit_stride == 0 and len(fit_crops) < cfg.team_fit_max_crops:
                pl = parts.players
                for c in crop_boxes(frame, pl.xyxy):
                    if c is not None and c.size:
                        fit_crops.append(cv2.resize(c, (96, 128)) if c.shape[0] > 4 else c)

            # 4) 공
            if m is not None and len(parts.ball):
                bc = HomographyEstimator.transform(m, center_points(parts.ball))
                k = int(np.argmax(parts.ball.confidence)) if parts.ball.confidence is not None else 0
                ball_rows.append({"frame": gframe, "x": float(bc[k, 0]), "y": float(bc[k, 1])})

            local_out += 1
            done_global += 1

        if cancelled:
            break
        cum_time += seg_end - seg_start

    pool.shutdown(wait=True)
    cap.release()
    on_progress("분석", done_global, total_est or done_global)

    coords = pd.DataFrame(rows)
    ball = constrain_to_pitch(pd.DataFrame(ball_rows), cfg)
    if coords.empty:
        return PipelineResult(coords, ball, out_fps, cancelled=cancelled)

    # 트랙별 클래스 확정
    track_cls = {tid: CLASS_NAME.get(c.most_common(1)[0][0], "player")
                 for tid, c in track_cls_votes.items()}

    # 팀 분류 — 전체 구간 크롭으로 1회 학습 (구간 간 팀 색 일관성)
    team_of: dict[int, int] = {}
    if not cancelled:
        try:
            on_progress("팀분류", 0, 1)
            clf = TeamClassifier(device=device)
            clf.fit(fit_crops)
            voter = TrackTeamVoter()
            for tid, crops in track_crops.items():
                if not crops:
                    continue
                for t in clf.predict(crops):
                    voter.add(tid, t)
            team_of = voter.resolve()
            on_progress("팀분류", 1, 1)
        except Exception:
            pass  # 팀 분류 실패해도 좌표는 살림

    coords["cls"] = coords["track_id"].map(track_cls).fillna("player")
    coords["team"] = coords["track_id"].map(team_of).fillna(0).astype(int)
    coords = coords[["frame", "track_id", "team", "cls", "x", "y"]]

    if cancelled:
        return PipelineResult(coords, ball, out_fps, cancelled=True)

    # 스무딩 — 트랙이 구간 경계를 넘지 않으므로(id 네임스페이스 분리) 전역 호출 안전
    on_progress("스무딩", 0, 1)
    coords = smooth_tracks(coords, out_fps, cfg)
    on_progress("스무딩", 1, 1)

    result = PipelineResult(coords, ball, out_fps)
    if out_video:
        # 편집본과 길이를 정확히 맞추기 위해 타임라인 전체 범위를 강제 렌더링
        total_frames = max(1, int(round(sum(e - s for s, e in segments) * out_fps)))
        on_progress("렌더링", 0, 1)
        result.video_out = render_video(
            coords, out_fps, cfg, out_video, ball_df=ball,
            frame_range=(0, total_frames - 1),
        )
        on_progress("렌더링", 1, 1)
    return result
