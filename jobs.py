#!/usr/bin/env python3
"""
jobs.py — 배치 잡 상태 저장소·백그라운드 워커·오디오/비전 처리 파이프라인.

JOBS 딕셔너리가 이 앱의 유일한 상태 저장소다. 서버가 재시작되면 원칙적으로
초기화되지만, 판별까지 끝난(ready) 잡은 results/results_{jid}.json으로 이미
저장돼 있으므로 restore_recent_results()가 이를 스캔해 최근 것만 복원한다.
모든 접근은 JLOCK으로 보호한다.
"""

import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import date
from pathlib import Path

import config
import pan_signal
import soccer_highlights as sh

log = logging.getLogger("hl")

JOBS = {}
JOB_QUEUE = queue.Queue()
JLOCK = threading.Lock()
BUILD_LOCK = threading.Lock()

ACTIVE_STATUSES = ("merging", "detecting", "classifying", "building")


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def order_parts(paths):
    """병합 파트를 촬영 순서(앞→뒤)로 정렬한다.

    XbotGo는 30분을 넘기면 파일을 순차 저장하므로 **파일 수정시각(mtime)이 곧
    파트 순서**다. 30분 꽉 채운 파트가 먼저, 자투리(짧은) 파트가 항상 뒤로 간다.
    사용자가 파일 선택창에서 아무 순서로 골라도 여기서 바로잡는다.
    mtime이 동일하면(드묾) 파일명 자연 정렬로 보조한다.
    """
    def key(p):
        try:
            mt = round(os.path.getmtime(p), 3)
        except OSError:
            mt = 0.0
        return (mt, _natural_key(os.path.basename(p)))
    return sorted(paths, key=key)


def new_job(video, sensitivity="normal", workers=sh.VISION_WORKERS,
            pre_sec=None, post_sec=None, source_videos=None):
    jid = uuid.uuid4().hex[:8]

    # source_videos 가 2개 이상이면 "한 경기가 여러 파트로 나뉜" 잡이다.
    # (XbotGo 30분 자동 분할). 처리 시작 시 _process 가 먼저 하나로 병합하고,
    # 그 뒤 단계는 기존과 동일하게 단일 파일로 진행한다 → 하이라이트/업로드가 1개.
    source_videos = [os.path.abspath(p) for p in (source_videos or [])]
    needs_merge = len(source_videos) >= 2
    if needs_merge:
        source_videos = order_parts(source_videos)   # 촬영 순서로 자동 정렬
        video = str((config.OUTPUT_DIR / "_merged" / f"{jid}.mp4").resolve())
        # 표시 이름은 첫 파트 이름 그대로 — YouTube 제목이 깔끔하게 나오도록.
        # "여러 파트 합본"임은 큐 UI가 n_parts 배지로 따로 보여준다.
        video_name = os.path.basename(source_videos[0])
    else:
        # source_videos 가 1개뿐이면 병합 아님 — 그 파일을 그대로 쓴다.
        if video is None and source_videos:
            video = source_videos[0]
        video = os.path.abspath(video)
        video_name = os.path.basename(video)
        source_videos = []

    job = dict(
        id=jid,
        video=video,
        video_name=video_name,
        source_videos=source_videos,   # [] 이면 단일 파일 잡
        needs_merge=needs_merge,
        sensitivity=sensitivity,
        workers=workers,
        pre_sec=float(pre_sec) if pre_sec is not None else sh.PRE_SEC,
        post_sec=float(post_sec) if post_sec is not None else sh.POST_SEC,
        status="pending",
        candidates=[],
        vision_used=False,
        usage=None,
        workdir=None,
        output=None,
        error=None,
        duration=None,
        approved=None,
        progress=dict(stage=None, done=0, total=0),
        started_at=None,   # monotonic timestamp — 처리 시작
        finished_at=None,  # monotonic timestamp — ready 도달
        # YouTube 업로드 상태
        yt_status=None,    # None | "uploading" | "done" | "error"
        yt_url=None,       # https://youtu.be/<id>
        yt_error=None,
        yt_progress=dict(done=0, total=0),
        # BAND 게시 상태
        band_status=None,  # None | "posting" | "done" | "error"
        band_post_url=None,
        band_error=None,
        match_date=date.today().isoformat(),  # BAND 날짜별 그룹핑용
        # 취소/지연삭제 상태
        cancel_requested=False,   # True면 처리 루프가 다음 체크포인트에서 중단
        pending_delete=False,     # 처리 중 삭제 요청됨 — 완료 시 실제 제거
    )
    with JLOCK:
        JOBS[jid] = job
    save_job_state(jid)
    return jid


def update(jid, **kw):
    """jid가 이미 삭제됐으면 조용히 무시한다(처리 중 삭제로 인한 경쟁 방지)."""
    with JLOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        job.update(kw)
    non_progress_keys = set(kw.keys()) - {"yt_progress", "progress"}
    if non_progress_keys:
        save_job_state(jid)


def set_progress(jid, stage, done, total):
    with JLOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        job["progress"] = dict(stage=stage, done=done, total=total)


def clear_progress(jid):
    with JLOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        job["progress"] = dict(stage=None, done=0, total=0)


def is_cancelled(jid) -> bool:
    with JLOCK:
        job = JOBS.get(jid)
        return bool(job and job.get("cancel_requested"))


def finalize_pending_delete(jid) -> bool:
    """취소 요청으로 삭제가 지연됐던 잡을 처리 종료 후 실제로 정리한다."""
    with JLOCK:
        job = JOBS.get(jid)
        if job is None or not job.get("pending_delete"):
            return False
        JOBS.pop(jid, None)
    if job.get("workdir") and os.path.exists(job["workdir"]):
        shutil.rmtree(job["workdir"], ignore_errors=True)
    delete_results_file(jid)
    delete_state_file(jid)
    delete_merged_file(jid)
    log.info("[%s] 지연 삭제 완료 (취소된 작업 정리)", jid)
    return True


def delete_results_file(jid) -> None:
    """results/results_{jid}.json을 제거한다 (잡이 완전히 삭제될 때 함께 정리해
    재시작 시 이미 삭제된 잡이 restore_recent_results()로 되살아나지 않게 한다)."""
    try:
        (config.RESULTS_DIR / f"results_{jid}.json").unlink(missing_ok=True)
    except OSError:
        pass


def delete_state_file(jid) -> None:
    """results/state_{jid}.json을 제거한다."""
    try:
        (config.RESULTS_DIR / f"state_{jid}.json").unlink(missing_ok=True)
    except OSError:
        pass


def delete_merged_file(jid) -> None:
    """output/_merged/{jid}.mp4 를 제거한다 (파트 병합 잡이 삭제될 때 함께 정리).
    병합본은 파트 용량 합만큼 커서 잡을 지우면 반드시 같이 지운다."""
    try:
        (config.OUTPUT_DIR / "_merged" / f"{jid}.mp4").unlink(missing_ok=True)
    except OSError:
        pass


def save_job_state(jid) -> None:
    """잡의 전체 상태를 results/state_{jid}.json 파일에 저장한다."""
    with JLOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        job_copy = dict(job)

    # monotonic 타임스탬프 계산 및 보존
    if job_copy.get("started_at") and job_copy.get("finished_at"):
        job_copy["elapsed_sec"] = round(job_copy["finished_at"] - job_copy["started_at"])

    # JSON 직렬화가 불가능하거나 monotonic이라 무의미한 monotonic 시간 필드 제외
    job_copy_save = {k: v for k, v in job_copy.items() if k not in ("started_at", "finished_at")}

    state_file = config.RESULTS_DIR / f"state_{jid}.json"
    try:
        # 임시 파일 작성 후 교체하는 원자적 쓰기로 혹시 모를 쓰기 중 손상 방지
        temp_state = state_file.with_name(f"{state_file.name}.tmp")
        temp_state.write_text(json.dumps(job_copy_save, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_state.replace(state_file)
    except Exception:
        log.exception("[%s] 상태 파일 저장 실패", jid)


def restore_recent_results(max_age_hours: float = 24.0) -> int:
    """서버 재시작 시 results/ 의 최근 판별 결과(state_*.json 및 results_*.json)를 복원한다.

    1. state_*.json 파일을 우선적으로 검색하여 완벽한 상태(승인 여부, 상태, 업로드 정보 등)를 복원한다.
    2. state_*.json이 없으면 하위 호환성을 위해 results_*.json 파일을 검색해 복원한다.
    """
    restored = 0
    now = time.time()

    # 1. state_*.json 복원 시도
    for path in sorted(config.RESULTS_DIR.glob("state_*.json")):
        jid = path.stem[len("state_"):]
        with JLOCK:
            already_loaded = jid in JOBS
        if already_loaded:
            continue
        age_hours = (now - path.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        video = job.get("video")
        srcs = job.get("source_videos") or []
        if job.get("needs_merge") and srcs:
            # 아직 병합 전이라 merged 파일은 없다 — 원본 파트가 다 살아 있으면 복원 가능.
            if not all(os.path.exists(s) for s in srcs):
                continue
        elif not video or not os.path.exists(video):
            continue

        # 중요: 서버가 강제 종료(크래시 등)되어 복구되는 경우, active 상태인 잡은
        # 백그라운드 워커/빌드가 실행되고 있지 않으므로 "error" 상태로 전환한다.
        if job.get("status") in ACTIVE_STATUSES:
            job["status"] = "error"
            job["error"] = "서버 강제 종료로 인해 작업이 중단되었습니다. [재시도]를 눌러 다시 진행할 수 있습니다."

        # progress 정보는 휘발성이므로 초기화
        job["progress"] = dict(stage=None, done=0, total=0)

        # yt_progress 도 휘발성이므로 status가 "uploading"이었으면 "error" 상태로 전환
        if job.get("yt_status") == "uploading":
            job["yt_status"] = "error"
            job["yt_error"] = "서버 강제 종료로 인해 업로드가 중단되었습니다."
        job["yt_progress"] = dict(done=0, total=0)

        # band_status 가 "posting"이었으면 "error" 상태로 전환
        if job.get("band_status") == "posting":
            job["band_status"] = "error"
            job["band_error"] = "서버 강제 종료로 인해 게시가 중단되었습니다."

        # cancel_requested 와 pending_delete 도 초기화
        job["cancel_requested"] = False
        job["pending_delete"] = False

        # monotonic 시간 복원
        job["started_at"] = None
        job["finished_at"] = None

        # workdir가 없거나 실재하지 않는다면 새로 설정
        if not job.get("workdir") or not os.path.exists(job["workdir"]):
            job["workdir"] = str(Path(tempfile.mkdtemp(prefix=f"hl_{jid}_")))

        with JLOCK:
            if jid not in JOBS:
                JOBS[jid] = job
                restored += 1
        save_job_state(jid)

    # 2. results_*.json 하위 호환용 복원 시도
    for path in sorted(config.RESULTS_DIR.glob("results_*.json")):
        jid = path.stem[len("results_"):]
        with JLOCK:
            already_loaded = jid in JOBS
        if already_loaded:
            continue
        age_hours = (now - path.stat().st_mtime) / 3600
        if age_hours > max_age_hours:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        video = data.get("video")
        if not video or not os.path.exists(video):
            continue

        params = data.get("params") or {}
        job = dict(
            id=jid,
            video=video,
            video_name=os.path.basename(video),
            sensitivity="normal",
            workers=sh.VISION_WORKERS,
            pre_sec=params.get("PRE_SEC", sh.PRE_SEC),
            post_sec=params.get("POST_SEC", sh.POST_SEC),
            status="ready",
            candidates=data.get("candidates", []),
            vision_used=bool(data.get("vision_used", True)),
            usage=None,
            workdir=str(Path(tempfile.mkdtemp(prefix=f"hl_{jid}_"))),
            output=None,
            error=None,
            duration=data.get("duration"),
            approved=None,
            progress=dict(stage=None, done=0, total=0),
            started_at=None,
            finished_at=None,
            elapsed_sec=None,
            yt_status=None,
            yt_url=None,
            yt_error=None,
            yt_progress=dict(done=0, total=0),
            band_status=None,
            band_post_url=None,
            band_error=None,
            match_date=date.today().isoformat(),
            cancel_requested=False,
            pending_delete=False,
        )
        with JLOCK:
            if jid not in JOBS:
                JOBS[jid] = job
                restored += 1
    if restored:
        log.info("이전 세션의 판별 결과 %d개를 복원 (24시간 이내)", restored)
    return restored


def _friendly_error(exc: Exception) -> str:
    """예외를 사람이 읽기 쉬운 한 줄 메시지로 변환."""
    msg = str(exc)
    etype = type(exc).__name__

    # ffmpeg / ffprobe 없음
    if isinstance(exc, FileNotFoundError):
        if "ffmpeg" in msg.lower() or "ffprobe" in msg.lower() or "WinError 2" in msg:
            return (
                "ffmpeg를 찾을 수 없습니다. "
                "설치 후 앱을 재시작하세요.\n"
                "  winget install Gyan.FFmpeg  (관리자 PowerShell)\n"
                "또는 https://ffmpeg.org/download.html 에서 수동 설치"
            )
        return f"파일을 찾을 수 없습니다: {msg}"

    # ffmpeg 실행 오류
    if isinstance(exc, RuntimeError) and "command failed" in msg:
        return f"ffmpeg 실행 오류: {msg}"

    # 영상 파일 없음·접근 불가
    if isinstance(exc, (FileNotFoundError, PermissionError, OSError)):
        return f"[{etype}] {msg}"

    # 그 외: 마지막 줄만 표시 (트레이스백 제외)
    last_line = msg.strip().splitlines()[-1] if msg.strip() else repr(exc)
    return f"[{etype}] {last_line}"


def _process(jid):
    job = JOBS.get(jid)
    if job is None:
        return
    video = job["video"]
    sp = sh.SENSITIVITY_PRESETS.get(job["sensitivity"], sh.SENSITIVITY_PRESETS["normal"])

    wd = Path(tempfile.mkdtemp(prefix=f"hl_{jid}_"))
    t_start = time.monotonic()
    update(jid, workdir=str(wd), started_at=t_start)

    # 파트 병합 — 한 경기가 여러 파일로 나뉜 잡(XbotGo 30분 자동 분할)은 먼저
    # 하나로 이어붙인다. 성공하면 needs_merge=False 로 내려 재시도 시 다시 안 합친다.
    if job.get("needs_merge"):
        srcs = order_parts(job.get("source_videos") or [])   # 방어적으로 재정렬
        update(jid, status="merging", source_videos=srcs)
        # 각 파트 길이를 확인해 순서를 검증한다 — XbotGo 규칙상 자투리(짧은) 파트는
        # 항상 마지막이어야 한다. 앞 파트가 뒤 파트보다 짧으면 순서 의심 → 경고.
        durs = []
        for p in srcs:
            try:
                durs.append(sh.probe_duration(p))
            except Exception:
                durs.append(None)
        order_desc = ", ".join(
            f"{os.path.basename(p)}({d/60:.0f}분)" if d else os.path.basename(p)
            for p, d in zip(srcs, durs))
        log.info("[%s] 파트 %d개 병합 시작 (순서: %s) → %s", jid, len(srcs), order_desc, video)
        known = [d for d in durs if d]
        if len(known) >= 2 and any(a + 30 < b for a, b in zip(known, known[1:])):
            log.warning("[%s] 파트 순서 의심 — 뒤 파트가 앞 파트보다 깁니다. "
                        "파일 수정시각 기준 정렬 결과이니 원본 타임스탬프를 확인하세요.", jid)
        Path(video).parent.mkdir(parents=True, exist_ok=True)
        sh.concat_videos(srcs, video, workdir=wd)
        update(jid, needs_merge=False)
        log.info("[%s] 병합 완료: %s", jid, video)
        if is_cancelled(jid):
            update(jid, status="error", error="사용자 취소")
            return

    update(jid, status="detecting")
    log.info("[%s] 검출 시작: %s (민감도: %s)", jid, video, job["sensitivity"])

    dur = sh.probe_duration(video)
    if job.get("candidates"):
        cands = job["candidates"]
        update(jid, duration=dur)
        log.info("[%s] 기존 후보 %d개 유지 (검출 단계 건너뜀)", jid, len(cands))
    else:
        cands = sh.detect_spikes(video, wd, percentile=sp["percentile"], min_db=sp["min_db"],
                                 pre_sec=job.get("pre_sec"), post_sec=job.get("post_sec"))
        update(jid, duration=dur, candidates=cands)
        log.info("[%s] 검출 완료: %.1fs, 후보 %d개", jid, dur, len(cands))

    if is_cancelled(jid):
        update(jid, status="error", error="사용자 취소")
        log.info("[%s] 검출 직후 취소됨", jid)
        return

    # 팬 궤적 3차 신호 — 실패해도 파이프라인은 계속 (후보 단위 격리와 같은 원칙)
    if cands:
        try:
            # 30분 영상 기준 1~2분 걸리는 구간이다. 진행 단계를 표시해 주지 않으면
            # UI가 계속 "오디오 분석 중"으로 보여 멈춘 것처럼 오해하게 된다.
            set_progress(jid, "pan", 0, 0)
            series = pan_signal.compute_pan_series(video, wd, run=sh.run)
            boosted = pan_signal.annotate_candidates(cands, series)
            update(jid, candidates=cands)
            log.info("[%s] 팬 궤적 분석: 이동폭 %.0fpx, 지지 후보 %d개%s",
                     jid, series["range_px"], boosted,
                     "" if series["reliable"] else " (신뢰도 부족 — 미적용)")
        except Exception:
            log.warning("[%s] 팬 궤적 분석 실패 — 신호 없이 진행", jid, exc_info=True)
        finally:
            clear_progress(jid)

    if is_cancelled(jid):
        update(jid, status="error", error="사용자 취소")
        log.info("[%s] 팬 분석 직후 취소됨", jid)
        return

    key = config.load_gemini_api_key()
    if not key:
        update(jid, status="ready", vision_used=False, finished_at=time.monotonic())
        log.warning("[%s] API 키 없음 — 비전 생략", jid)
        return

    from google import genai
    client = genai.Client(api_key=key)
    update(jid, status="classifying")
    set_progress(jid, "classify", 0, len(cands))

    def on_classify_progress(d, t):
        set_progress(jid, "classify", d, t)
        save_job_state(jid)

    usage = sh.classify_all_parallel(
        cands, video, wd, client, sh.CONF_AUTO, job["workers"],
        should_cancel=lambda: is_cancelled(jid),
        on_progress=on_classify_progress,
        pre_sec=job.get("pre_sec"), post_sec=job.get("post_sec"),
    )
    if usage.get("cancelled") or is_cancelled(jid):
        update(jid, status="error", error="사용자 취소")
        clear_progress(jid)
        log.info("[%s] 판별 중 취소됨", jid)
        return
    cost = (usage["in"] / 1e6 * sh.PRICE_IN_PER_M +
            usage["out"] / 1e6 * sh.PRICE_OUT_PER_M)
    usage["cost_usd"] = round(cost, 4)

    sh.save_results(
        str(config.RESULTS_DIR / f"results_{jid}.json"),
        video, dur, cands, True,
    )
    update(jid, status="ready", vision_used=True, usage=usage,
           candidates=cands, finished_at=time.monotonic())
    clear_progress(jid)
    log.info("[%s] 판별 완료: $%.4f", jid, cost)


def _worker():
    while True:
        jid = JOB_QUEUE.get()
        if jid is None:
            break
        try:
            with JLOCK:
                exists = jid in JOBS
            if exists:
                _process(jid)
        except Exception as exc:
            update(jid, status="error", error=_friendly_error(exc))
            log.exception("[%s] 처리 실패", jid)
        finally:
            finalize_pending_delete(jid)
            JOB_QUEUE.task_done()


def start_worker() -> threading.Thread:
    """백그라운드 워커 스레드를 시작한다 (앱 시작 시 1회 호출)."""
    t = threading.Thread(target=_worker, daemon=True, name="hl-worker")
    t.start()
    return t


# ─── 직렬화 ───────────────────────────────────────────────
def flag(c, conf_auto, vision_used):
    if not vision_used:
        return "auto"
    conf = sh.effective_conf(c)  # 팬 신호 가산치 반영 (승격 전용)
    if c.get("highlight") and conf >= conf_auto:
        return "auto"
    if c.get("highlight") and conf >= sh.CONF_MAYBE:
        return "maybe"
    return "reject"


def job_summary(job):
    cands = job["candidates"]
    vu = job["vision_used"]
    n_auto  = sum(1 for c in cands if flag(c, sh.CONF_AUTO, vu) == "auto")
    n_maybe = sum(1 for c in cands if flag(c, sh.CONF_AUTO, vu) == "maybe")
    elapsed = job.get("elapsed_sec")
    if elapsed is None and job["started_at"] and job["finished_at"]:
        elapsed = round(job["finished_at"] - job["started_at"])
    return dict(
        id=job["id"],
        video_name=job["video_name"],
        status=job["status"],
        duration=job["duration"],
        sensitivity=job["sensitivity"],
        n_candidates=len(cands),
        n_auto=n_auto,
        n_maybe=n_maybe,
        n_approved=(len(job["approved"]) if job["approved"] is not None else None),
        usage=job["usage"],
        error=job["error"],
        progress=job["progress"],
        output=job["output"],
        vision_used=vu,
        elapsed_sec=elapsed,
        n_parts=len(job.get("source_videos") or []),
        needs_merge=bool(job.get("needs_merge")),
        yt_status=job.get("yt_status"),
        yt_url=job.get("yt_url"),
        yt_error=job.get("yt_error"),
        yt_progress=job.get("yt_progress", {"done": 0, "total": 0}),
        band_status=job.get("band_status"),
        band_post_url=job.get("band_post_url"),
        band_error=job.get("band_error"),
        match_date=job.get("match_date"),
        pre_sec=job.get("pre_sec", sh.PRE_SEC),
        post_sec=job.get("post_sec", sh.POST_SEC),
    )


def serialize_candidates(cands, conf_auto, vision_used):
    out = []
    for i, c in enumerate(cands):
        conf = float(c.get("confidence", 0))
        pan = c.get("pan") or {}
        out.append(dict(
            idx=i,
            peak=round(float(c["peak"]), 1),
            delta_db=round(float(c.get("delta_db", 0)), 1),
            type=c.get("type", "-"),
            confidence=round(conf, 2),
            conf_eff=round(sh.effective_conf(c), 2),
            pan_bonus=round(float(c.get("pan_bonus") or 0.0), 2),
            pan_label=pan_signal.STATE_LABELS.get(pan.get("state")) if pan else None,
            reason=c.get("reason", ""),
            highlight=bool(c.get("highlight", False)),
            flag=flag(c, conf_auto, vision_used),
        ))
    return out
