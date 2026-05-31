#!/usr/bin/env python3
"""
app.py — 축구 하이라이트 추출기 로컬 웹 UI
============================================
soccer_highlights.py 의 파이프라인 함수를 그대로 재사용하는 얇은 웹 래퍼.

흐름:
  1) /api/detect   : 오디오 볼륨 급증 후보 검출 (무료, API 호출 없음)
  2) /api/classify : Gemini 비전 판별 (병렬). 결과를 세션에 저장
  3) /api/select   : 신뢰도 임계만 바꿔 즉시 재선택 (API 재호출 없음)
  4) /api/build    : 채택 구간을 잘라 이어붙여 최종 영상 생성

실행:
  pip install flask
  python app.py        →  http://127.0.0.1:5000
"""

import os
import tempfile
import threading
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

import logging

import soccer_highlights as sh

app = Flask(__name__)

# 파일 + 콘솔 로깅: 진단 시 app.log 를 읽으면 단계별 요청 흐름을 추적할 수 있다.
LOG_PATH = os.path.join(os.path.dirname(__file__), "app.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("hl")

# 단일 사용자 로컬 도구이므로 메모리 내 단일 세션 상태로 충분하다.
SESSION = {
    "video": None,       # 입력 영상 절대경로
    "duration": None,    # 초
    "candidates": [],    # 비전 결과까지 포함된 후보 dict 리스트
    "vision_used": False,
    "workdir": None,     # 오디오/프레임/클립용 임시 작업 폴더
    "output": None,      # 마지막 생성 영상 경로
    "usage": None,       # 마지막 비전 판별 토큰/비용 요약
}
CANCEL = threading.Event()  # 비전 판별 중단 신호

# 진행 상태(프로그레스 바용). UI가 /api/progress 로 폴링한다.
PROGRESS = {"stage": None, "done": 0, "total": 0}
_PLOCK = threading.Lock()


def set_progress(stage, done, total):
    with _PLOCK:
        PROGRESS.update({"stage": stage, "done": done, "total": total})


def clear_progress():
    with _PLOCK:
        PROGRESS.update({"stage": None, "done": 0, "total": 0})
_LOCK = threading.Lock()  # classify/build 같은 장시간 작업 직렬화


# ----------------------------------------------------------------------------
def load_api_key():
    """--api-key 없이 .env / 환경변수에서 GEMINI_API_KEY 를 읽는다."""
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


def fresh_workdir():
    """세션 작업 폴더를 새로 만든다 (이전 것은 정리)."""
    old = SESSION.get("workdir")
    if old and Path(old).exists():
        import shutil
        shutil.rmtree(old, ignore_errors=True)
    wd = Path(tempfile.mkdtemp(prefix="hl_ui_"))
    SESSION["workdir"] = str(wd)
    return wd


def flag_for(c, conf_auto):
    """후보 하나의 분류 라벨(auto/maybe/reject) 계산."""
    if not SESSION["vision_used"]:
        return "auto"
    conf = float(c.get("confidence", 0))
    if c.get("highlight") and conf >= conf_auto:
        return "auto"
    if c.get("highlight") and conf >= sh.CONF_MAYBE:
        return "maybe"
    return "reject"


def serialize(conf_auto):
    """프론트로 보낼 후보 리스트 직렬화."""
    out = []
    for i, c in enumerate(SESSION["candidates"]):
        out.append({
            "idx": i,
            "peak": round(float(c["peak"]), 1),
            "delta_db": round(float(c.get("delta_db", 0)), 1),
            "type": c.get("type", "-"),
            "confidence": round(float(c.get("confidence", 0)), 2),
            "reason": c.get("reason", ""),
            "highlight": bool(c.get("highlight", False)),
            "flag": flag_for(c, conf_auto),
        })
    return out


# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html",
                           conf_auto=sh.CONF_AUTO,
                           workers=sh.VISION_WORKERS,
                           has_key=bool(load_api_key()),
                           sensitivities=sh.SENSITIVITY_PRESETS,
                           qualities=sh.QUALITY_PRESETS)


@app.route("/api/browse", methods=["POST"])
def api_browse():
    """서버(로컬)에서 네이티브 파일 선택 대화상자를 띄워 경로를 돌려준다."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="경기 영상 선택",
            filetypes=[("동영상", "*.mp4 *.mov *.mkv *.avi *.m4v"),
                       ("모든 파일", "*.*")])
        root.destroy()
        return jsonify({"path": path or ""})
    except Exception as e:
        return jsonify({"path": "", "error": str(e)})


@app.route("/api/detect", methods=["POST"])
def api_detect():
    body = request.json or {}
    video = body.get("video", "").strip().strip('"')
    sens = body.get("sensitivity", "normal")
    sp = sh.SENSITIVITY_PRESETS.get(sens, sh.SENSITIVITY_PRESETS["normal"])
    log.info("DETECT 요청: video=%r, 민감도=%s", video, sens)
    if not video or not os.path.exists(video):
        log.warning("DETECT 실패: 파일 없음 %r", video)
        return jsonify({"error": f"파일을 찾을 수 없습니다: {video}"}), 400
    with _LOCK:
        try:
            video = os.path.abspath(video)
            wd = fresh_workdir()
            dur = sh.probe_duration(video)
            cands = sh.detect_spikes(video, wd, percentile=sp["percentile"],
                                     min_db=sp["min_db"])
            SESSION.update({"video": video, "duration": dur,
                            "candidates": cands, "vision_used": False,
                            "output": None})
            log.info("DETECT 완료: %.1fs, 후보 %d개 (민감도 %s)",
                     dur, len(cands), sens)
            return jsonify({
                "video": video,
                "duration": round(dur, 1),
                "count": len(cands),
                "candidates": serialize(sh.CONF_AUTO),
                "vision_used": False,
            })
        except Exception as e:
            log.exception("DETECT 예외")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500


@app.route("/api/classify", methods=["POST"])
def api_classify():
    body = request.json or {}
    conf = float(body.get("conf", sh.CONF_AUTO))
    workers = max(1, min(int(body.get("workers", sh.VISION_WORKERS)), 8))
    if not SESSION["candidates"]:
        return jsonify({"error": "먼저 후보를 검출하세요."}), 400
    key = load_api_key()
    if not key:
        return jsonify({"error": "GEMINI_API_KEY 가 없습니다 (.env 또는 환경변수)."}), 400
    CANCEL.clear()
    set_progress("classify", 0, len(SESSION["candidates"]))
    with _LOCK:
        try:
            log.info("CLASSIFY 시작: 후보 %d개, workers=%d",
                     len(SESSION["candidates"]), workers)
            from google import genai
            client = genai.Client(api_key=key)
            wd = Path(SESSION["workdir"])
            usage = sh.classify_all_parallel(
                SESSION["candidates"], SESSION["video"], wd, client, conf,
                workers, should_cancel=CANCEL.is_set,
                on_progress=lambda d, t: set_progress("classify", d, t))
            SESSION["vision_used"] = True
            cost = (usage["in"] / 1e6 * sh.PRICE_IN_PER_M +
                    usage["out"] / 1e6 * sh.PRICE_OUT_PER_M)
            usage["cost_usd"] = round(cost, 4)
            SESSION["usage"] = usage
            # 재선택/재현용으로 자동 저장
            sh.save_results(os.path.join(os.path.dirname(__file__), "results.json"),
                            SESSION["video"], SESSION["duration"],
                            SESSION["candidates"], True)
            n_hl = sum(1 for c in SESSION["candidates"] if c.get("highlight"))
            n_failed = sum(1 for c in SESSION["candidates"]
                           if str(c.get("reason", "")).startswith(("api_error",
                                                                    "frame_error")))
            log.info("CLASSIFY %s: 판별 %d/%d, 하이라이트 %d개, 실패 %d개, "
                     "토큰 in=%d out=%d ≈ $%.4f",
                     "취소됨" if usage.get("cancelled") else "완료",
                     usage["classified"], usage["total"], n_hl, n_failed,
                     usage["in"], usage["out"], cost)
            return jsonify({"candidates": serialize(conf), "vision_used": True,
                            "n_failed": n_failed, "usage": usage})
        except Exception as e:
            log.exception("CLASSIFY 예외")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500
        finally:
            clear_progress()


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """진행 중인 비전 판별을 중단 요청 (이미 시작된 호출은 마무리됨)."""
    CANCEL.set()
    log.info("CANCEL 요청 수신")
    return jsonify({"ok": True})


@app.route("/api/progress")
def api_progress():
    """현재 진행 상태 스냅샷 (프로그레스 바 폴링용)."""
    with _PLOCK:
        return jsonify(dict(PROGRESS))


@app.route("/api/select", methods=["POST"])
def api_select():
    """conf 임계만 바꿔 즉시 재분류 (API 재호출 없음)."""
    conf = float((request.json or {}).get("conf", sh.CONF_AUTO))
    if not SESSION["candidates"]:
        return jsonify({"error": "후보가 없습니다."}), 400
    selected, maybe = sh.select_segments(
        SESSION["candidates"], conf, SESSION["vision_used"])
    return jsonify({
        "candidates": serialize(conf),
        "n_auto": len(selected),
        "n_maybe": len(maybe),
    })


@app.route("/api/build", methods=["POST"])
def api_build():
    body = request.json or {}
    conf = float(body.get("conf", sh.CONF_AUTO))
    output = body.get("output", "highlights.mp4").strip() or "highlights.mp4"
    title = (body.get("title") or "").strip()
    quality = body.get("quality", "balanced")
    qk = sh.QUALITY_PRESETS.get(quality, sh.QUALITY_PRESETS["balanced"])
    if not SESSION["candidates"]:
        return jsonify({"error": "후보가 없습니다."}), 400
    selected, _ = sh.select_segments(
        SESSION["candidates"], conf, SESSION["vision_used"])
    if not selected:
        return jsonify({"error": "채택된 구간이 없습니다. 임계를 낮춰보세요."}), 400
    set_progress("build", 0, len(selected))
    with _LOCK:
        try:
            out_path = output if os.path.isabs(output) else \
                os.path.join(os.path.dirname(__file__), output)
            selected = sorted(selected, key=lambda x: x["peak"])
            log.info("BUILD 시작: %d개 구간 -> %s (타이틀=%r, 품질=%s)",
                     len(selected), out_path, title, quality)
            sh.build_output(SESSION["video"], selected, out_path,
                            Path(SESSION["workdir"]), title=title,
                            preset=qk.get("preset"), crf=qk.get("crf"),
                            copy_mode=qk.get("copy", False),
                            on_progress=lambda d, t: set_progress("build", d, t))
            SESSION["output"] = out_path
            log.info("BUILD 완료: %s", out_path)
            return jsonify({"output": out_path, "n": len(selected)})
        except Exception as e:
            log.exception("BUILD 예외")
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500
        finally:
            clear_progress()


@app.route("/api/preview")
def api_preview():
    """생성된 결과 영상을 브라우저에서 재생용으로 전달."""
    out = SESSION.get("output")
    if not out or not os.path.exists(out):
        return "no output", 404
    return send_file(out, mimetype="video/mp4")


if __name__ == "__main__":
    url = "http://127.0.0.1:5000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  ▶ 하이라이트 추출기 UI: {url}\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
