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

import soccer_highlights as sh

app = Flask(__name__)

# 단일 사용자 로컬 도구이므로 메모리 내 단일 세션 상태로 충분하다.
SESSION = {
    "video": None,       # 입력 영상 절대경로
    "duration": None,    # 초
    "candidates": [],    # 비전 결과까지 포함된 후보 dict 리스트
    "vision_used": False,
    "workdir": None,     # 오디오/프레임/클립용 임시 작업 폴더
    "output": None,      # 마지막 생성 영상 경로
}
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
                           has_key=bool(load_api_key()))


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
    video = (request.json or {}).get("video", "").strip().strip('"')
    if not video or not os.path.exists(video):
        return jsonify({"error": f"파일을 찾을 수 없습니다: {video}"}), 400
    with _LOCK:
        try:
            video = os.path.abspath(video)
            wd = fresh_workdir()
            dur = sh.probe_duration(video)
            cands = sh.detect_spikes(video, wd)
            SESSION.update({"video": video, "duration": dur,
                            "candidates": cands, "vision_used": False,
                            "output": None})
            return jsonify({
                "video": video,
                "duration": round(dur, 1),
                "count": len(cands),
                "candidates": serialize(sh.CONF_AUTO),
                "vision_used": False,
            })
        except Exception as e:
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
    with _LOCK:
        try:
            from google import genai
            client = genai.Client(api_key=key)
            wd = Path(SESSION["workdir"])
            sh.classify_all_parallel(SESSION["candidates"], SESSION["video"],
                                     wd, client, conf, workers)
            SESSION["vision_used"] = True
            # 재선택/재현용으로 자동 저장
            sh.save_results(os.path.join(os.path.dirname(__file__), "results.json"),
                            SESSION["video"], SESSION["duration"],
                            SESSION["candidates"], True)
            return jsonify({"candidates": serialize(conf), "vision_used": True})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500


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
    if not SESSION["candidates"]:
        return jsonify({"error": "후보가 없습니다."}), 400
    selected, _ = sh.select_segments(
        SESSION["candidates"], conf, SESSION["vision_used"])
    if not selected:
        return jsonify({"error": "채택된 구간이 없습니다. 임계를 낮춰보세요."}), 400
    with _LOCK:
        try:
            out_path = output if os.path.isabs(output) else \
                os.path.join(os.path.dirname(__file__), output)
            selected = sorted(selected, key=lambda x: x["peak"])
            sh.build_output(SESSION["video"], selected, out_path,
                            Path(SESSION["workdir"]))
            SESSION["output"] = out_path
            return jsonify({"output": out_path, "n": len(selected)})
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500


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
