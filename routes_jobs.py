#!/usr/bin/env python3
"""routes_jobs.py — 큐 관리·리뷰·영상 생성 관련 API 라우트."""

import logging
import os
import shutil
import threading
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

import config
import jobs
import routes_auth
import soccer_highlights as sh

log = logging.getLogger("hl")

bp_jobs = Blueprint("jobs_api", __name__)


@bp_jobs.route("/api/browse", methods=["POST"])
def api_browse():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(
            title="경기 영상 선택 (복수 선택 가능)",
            filetypes=[("동영상", "*.mp4 *.mov *.mkv *.avi *.m4v"),
                       ("모든 파일", "*.*")])
        root.destroy()
        return jsonify({"paths": list(paths)})
    except Exception as e:
        return jsonify({"paths": [], "error": str(e)})


@bp_jobs.route("/api/jobs/add", methods=["POST"])
def api_jobs_add():
    body = request.json or {}
    global_workers = max(1, min(int(body.get("workers", sh.VISION_WORKERS)), 8))

    # 신형: {jobs: [{video, sensitivity, workers}]}
    # 구형: {videos: [], sensitivity, workers}
    if "jobs" in body:
        job_list = body["jobs"]
    else:
        sensitivity = body.get("sensitivity", "normal")
        job_list = [{"video": v, "sensitivity": sensitivity}
                    for v in body.get("videos", [])]

    # 현재 활성 잡의 경로 집합 (중복 방지)
    with jobs.JLOCK:
        active_paths = {j["video"] for j in jobs.JOBS.values()
                        if j["status"] not in ("done", "error")}

    added, errors, skipped = [], [], []
    for item in job_list:
        v = item.get("video", "").strip().strip('"')
        if not v:
            continue
        abs_v = os.path.abspath(v)
        if not os.path.exists(abs_v):
            errors.append(f"파일 없음: {os.path.basename(v)}")
            continue
        if abs_v in active_paths:
            skipped.append(os.path.basename(v))
            continue
        sens     = item.get("sensitivity", "normal")
        w        = max(1, min(int(item.get("workers", global_workers)), 8))
        pre_sec  = item.get("pre_sec")
        post_sec = item.get("post_sec")
        jid = jobs.new_job(abs_v, sens, w, pre_sec=pre_sec, post_sec=post_sec)
        jobs.JOB_QUEUE.put(jid)
        added.append(jid)
        active_paths.add(abs_v)
        log.info("잡 추가: %s [%s] (민감도: %s)", os.path.basename(abs_v), jid, sens)

    return jsonify({"added": added, "errors": errors, "skipped": skipped})


@bp_jobs.route("/api/jobs")
def api_jobs():
    with jobs.JLOCK:
        jobs_copy = list(jobs.JOBS.values())
    return jsonify({"jobs": [jobs.job_summary(j) for j in jobs_copy]})


@bp_jobs.route("/api/jobs/<jid>/candidates")
def api_job_cands(jid):
    job = jobs.JOBS.get(jid)
    if not job:
        return jsonify({"error": "없음"}), 404
    conf = float(request.args.get("conf", sh.CONF_AUTO))
    return jsonify(dict(
        candidates=jobs.serialize_candidates(job["candidates"], conf, job["vision_used"]),
        approved=job["approved"],
        vision_used=job["vision_used"],
    ))


@bp_jobs.route("/api/jobs/<jid>/approve", methods=["POST"])
def api_job_approve(jid):
    job = jobs.JOBS.get(jid)
    if not job:
        return jsonify({"error": "없음"}), 404
    approved = (request.json or {}).get("approved", [])
    jobs.update(jid, approved=approved)
    log.info("[%s] 승인 저장: %d개", jid, len(approved))
    return jsonify({"ok": True, "n": len(approved)})


@bp_jobs.route("/api/jobs/approve-all", methods=["POST"])
def api_jobs_approve_all():
    """ready/done 잡에 AI 기본값(conf 기준 auto 플래그) 일괄 적용.

    job_ids가 오면 그 잡만 대상으로 한다 — 원스톱 파이프라인이 큐에 남아 있던
    예전 잡까지 건드리지 않게 하기 위함.
    """
    body = request.json or {}
    conf = float(body.get("conf", sh.CONF_AUTO))
    job_ids = body.get("job_ids")
    id_filter = set(job_ids) if job_ids else None
    saved = []
    with jobs.JLOCK:
        for jid, job in jobs.JOBS.items():
            if id_filter is not None and jid not in id_filter:
                continue
            if job["status"] not in ("ready", "done"):
                continue
            cands = job["candidates"]
            vu = job["vision_used"]
            approved = [i for i, c in enumerate(cands)
                        if jobs.flag(c, conf, vu) == "auto"]
            job["approved"] = approved
            saved.append({"id": jid, "n": len(approved)})
    log.info("일괄 저장: %d개 잡 AI 기본값 적용 (conf=%.2f)", len(saved), conf)
    return jsonify({"saved": saved, "total": len(saved)})


def _try_auto_upload(jid: str, title: str, privacy: str) -> bool:
    """빌드 직후 자동 업로드를 시도한다.

    인증이 없어 건너뛰는 경우 반드시 흔적을 남긴다 — 예전엔 조용히 넘어가서
    "업로드가 안 됐는데 이유도 안 나온다"는 상황이 생겼다.
    """
    if not routes_auth.HAS_YT:
        jobs.update(jid, yt_status="error",
                    yt_error="YouTube 패키지가 설치되지 않아 업로드를 건너뛰었습니다.")
        log.warning("[%s] 자동 업로드 건너뜀 — google-api-python-client 미설치", jid)
        return False
    if not routes_auth.yt_up.is_authenticated():
        jobs.update(jid, yt_status="error",
                    yt_error="YouTube 인증이 없어 업로드를 건너뛰었습니다. 인증 후 [이어서 진행]을 누르세요.")
        log.warning("[%s] 자동 업로드 건너뜀 — YouTube 미인증", jid)
        return False
    threading.Thread(
        target=routes_auth.upload_job_to_youtube,
        args=(jid, title or config.get_default_title(), privacy),
        daemon=True,
    ).start()
    log.info("[%s] 빌드 완료 → 업로드 즉시 시작", jid)
    return True


@bp_jobs.route("/api/jobs/build-all", methods=["POST"])
def api_jobs_build_all():
    body = request.json or {}
    quality     = body.get("quality", "balanced")
    titles      = body.get("titles", {})      # 워터마크용 베이스 제목
    yt_titles   = body.get("yt_titles", {})   # YouTube 업로드용 전체 제목 (auto_upload 시)
    outputs     = body.get("outputs", {})
    auto_upload = bool(body.get("auto_upload", False))  # 빌드 직후 자동 업로드
    job_ids     = body.get("job_ids")                   # None이면 전체 대상
    # 이미 생성된 영상은 다시 만들지 않는다 — 파이프라인 [이어서 진행]이
    # 성공한 빌드를 처음부터 재실행하지 않게 하는 스위치.
    skip_built  = bool(body.get("skip_built", False))
    qk = sh.QUALITY_PRESETS.get(quality, sh.QUALITY_PRESETS["balanced"])
    privacy = config.get_youtube_privacy()
    id_filter = set(job_ids) if job_ids else None

    def _in_scope(jid, job):
        return ((id_filter is None or jid in id_filter)
                and job["status"] in ("ready", "done")
                and job["approved"] is not None)

    with jobs.JLOCK:
        to_build, already_built = [], []
        for jid, job in jobs.JOBS.items():
            if not _in_scope(jid, job) or len(job["approved"]) == 0:
                continue
            if (skip_built and job["status"] == "done" and job.get("output")
                    and os.path.exists(job["output"])):
                already_built.append(jid)
                continue
            to_build.append(jid)
        skipped = [
            job["video_name"] for jid, job in jobs.JOBS.items()
            if _in_scope(jid, job) and len(job["approved"]) == 0
        ]
    if already_built:
        log.info("빌드 건너뜀 (이미 생성됨): %d개", len(already_built))
    if not to_build:
        # 이미 전부 생성돼 있으면 오류가 아니다 — 재개 시 정상 경로.
        if already_built:
            return jsonify({"ok": True, "n": 0, "already_built": len(already_built),
                            "skipped": skipped})
        return jsonify({"error": "승인된 구간이 있는 잡이 없습니다.", "skipped": skipped}), 400

    def _run():
        with jobs.BUILD_LOCK:
            for jid in to_build:
                job = jobs.JOBS.get(jid)
                if not job:
                    continue
                if job.get("cancel_requested"):
                    jobs.finalize_pending_delete(jid)
                    continue
                try:
                    jobs.update(jid, status="building")
                    cands = job["candidates"]
                    selected = sorted(
                        [cands[i] for i in job["approved"] if i < len(cands)],
                        key=lambda x: x["peak"],
                    )
                    title    = titles.get(jid, "").strip()
                    out_name = (outputs.get(jid) or
                                f"highlights_{os.path.splitext(job['video_name'])[0]}.mp4")
                    out_path = (out_name if os.path.isabs(out_name)
                                else str(config.OUTPUT_DIR / out_name))
                    log.info("[%s] 빌드 시작: %d구간 → %s", jid, len(selected), out_path)
                    sh.build_output(
                        job["video"], selected, out_path, Path(job["workdir"]),
                        title=title,
                        preset=qk.get("preset"), crf=qk.get("crf"),
                        copy_mode=qk.get("copy", False),
                        on_progress=lambda d, t, _j=jid: jobs.set_progress(_j, "build", d, t),
                        should_cancel=lambda _j=jid: jobs.is_cancelled(_j),
                        pre_sec=job.get("pre_sec"), post_sec=job.get("post_sec"),
                    )
                    jobs.update(jid, status="done", output=out_path)
                    jobs.clear_progress(jid)
                    log.info("[%s] 빌드 완료: %s", jid, out_path)

                    # 빌드 완료 즉시 업로드 시작 — 다음 잡 빌드와 병렬 처리
                    if auto_upload:
                        _try_auto_upload(jid, yt_titles.get(jid) or title, privacy)

                except Exception as e:
                    jobs.update(jid, status="error", error=str(e)[:300])
                    jobs.clear_progress(jid)
                    log.exception("[%s] 빌드 실패", jid)
                finally:
                    jobs.finalize_pending_delete(jid)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "n": len(to_build),
                    "already_built": len(already_built), "skipped": skipped})


@bp_jobs.route("/api/jobs/<jid>/output")
def api_job_output(jid):
    job = jobs.JOBS.get(jid)
    if not job or not job["output"] or not os.path.exists(job["output"]):
        return "no output", 404
    return send_file(job["output"], mimetype="video/mp4")


@bp_jobs.route("/api/jobs/<jid>/source")
def api_job_source(jid):
    """리뷰 탭 구간 미리보기용 원본 영상 스트리밍 (Range 요청 지원)."""
    job = jobs.JOBS.get(jid)
    if not job or not job.get("video") or not os.path.exists(job["video"]):
        return "no source", 404
    return send_file(job["video"], conditional=True)


@bp_jobs.route("/api/jobs/<jid>/thumb/<int:idx>")
def api_job_thumb(jid, idx):
    """후보 구간 대표 프레임 썸네일. 비전 판별용으로 추출된 프레임을 재사용하되,
    서버 재시작 등으로 임시 디렉토리가 지워진 경우 온디맨드로 대표 프레임을 추출한다."""
    job = jobs.JOBS.get(jid)
    workdir = job.get("workdir") if job else None
    if not workdir:
        return "no thumbnail", 404

    # 임시 디렉토리 생성 보장 (재시작 시 정리되었을 수 있음)
    Path(workdir).mkdir(parents=True, exist_ok=True)

    frames = sorted(Path(workdir).glob(f"cand{idx}_*.jpg"))
    if not frames:
        try:
            # candidates 범위 체크
            cands = job.get("candidates") or []
            if idx >= len(cands):
                return "no thumbnail", 404
            cand = cands[idx]
            peak = float(cand["peak"])

            # 앞뒤 길이 설정
            pre_sec = job.get("pre_sec", sh.PRE_SEC)
            post_sec = job.get("post_sec", sh.POST_SEC)
            seg_start = max(0.0, peak - pre_sec)
            seg_len = pre_sec + post_sec
            thumb_ts = seg_start + (seg_len / 2.0)

            thumb_path = Path(workdir) / f"cand{idx}_000.jpg"
            cmd_thumb = [
                "ffmpeg", "-y", "-ss", f"{thumb_ts:.2f}", "-i", job["video"],
                "-vframes", "1", "-vf", "scale=640:-1",
                str(thumb_path), "-loglevel", "error"
            ]
            sh.run(cmd_thumb)
            frames = [thumb_path]
        except Exception:
            log.exception("[%s] 온디맨드 썸네일 추출 실패", jid)
            return "no thumbnail", 404

    pick = frames[len(frames) // 2]  # 구간 중간 프레임을 대표 이미지로 사용
    return send_file(pick, mimetype="image/jpeg")


@bp_jobs.route("/api/jobs/<jid>/delete", methods=["POST"])
def api_job_delete(jid):
    with jobs.JLOCK:
        job = jobs.JOBS.get(jid)
        if job is None:
            return jsonify({"ok": True})
        if job["status"] in jobs.ACTIVE_STATUSES:
            # 처리 중인 잡은 즉시 제거하지 않고 취소 플래그만 세운다.
            # 워커/빌드 루프가 다음 체크포인트에서 감지해 실제로 정리한다.
            job["cancel_requested"] = True
            job["pending_delete"] = True
            log.info("[%s] 처리 중 삭제 요청 — 취소 후 정리 예정", jid)
            jobs.delete_state_file(jid)
            return jsonify({"ok": True, "deferred": True})
        job = jobs.JOBS.pop(jid, None)
    if job and job.get("workdir") and os.path.exists(job["workdir"]):
        shutil.rmtree(job["workdir"], ignore_errors=True)
    jobs.delete_results_file(jid)
    jobs.delete_state_file(jid)
    log.info("잡 삭제: %s", jid)
    return jsonify({"ok": True})


@bp_jobs.route("/api/jobs/<jid>/cancel", methods=["POST"])
def api_job_cancel(jid):
    with jobs.JLOCK:
        job = jobs.JOBS.get(jid)
        if job is None:
            return jsonify({"error": "없음"}), 404
        if job["status"] not in jobs.ACTIVE_STATUSES:
            return jsonify({"error": "처리 중인 잡만 취소할 수 있습니다."}), 400
        job["cancel_requested"] = True
    log.info("[%s] 취소 요청", jid)
    return jsonify({"ok": True})


@bp_jobs.route("/api/jobs/<jid>/retry", methods=["POST"])
def api_job_retry(jid):
    """오류 상태인 잡을 같은 설정으로 다시 큐에 넣어 재처리.

    stage="build"면 분석 결과(후보·승인)를 그대로 두고 "ready"로만 되돌린다 —
    인코딩 단계에서만 실패한 잡을 몇 분짜리 재분석 없이 다시 만들기 위함.
    """
    job = jobs.JOBS.get(jid)
    if not job:
        return jsonify({"error": "잡 없음"}), 404
    if job["status"] != "error":
        return jsonify({"error": "오류 상태인 잡만 재시도할 수 있습니다."}), 400

    stage = (request.json or {}).get("stage", "")
    if stage == "build":
        if not job.get("candidates"):
            return jsonify({"error": "분석 결과가 없어 빌드만 재시도할 수 없습니다."}), 400
        jobs.update(jid, status="ready", error=None, output=None,
                    progress=dict(stage=None, done=0, total=0),
                    yt_status=None, yt_url=None, yt_error=None,
                    cancel_requested=False, pending_delete=False)
        log.info("[%s] 빌드 단계만 재시도 — 분석 결과 유지 (후보 %d개)", jid, len(job["candidates"]))
        return jsonify({"ok": True, "stage": "build"})

    if job.get("workdir") and os.path.exists(job["workdir"]):
        shutil.rmtree(job["workdir"], ignore_errors=True)
    
    # 기존 candidates가 있으면 유지한다 (분류 재시도 시 이미 완료된 비전 캐시 활용을 위함)
    cands = job.get("candidates") or []
    
    jobs.update(jid,
         status="pending", error=None, candidates=cands,
         usage=None, workdir=None, duration=None, approved=None,
         output=None,
         progress=dict(stage=None, done=0, total=0),
         started_at=None, finished_at=None,
         yt_status=None, yt_url=None, yt_error=None,
         band_status=None, band_post_url=None, band_error=None,
         cancel_requested=False, pending_delete=False)
    jobs.JOB_QUEUE.put(jid)
    log.info("[%s] 재시도 요청 — 큐에 재투입 (기존 후보 %d개 유지)", jid, len(cands))
    return jsonify({"ok": True})


@bp_jobs.route("/api/jobs/<jid>/audio-signal")
def api_job_audio_signal(jid):
    job = jobs.JOBS.get(jid)
    if not job:
        return jsonify({"error": "없음"}), 404
        
    # 메모리 캐시 확인
    if "audio_signal" in job:
        return jsonify(job["audio_signal"])
        
    try:
        workdir = Path(job["workdir"])
        workdir.mkdir(parents=True, exist_ok=True)
        
        signal = sh.get_audio_signal(job["video"], workdir, job.get("sensitivity", "normal"))
        
        # 메모리 캐싱
        job["audio_signal"] = signal
        return jsonify(signal)
    except Exception as e:
        log.exception("[%s] 오디오 시그널 추출 실패", jid)
        return jsonify({"error": str(e)}), 500


@bp_jobs.route("/api/jobs/<jid>/candidates/add-manual", methods=["POST"])
def api_job_add_manual_cand(jid):
    job = jobs.JOBS.get(jid)
    if not job:
        return jsonify({"error": "없음"}), 404
    body = request.json or {}
    peak = float(body.get("peak", 0.0))
    
    # 수동 하이라이트 후보 생성
    new_cand = {
        "start": max(0.0, peak - job.get("pre_sec", sh.PRE_SEC)),
        "end": peak + job.get("post_sec", sh.POST_SEC),
        "peak": peak,
        "delta_db": 0.0,
        "highlight": True,
        "type": "manual",
        "confidence": 1.0,
        "reason": "사용자 수동 추가"
    }
    
    with jobs.JLOCK:
        job["candidates"].append(new_cand)
        idx = len(job["candidates"]) - 1
        if job["approved"] is None:
            conf_auto = sh.CONF_AUTO
            vu = job["vision_used"]
            job["approved"] = [i for i, c in enumerate(job["candidates"])
                               if jobs.flag(c, conf_auto, vu) == "auto"]
        else:
            job["approved"].append(idx)
            
    # 디스크에 즉시 보존
    jobs.save_job_state(jid)
    
    return jsonify({
        "ok": True,
        "idx": idx,
        "candidate": new_cand,
        "approved": job["approved"]
    })
