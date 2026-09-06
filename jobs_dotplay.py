#!/usr/bin/env python3
"""jobs_dotplay.py — Dot-play(FM 스타일 2D 버드뷰) 배치 잡 상태 저장소·독립 워커.

jobs.py(하이라이트 파이프라인)와 **완전히 분리된** 별도 큐/워커/락을 쓴다.
dot-play는 CPU 추론이 무거워(수십 분 단위) 하이라이트 처리 잡을 막지 않도록
독자적인 워커 스레드에서 순차 처리한다. 상태 딕셔너리·락·지연삭제 패턴은
jobs.py와 동일한 관례를 따르되, 이름공간만 분리했다(DOTPLAY_JOBS 등).
"""

import logging
import os
import queue
import threading
import time
import uuid

import config

log = logging.getLogger("hl")

DOTPLAY_JOBS = {}
DOTPLAY_QUEUE = queue.Queue()
DOTPLAY_LOCK = threading.Lock()

ACTIVE_STATUSES = ("running",)


def new_job(video: str, stride: int = 2, mode: str = "video",
            hl: dict | None = None, note: str | None = None) -> str:
    """mode="video": 영상 전체를 dot-play로 변환.
    mode="pip": 완료된 하이라이트의 구간들만 원본에서 분석해 하이라이트 하단에
    합성. hl = dict(name, output, segments=[[start,end],...], total) — 추가
    시점의 하이라이트 잡 스냅샷(이후 하이라이트 잡이 삭제/재빌드돼도 무관).
    """
    hl = hl or {}
    jid = uuid.uuid4().hex[:8]
    job = dict(
        id=jid,
        video=os.path.abspath(video),
        video_name=os.path.basename(video),
        stride=int(stride),
        mode=mode,
        status="pending",   # pending -> running -> done | error | cancelled
        error=None,
        note=note,          # 치명적이지 않은 경고(예: PiP 싱크 어긋남 가능성)
        progress=dict(stage=None, done=0, total=0),
        started_at=None,    # monotonic
        finished_at=None,   # monotonic
        elapsed_sec=None,
        output_video=None,  # config.DOTPLAY_DIR 기준 파일명 (pip 모드에선 합성본)
        output_radar=None,  # pip 모드 전용 — 합성 전 레이더 단독 영상
        output_coords=None,
        n_frames=None,
        n_tracks=None,
        # pip 모드 스냅샷
        hl_name=hl.get("name"),
        hl_output=hl.get("output"),
        hl_segments=hl.get("segments"),
        hl_total=hl.get("total"),
        cancel_requested=False,
        pending_delete=False,
    )
    with DOTPLAY_LOCK:
        DOTPLAY_JOBS[jid] = job
    return jid


def update(jid, **kw) -> None:
    with DOTPLAY_LOCK:
        job = DOTPLAY_JOBS.get(jid)
        if job is None:
            return
        job.update(kw)


def set_progress(jid, stage, done, total) -> None:
    with DOTPLAY_LOCK:
        job = DOTPLAY_JOBS.get(jid)
        if job is None:
            return
        job["progress"] = dict(stage=stage, done=done, total=total)


def is_cancelled(jid) -> bool:
    with DOTPLAY_LOCK:
        job = DOTPLAY_JOBS.get(jid)
        return bool(job and job.get("cancel_requested"))


def request_cancel(jid) -> bool:
    """실행 중인 잡에 취소를 요청한다(삭제하지 않음, 다음 체크포인트에서 중단)."""
    with DOTPLAY_LOCK:
        job = DOTPLAY_JOBS.get(jid)
        if job is None:
            return False
        job["cancel_requested"] = True
        return True


def request_delete(jid) -> bool:
    """실행 중이 아니면 즉시 삭제, 실행 중이면 취소 요청 후 지연 삭제로 전환."""
    with DOTPLAY_LOCK:
        job = DOTPLAY_JOBS.get(jid)
        if job is None:
            return False
        if job["status"] == "running":
            job["cancel_requested"] = True
            job["pending_delete"] = True
            return True
        DOTPLAY_JOBS.pop(jid, None)
    _cleanup_outputs(jid)
    return True


def _cleanup_outputs(jid) -> None:
    for name in (f"{jid}.mp4", f"{jid}_radar.mp4", f"{jid}.parquet"):
        try:
            (config.DOTPLAY_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


def finalize_pending_delete(jid) -> bool:
    """취소 요청으로 지연됐던 삭제를 처리 종료 후 실제로 수행한다."""
    with DOTPLAY_LOCK:
        job = DOTPLAY_JOBS.get(jid)
        if job is None or not job.get("pending_delete"):
            return False
        DOTPLAY_JOBS.pop(jid, None)
    _cleanup_outputs(jid)
    log.info("[dotplay:%s] 지연 삭제 완료(취소된 작업 정리)", jid)
    return True


def job_summary(job: dict) -> dict:
    """UI 노출용 사본(절대경로 등 내부 정보 제외)."""
    d = {k: job[k] for k in (
        "id", "video_name", "stride", "mode", "status", "error", "note", "progress",
        "output_video", "output_radar", "output_coords", "n_frames", "n_tracks",
        "elapsed_sec", "hl_name",
    )}
    # 실행 중인 잡은 경과 시간을 실시간으로 계산해 UI 타이머·ETA 추정에 쓴다
    if job["status"] == "running" and job.get("started_at") is not None:
        d["elapsed_sec"] = round(time.monotonic() - job["started_at"])
    return d


def _friendly_error(exc: Exception) -> str:
    """예외를 사람이 읽을 수 있는 한 줄로 바꾼다.

    주의: 메시지 문자열에서 "api_key"/"401"을 찾아 키 문제로 단정하면 안 된다.
    Roboflow 요청 URL에 `?api_key=...`가 들어 있어 단순 네트워크 오류까지
    "키가 잘못됨"으로 오진했다(2026-08-01, 실제 원인은 ConnectTimeout).
    인증 실패는 전용 예외 타입으로만 판별한다.
    """
    from dotplay.roboflow_client import RoboflowAuthError, RoboflowTransientError

    msg = str(exc)
    etype = type(exc).__name__
    if isinstance(exc, ImportError):
        return f"필요한 패키지가 설치되지 않았습니다: {msg}"
    if isinstance(exc, FileNotFoundError):
        return f"파일을 찾을 수 없습니다: {msg}"
    if isinstance(exc, RoboflowAuthError):
        return msg
    if isinstance(exc, RoboflowTransientError):
        return f"네트워크/서버 문제로 중단되었습니다 — 연결 확인 후 재시도하세요. {msg}"
    last_line = msg.strip().splitlines()[-1] if msg.strip() else repr(exc)
    return f"[{etype}] {last_line}"


def _process(jid: str) -> None:
    job = DOTPLAY_JOBS.get(jid)
    if job is None:
        return
    video = job["video"]
    t_start = time.monotonic()
    update(jid, status="running", started_at=t_start)
    log.info("[dotplay:%s] 분석 시작: %s (stride=%s)", jid, video, job["stride"])

    try:
        # 무거운 CV 스택은 여기서 처음 import(지연 로딩) — 앱 시작 속도에 영향 없음
        from dotplay.config import PipelineConfig
        from dotplay.device import resolve_device
        from dotplay.pipeline import ModelSpec, run_radar, run_radar_segments

        device = resolve_device("auto")
        cfg = PipelineConfig(device=device, stride=job["stride"])
        api_key = config.load_roboflow_api_key()
        if not api_key:
            raise RuntimeError(
                "ROBOFLOW_API_KEY가 설정되지 않았습니다. .env에 추가한 뒤 앱을 재시작하세요."
            )
        models = ModelSpec(
            player_model_id=config.get_dotplay_player_model_id(),
            field_model_id=config.get_dotplay_field_model_id(),
            api_key=api_key,
        )
        out_mp4 = config.DOTPLAY_DIR / f"{jid}.mp4"
        out_parquet = config.DOTPLAY_DIR / f"{jid}.parquet"

        if job.get("mode") == "pip":
            # 원본의 하이라이트 구간들만 분석 → 편집본 타임라인 정렬 레이더 영상
            radar_mp4 = config.DOTPLAY_DIR / f"{jid}_radar.mp4"
            segments = [tuple(s) for s in (job.get("hl_segments") or [])]
            if not segments:
                raise RuntimeError("하이라이트 구간 정보가 없습니다.")
            hl_output = job.get("hl_output")
            if not hl_output or not os.path.exists(hl_output):
                raise RuntimeError("하이라이트 출력 파일이 없습니다. 영상을 다시 생성한 뒤 시도하세요.")

            result = run_radar_segments(
                video, segments, models, cfg, device,
                out_video=str(radar_mp4),
                on_progress=lambda stage, done, total: set_progress(jid, stage, done, total),
                should_cancel=lambda: is_cancelled(jid),
            )
            if result.cancelled or is_cancelled(jid):
                update(jid, status="cancelled", finished_at=time.monotonic())
                log.info("[dotplay:%s] 취소됨", jid)
                return
            if result.coords.empty:
                raise RuntimeError(
                    "선수/공을 검출하지 못했습니다 — 피치 키포인트 인식 실패 또는 모델 부적합."
                )

            set_progress(jid, "합성", 0, 1)
            _composite_pip(hl_output, radar_mp4, out_mp4)
            set_progress(jid, "합성", 1, 1)
        else:
            radar_mp4 = None
            result = run_radar(
                video, models, cfg, device,
                out_video=str(out_mp4),
                on_progress=lambda stage, done, total: set_progress(jid, stage, done, total),
                should_cancel=lambda: is_cancelled(jid),
            )
            if result.cancelled or is_cancelled(jid):
                update(jid, status="cancelled", finished_at=time.monotonic())
                log.info("[dotplay:%s] 취소됨", jid)
                return

        if not result.coords.empty:
            result.coords.to_parquet(out_parquet)

        t_end = time.monotonic()
        update(
            jid, status="done", finished_at=t_end,
            elapsed_sec=round(t_end - t_start),
            output_video=out_mp4.name if out_mp4.exists() else None,
            output_radar=(radar_mp4.name if radar_mp4 is not None and radar_mp4.exists() else None),
            output_coords=out_parquet.name if out_parquet.exists() else None,
            n_frames=int(result.coords["frame"].nunique()) if not result.coords.empty else 0,
            n_tracks=int(result.coords["track_id"].nunique()) if not result.coords.empty else 0,
        )
        log.info("[dotplay:%s] 완료 (%.0fs)", jid, t_end - t_start)
    except Exception as e:
        log.exception("[dotplay:%s] 처리 오류", jid)
        update(jid, status="error", error=_friendly_error(e), finished_at=time.monotonic())
    finally:
        finalize_pending_delete(jid)


def _composite_pip(main_path, pip_path, out_path,
                   width_ratio: float = 0.28, margin: int = 18,
                   opacity: float = 0.9) -> None:
    """하이라이트 영상 하단 중앙에 레이더 영상을 작게 얹는다(ffmpeg overlay).

    레이더는 편집본 타임라인에 맞춰 렌더링돼 있으므로 시작점(0초)부터 그대로
    겹치면 된다. 길이가 반올림 오차로 살짝 짧아도 eof_action=repeat로 마지막
    프레임을 유지한다.
    """
    import cv2
    import soccer_highlights as sh  # ffmpeg run 헬퍼 재사용 (앱 시작 시 이미 로드됨)

    cap = cv2.VideoCapture(str(main_path))
    main_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    cap.release()
    if main_w <= 0:
        raise RuntimeError(f"하이라이트 영상을 열 수 없습니다: {main_path}")

    pip_w = max(2, int(round(main_w * width_ratio / 2)) * 2)
    fc = (
        f"[1:v]scale={pip_w}:-2,format=yuva420p,colorchannelmixer=aa={opacity}[pip];"
        f"[0:v][pip]overlay=(W-w)/2:H-h-{margin}:eof_action=repeat[v]"
    )
    sh.run([
        "ffmpeg", "-y", "-i", str(main_path), "-i", str(pip_path),
        "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "copy", str(out_path), "-loglevel", "error",
    ])


def _worker() -> None:
    while True:
        jid = DOTPLAY_QUEUE.get()
        try:
            _process(jid)
        except Exception:
            log.exception("[dotplay:%s] 워커 처리 중 예외", jid)
        finally:
            DOTPLAY_QUEUE.task_done()


def start_worker() -> threading.Thread:
    t = threading.Thread(target=_worker, daemon=True, name="hl-dotplay-worker")
    t.start()
    return t
