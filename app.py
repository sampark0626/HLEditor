#!/usr/bin/env python3
"""
app.py — 축구 하이라이트 추출기 배치 웹 UI
"""

import os
import queue
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file
import logging

import soccer_highlights as sh

# YouTube / BAND 모듈 (선택적 — 패키지 미설치 시 기능 비활성화)
try:
    import youtube_uploader as yt_up
    _HAS_YT = True
except Exception:
    _HAS_YT = False

try:
    import band_poster as bp
    _HAS_BAND = True
except Exception:
    _HAS_BAND = False

app = Flask(__name__)

LOG_PATH = os.path.join(os.path.dirname(__file__), "app.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("hl")

# ─── 잡 상태 관리 ─────────────────────────────────────────
JOBS = {}
JOB_QUEUE = queue.Queue()
_JLOCK = threading.Lock()
_BUILD_LOCK = threading.Lock()


def _new_job(video, sensitivity="normal", workers=sh.VISION_WORKERS):
    jid = uuid.uuid4().hex[:8]
    job = dict(
        id=jid,
        video=os.path.abspath(video),
        video_name=os.path.basename(video),
        sensitivity=sensitivity,
        workers=workers,
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
    )
    with _JLOCK:
        JOBS[jid] = job
    return jid


def _upd(jid, **kw):
    with _JLOCK:
        JOBS[jid].update(kw)


def _set_prog(jid, stage, done, total):
    with _JLOCK:
        JOBS[jid]["progress"] = dict(stage=stage, done=done, total=total)


def _clr_prog(jid):
    with _JLOCK:
        JOBS[jid]["progress"] = dict(stage=None, done=0, total=0)


# ─── API 키 ───────────────────────────────────────────────
def load_api_key():
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ─── 오류 메시지 정리 ────────────────────────────────────
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


# ─── 백그라운드 워커 (순차) ───────────────────────────────
def _worker():
    while True:
        jid = JOB_QUEUE.get()
        if jid is None:
            break
        try:
            _process(jid)
        except Exception as exc:
            _upd(jid, status="error", error=_friendly_error(exc))
            log.exception("[%s] 처리 실패", jid)
        finally:
            JOB_QUEUE.task_done()


def _process(jid):
    job = JOBS[jid]
    video = job["video"]
    sp = sh.SENSITIVITY_PRESETS.get(job["sensitivity"], sh.SENSITIVITY_PRESETS["normal"])

    wd = Path(tempfile.mkdtemp(prefix=f"hl_{jid}_"))
    t_start = time.monotonic()
    _upd(jid, workdir=str(wd), status="detecting", started_at=t_start)
    log.info("[%s] 검출 시작: %s (민감도: %s)", jid, video, job["sensitivity"])

    dur = sh.probe_duration(video)
    cands = sh.detect_spikes(video, wd, percentile=sp["percentile"], min_db=sp["min_db"])
    _upd(jid, duration=dur, candidates=cands)
    log.info("[%s] 검출 완료: %.1fs, 후보 %d개", jid, dur, len(cands))

    key = load_api_key()
    if not key:
        _upd(jid, status="ready", vision_used=False, finished_at=time.monotonic())
        log.warning("[%s] API 키 없음 — 비전 생략", jid)
        return

    from google import genai
    client = genai.Client(api_key=key)
    _upd(jid, status="classifying")
    _set_prog(jid, "classify", 0, len(cands))

    usage = sh.classify_all_parallel(
        cands, video, wd, client, sh.CONF_AUTO, job["workers"],
        on_progress=lambda d, t: _set_prog(jid, "classify", d, t),
    )
    cost = (usage["in"] / 1e6 * sh.PRICE_IN_PER_M +
            usage["out"] / 1e6 * sh.PRICE_OUT_PER_M)
    usage["cost_usd"] = round(cost, 4)

    sh.save_results(
        os.path.join(os.path.dirname(__file__), f"results_{jid}.json"),
        video, dur, cands, True,
    )
    _upd(jid, status="ready", vision_used=True, usage=usage,
         candidates=cands, finished_at=time.monotonic())
    _clr_prog(jid)
    log.info("[%s] 판별 완료: $%.4f", jid, cost)


# ─── 직렬화 ───────────────────────────────────────────────
def _flag(c, conf_auto, vision_used):
    if not vision_used:
        return "auto"
    conf = float(c.get("confidence", 0))
    if c.get("highlight") and conf >= conf_auto:
        return "auto"
    if c.get("highlight") and conf >= sh.CONF_MAYBE:
        return "maybe"
    return "reject"


def _job_summary(job):
    cands = job["candidates"]
    vu = job["vision_used"]
    n_auto  = sum(1 for c in cands if _flag(c, sh.CONF_AUTO, vu) == "auto")
    n_maybe = sum(1 for c in cands if _flag(c, sh.CONF_AUTO, vu) == "maybe")
    elapsed = None
    if job["started_at"] and job["finished_at"]:
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
        yt_status=job.get("yt_status"),
        yt_url=job.get("yt_url"),
        yt_error=job.get("yt_error"),
        yt_progress=job.get("yt_progress", {"done": 0, "total": 0}),
        band_status=job.get("band_status"),
        band_post_url=job.get("band_post_url"),
        band_error=job.get("band_error"),
        match_date=job.get("match_date"),
    )


def _serialize_cands(cands, conf_auto, vision_used):
    out = []
    for i, c in enumerate(cands):
        conf = float(c.get("confidence", 0))
        out.append(dict(
            idx=i,
            peak=round(float(c["peak"]), 1),
            delta_db=round(float(c.get("delta_db", 0)), 1),
            type=c.get("type", "-"),
            confidence=round(conf, 2),
            reason=c.get("reason", ""),
            highlight=bool(c.get("highlight", False)),
            flag=_flag(c, conf_auto, vision_used),
        ))
    return out


# ─── 라우트 ───────────────────────────────────────────────
def _ffmpeg_ok() -> bool:
    """ffmpeg + ffprobe 가 PATH에 있는지 확인 (캐시 없이 매번 체크)."""
    import subprocess as _sp
    for tool in ("ffmpeg", "ffprobe"):
        try:
            _sp.run([tool, "-version"], capture_output=True, check=False)
        except FileNotFoundError:
            return False
    return True


@app.route("/")
def index():
    yt_auth = _HAS_YT and yt_up.is_authenticated()
    yt_has_secrets = _HAS_YT and yt_up.has_client_secrets()
    band_auth = _HAS_BAND and bp.is_authenticated()
    band_has_creds = _HAS_BAND and bp.has_credentials()
    return render_template("index.html",
                           conf_auto=sh.CONF_AUTO,
                           conf_maybe=sh.CONF_MAYBE,
                           workers=sh.VISION_WORKERS,
                           has_key=bool(load_api_key()),
                           ffmpeg_ok=_ffmpeg_ok(),
                           sensitivities=sh.SENSITIVITY_PRESETS,
                           qualities=sh.QUALITY_PRESETS,
                           yt_auth=yt_auth,
                           yt_has_secrets=yt_has_secrets,
                           band_auth=band_auth,
                           band_has_creds=band_has_creds)


@app.route("/api/browse", methods=["POST"])
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


@app.route("/api/jobs/add", methods=["POST"])
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
    with _JLOCK:
        active_paths = {j["video"] for j in JOBS.values()
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
        sens = item.get("sensitivity", "normal")
        w = max(1, min(int(item.get("workers", global_workers)), 8))
        jid = _new_job(abs_v, sens, w)
        JOB_QUEUE.put(jid)
        added.append(jid)
        active_paths.add(abs_v)
        log.info("잡 추가: %s [%s] (민감도: %s)", os.path.basename(abs_v), jid, sens)

    return jsonify({"added": added, "errors": errors, "skipped": skipped})


@app.route("/api/jobs")
def api_jobs():
    with _JLOCK:
        jobs_copy = list(JOBS.values())
    return jsonify({"jobs": [_job_summary(j) for j in jobs_copy]})


@app.route("/api/jobs/<jid>/candidates")
def api_job_cands(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "없음"}), 404
    conf = float(request.args.get("conf", sh.CONF_AUTO))
    return jsonify(dict(
        candidates=_serialize_cands(job["candidates"], conf, job["vision_used"]),
        approved=job["approved"],
        vision_used=job["vision_used"],
    ))


@app.route("/api/jobs/<jid>/approve", methods=["POST"])
def api_job_approve(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "없음"}), 404
    approved = (request.json or {}).get("approved", [])
    _upd(jid, approved=approved)
    log.info("[%s] 승인 저장: %d개", jid, len(approved))
    return jsonify({"ok": True, "n": len(approved)})


@app.route("/api/jobs/approve-all", methods=["POST"])
def api_jobs_approve_all():
    """모든 ready/done 잡에 AI 기본값(conf 기준 auto 플래그) 일괄 적용."""
    body = request.json or {}
    conf = float(body.get("conf", sh.CONF_AUTO))
    saved = []
    with _JLOCK:
        for jid, job in JOBS.items():
            if job["status"] not in ("ready", "done"):
                continue
            cands = job["candidates"]
            vu = job["vision_used"]
            approved = [i for i, c in enumerate(cands)
                        if _flag(c, conf, vu) == "auto"]
            job["approved"] = approved
            saved.append({"id": jid, "n": len(approved)})
    log.info("일괄 저장: %d개 잡 AI 기본값 적용 (conf=%.2f)", len(saved), conf)
    return jsonify({"saved": saved, "total": len(saved)})


@app.route("/api/jobs/build-all", methods=["POST"])
def api_jobs_build_all():
    body = request.json or {}
    quality = body.get("quality", "balanced")
    titles  = body.get("titles", {})
    outputs = body.get("outputs", {})
    qk = sh.QUALITY_PRESETS.get(quality, sh.QUALITY_PRESETS["balanced"])

    to_build = [
        jid for jid, job in JOBS.items()
        if job["status"] in ("ready", "done")
        and job["approved"] is not None
        and len(job["approved"]) > 0
    ]
    # 저장은 됐지만 승인 구간이 0개인 잡 (하이라이트 없음 → 빌드 제외)
    skipped = [
        job["video_name"] for job in JOBS.values()
        if job["status"] in ("ready", "done")
        and job["approved"] is not None
        and len(job["approved"]) == 0
    ]
    if not to_build:
        return jsonify({"error": "승인된 구간이 있는 잡이 없습니다.", "skipped": skipped}), 400

    def _run():
        with _BUILD_LOCK:
            for jid in to_build:
                job = JOBS.get(jid)
                if not job:
                    continue
                try:
                    _upd(jid, status="building")
                    cands = job["candidates"]
                    selected = sorted(
                        [cands[i] for i in job["approved"] if i < len(cands)],
                        key=lambda x: x["peak"],
                    )
                    title    = titles.get(jid, "").strip()
                    out_name = (outputs.get(jid) or
                                f"highlights_{os.path.splitext(job['video_name'])[0]}.mp4")
                    out_path = (out_name if os.path.isabs(out_name)
                                else os.path.join(os.path.dirname(__file__), out_name))
                    log.info("[%s] 빌드 시작: %d구간 → %s", jid, len(selected), out_path)
                    sh.build_output(
                        job["video"], selected, out_path, Path(job["workdir"]),
                        title=title,
                        preset=qk.get("preset"), crf=qk.get("crf"),
                        copy_mode=qk.get("copy", False),
                        on_progress=lambda d, t, _j=jid: _set_prog(_j, "build", d, t),
                    )
                    _upd(jid, status="done", output=out_path)
                    _clr_prog(jid)
                    log.info("[%s] 빌드 완료: %s", jid, out_path)
                except Exception as e:
                    _upd(jid, status="error", error=str(e)[:300])
                    _clr_prog(jid)
                    log.exception("[%s] 빌드 실패", jid)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "n": len(to_build), "skipped": skipped})


@app.route("/api/jobs/<jid>/output")
def api_job_output(jid):
    job = JOBS.get(jid)
    if not job or not job["output"] or not os.path.exists(job["output"]):
        return "no output", 404
    return send_file(job["output"], mimetype="video/mp4")


@app.route("/api/jobs/<jid>/delete", methods=["POST"])
def api_job_delete(jid):
    with _JLOCK:
        job = JOBS.pop(jid, None)
    if job and job.get("workdir") and os.path.exists(job["workdir"]):
        shutil.rmtree(job["workdir"], ignore_errors=True)
    log.info("잡 삭제: %s", jid)
    return jsonify({"ok": True})


@app.route("/api/jobs/<jid>/retry", methods=["POST"])
def api_job_retry(jid):
    """오류 상태인 잡을 같은 설정으로 다시 큐에 넣어 재처리."""
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "잡 없음"}), 404
    if job["status"] != "error":
        return jsonify({"error": "오류 상태인 잡만 재시도할 수 있습니다."}), 400
    if job.get("workdir") and os.path.exists(job["workdir"]):
        shutil.rmtree(job["workdir"], ignore_errors=True)
    _upd(jid,
         status="pending", error=None, candidates=[], vision_used=False,
         usage=None, workdir=None, duration=None, approved=None,
         output=None,
         progress=dict(stage=None, done=0, total=0),
         started_at=None, finished_at=None,
         yt_status=None, yt_url=None, yt_error=None,
         band_status=None, band_post_url=None, band_error=None)
    JOB_QUEUE.put(jid)
    log.info("[%s] 재시도 요청 — 큐에 재투입", jid)
    return jsonify({"ok": True})


# ─── YouTube OAuth ────────────────────────────────────────
def _yt_redirect_uri():
    return "http://localhost:5000/auth/youtube/callback"


@app.route("/auth/youtube")
def auth_youtube():
    if not _HAS_YT:
        return "google-auth-oauthlib 패키지 없음. pip install google-auth-oauthlib", 500
    try:
        url = yt_up.get_auth_url(_yt_redirect_uri())
        return redirect(url)
    except FileNotFoundError as e:
        return str(e), 400


@app.route("/auth/youtube/callback")
def auth_youtube_callback():
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return "인증 코드 없음", 400
    try:
        yt_up.exchange_code(code, _yt_redirect_uri(), state=state)
        log.info("YouTube 인증 완료")
        return redirect("/?yt=ok")
    except Exception as e:
        log.exception("YouTube 인증 실패")
        return f"YouTube 인증 실패: {e}", 500


@app.route("/api/auth/youtube/status")
def api_yt_status():
    if not _HAS_YT:
        return jsonify({"ok": False, "reason": "패키지 없음"})
    authenticated = yt_up.is_authenticated()
    channel = yt_up.get_channel_info() if authenticated else None
    return jsonify({
        "ok": authenticated,
        "has_secrets": yt_up.has_client_secrets(),
        "channel": channel,
    })


@app.route("/auth/youtube/revoke", methods=["POST"])
def auth_youtube_revoke():
    if _HAS_YT:
        yt_up.revoke()
    return jsonify({"ok": True})


# ─── YouTube 업로드 ───────────────────────────────────────
def _upload_job_to_youtube(jid: str, title: str, privacy: str):
    """배경 스레드: 하이라이트 영상 → YouTube 업로드."""
    import tempfile as _tmp

    job = JOBS.get(jid)
    if not job:
        return
    output = job.get("output")
    if not output or not os.path.exists(output):
        _upd(jid, yt_status="error", yt_error="출력 파일 없음")
        return

    _upd(jid, yt_status="uploading", yt_progress={"done": 0, "total": 0}, yt_error=None)
    log.info("[%s] YouTube 업로드 시작: %s", jid, title)

    thumb_path = None
    try:
        # 썸네일 추출: 가장 신뢰도 높은 승인 후보
        approved = job.get("approved") or []
        cands     = job.get("candidates") or []
        if approved and cands and _HAS_YT:
            best = max(
                (cands[i] for i in approved if i < len(cands)),
                key=lambda c: float(c.get("confidence") or 0),
                default=None,
            )
            if best:
                tf = _tmp.NamedTemporaryFile(suffix=".jpg", delete=False)
                tf.close()
                peak = float(best.get("peak", 0))
                if yt_up.extract_thumbnail(job["video"], peak, tf.name):
                    thumb_path = tf.name
                else:
                    try: os.unlink(tf.name)
                    except Exception: pass

        # 설명 생성
        desc = yt_up.generate_description(cands, approved, job["video_name"])

        def _on_prog(done, total):
            _upd(jid, yt_progress={"done": done, "total": total})

        yt_url = yt_up.upload_video(
            output, title, desc,
            thumbnail_path=thumb_path,
            privacy=privacy,
            on_progress=_on_prog,
        )
        _upd(jid, yt_status="done", yt_url=yt_url, yt_progress={"done": 1, "total": 1})
        log.info("[%s] YouTube 업로드 완료: %s", jid, yt_url)
    except Exception as e:
        msg = str(e)[:300]
        _upd(jid, yt_status="error", yt_error=msg)
        log.exception("[%s] YouTube 업로드 실패", jid)
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try: os.unlink(thumb_path)
            except Exception: pass


@app.route("/api/jobs/<jid>/upload-youtube", methods=["POST"])
def api_upload_youtube(jid):
    if not _HAS_YT:
        return jsonify({"error": "google-auth-oauthlib 패키지 없음"}), 500
    if not yt_up.is_authenticated():
        return jsonify({"error": "YouTube 인증 필요. /auth/youtube 로 인증하세요."}), 401
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "잡 없음"}), 404
    if job.get("status") != "done" or not job.get("output"):
        return jsonify({"error": "영상이 아직 생성되지 않았습니다."}), 400
    if job.get("yt_status") == "uploading":
        return jsonify({"error": "업로드 중입니다."}), 409

    body    = request.json or {}
    title   = body.get("title", "한울타리 FC 경기영상").strip() or "한울타리 FC 경기영상"
    privacy = _get_yt_privacy()

    threading.Thread(
        target=_upload_job_to_youtube,
        args=(jid, title, privacy),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/jobs/upload-all-youtube", methods=["POST"])
def api_upload_all_youtube():
    if not _HAS_YT:
        return jsonify({"error": "google-auth-oauthlib 패키지 없음"}), 500
    if not yt_up.is_authenticated():
        return jsonify({"error": "YouTube 인증 필요"}), 401

    body    = request.json or {}
    titles  = body.get("titles", {})
    privacy = _get_yt_privacy()

    to_upload = [
        j for j in JOBS.values()
        if j.get("status") == "done"
        and j.get("output")
        and j.get("yt_status") not in ("uploading", "done")
    ]
    if not to_upload:
        return jsonify({"error": "업로드할 영상이 없습니다."}), 400

    for job in to_upload:
        jid   = job["id"]
        title = titles.get(jid, "한울타리 FC 경기영상").strip() or "한울타리 FC 경기영상"
        threading.Thread(
            target=_upload_job_to_youtube,
            args=(jid, title, privacy),
            daemon=True,
        ).start()

    return jsonify({"ok": True, "n": len(to_upload)})


def _get_yt_privacy() -> str:
    """YOUTUBE_PRIVACY .env 값 (기본: public)."""
    env = {}
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return os.environ.get("YOUTUBE_PRIVACY") or env.get("YOUTUBE_PRIVACY", "public")


# ─── BAND OAuth ───────────────────────────────────────────
def _band_redirect_uri():
    return "http://localhost:5000/auth/band/callback"


@app.route("/auth/band")
def auth_band():
    if not _HAS_BAND:
        return "requests 패키지 없음. pip install requests", 500
    try:
        url = bp.get_auth_url(_band_redirect_uri())
        return redirect(url)
    except ValueError as e:
        return str(e), 400


@app.route("/auth/band/callback")
def auth_band_callback():
    code = request.args.get("code", "")
    if not code:
        return "인증 코드 없음", 400
    try:
        bp.exchange_code(code, _band_redirect_uri())
        log.info("BAND 인증 완료")
        return redirect("/?band=ok")
    except Exception as e:
        log.exception("BAND 인증 실패")
        return f"BAND 인증 실패: {e}", 500


@app.route("/api/auth/band/status")
def api_band_status():
    if not _HAS_BAND:
        return jsonify({"ok": False, "reason": "패키지 없음"})
    authenticated = bp.is_authenticated()
    bands = []
    if authenticated:
        try:
            bands = bp.get_bands()
        except Exception:
            pass
    return jsonify({
        "ok": authenticated,
        "has_creds": bp.has_credentials(),
        "bands": bands,
    })


@app.route("/auth/band/revoke", methods=["POST"])
def auth_band_revoke():
    if _HAS_BAND:
        bp.revoke()
    return jsonify({"ok": True})


# ─── BAND 게시 ────────────────────────────────────────────
@app.route("/api/jobs/post-band", methods=["POST"])
def api_post_band():
    if not _HAS_BAND:
        return jsonify({"error": "requests 패키지 없음"}), 500
    if not bp.is_authenticated():
        return jsonify({"error": "BAND 인증 필요. /auth/band 로 인증하세요."}), 401

    body     = request.json or {}
    band_key = body.get("band_key", "").strip()
    if not band_key:
        # .env의 BAND_TARGET_KEY 사용
        env_path = Path(__file__).parent / ".env"
        for line in (env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []):
            line = line.strip()
            if line.startswith("BAND_TARGET_KEY=") and not line.startswith("#"):
                band_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not band_key:
        return jsonify({"error": "밴드를 선택하거나 BAND_TARGET_KEY를 .env에 설정하세요."}), 400

    # 업로드 완료된 잡들을 날짜별 그룹핑
    uploaded = [
        {
            "id":         j["id"],
            "video_name": j["video_name"],
            "yt_url":     j.get("yt_url", ""),
            "match_date": j.get("match_date", date.today().isoformat()),
        }
        for j in JOBS.values()
        if j.get("yt_url") and j.get("yt_status") == "done"
        and j.get("band_status") not in ("posting", "done")
    ]
    if not uploaded:
        return jsonify({"error": "BAND에 게시할 YouTube 링크가 없습니다."}), 400

    groups = bp.group_by_date(uploaded)

    results = []
    errors  = []
    for match_date, video_links in groups.items():
        content = bp.format_post_content(video_links, match_date=match_date)
        try:
            post_url = bp.write_post(band_key, content)
            # 해당 잡들 상태 업데이트
            for vname, vurl in video_links:
                for j in JOBS.values():
                    if j.get("yt_url") == vurl:
                        _upd(j["id"], band_status="done", band_post_url=post_url)
            results.append({"date": match_date, "n": len(video_links), "url": post_url})
            log.info("BAND 게시 완료: %s, %d개 영상", match_date, len(video_links))
        except Exception as e:
            errors.append({"date": match_date, "error": str(e)[:200]})
            log.exception("BAND 게시 실패: %s", match_date)

    return jsonify({"posted": results, "errors": errors})


def _check_ffmpeg():
    """ffmpeg/ffprobe 설치 여부를 확인하고 없으면 경고."""
    import subprocess as _sp
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        try:
            _sp.run([tool, "-version"], capture_output=True, check=False)
        except FileNotFoundError:
            missing.append(tool)
    if missing:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  [경고] {', '.join(missing)} 를 찾을 수 없습니다!")
        print(f"  영상 처리를 하려면 ffmpeg 설치가 필요합니다.")
        print(f"")
        print(f"  Windows 설치 방법 (관리자 PowerShell):")
        print(f"    winget install Gyan.FFmpeg")
        print(f"  또는: https://ffmpeg.org/download.html")
        print(f"{sep}\n")
    return len(missing) == 0


if __name__ == "__main__":
    _check_ffmpeg()
    worker_t = threading.Thread(target=_worker, daemon=True, name="hl-worker")
    worker_t.start()
    url = "http://127.0.0.1:5000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  ▶ 하이라이트 추출기 UI: {url}\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
