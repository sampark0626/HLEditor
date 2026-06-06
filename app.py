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
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
import logging

import soccer_highlights as sh

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


# ─── 백그라운드 워커 (순차) ───────────────────────────────
def _worker():
    while True:
        jid = JOB_QUEUE.get()
        if jid is None:
            break
        try:
            _process(jid)
        except Exception:
            _upd(jid, status="error", error=traceback.format_exc()[-400:])
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
@app.route("/")
def index():
    return render_template("index.html",
                           conf_auto=sh.CONF_AUTO,
                           conf_maybe=sh.CONF_MAYBE,
                           workers=sh.VISION_WORKERS,
                           has_key=bool(load_api_key()),
                           sensitivities=sh.SENSITIVITY_PRESETS,
                           qualities=sh.QUALITY_PRESETS)


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
    if not to_build:
        return jsonify({"error": "승인된 구간이 있는 잡이 없습니다."}), 400

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
    return jsonify({"ok": True, "n": len(to_build)})


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


if __name__ == "__main__":
    worker_t = threading.Thread(target=_worker, daemon=True, name="hl-worker")
    worker_t.start()
    url = "http://127.0.0.1:5000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  ▶ 하이라이트 추출기 UI: {url}\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
