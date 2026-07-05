#!/usr/bin/env python3
"""
app.py — 축구 하이라이트 추출기 배치 웹 UI (진입점)

실제 라우트/잡 처리 로직은 다음 파일로 분리되어 있다:
  jobs.py         — 배치 잡 상태 저장소·백그라운드 워커·처리 파이프라인
  routes_jobs.py  — 큐 관리·리뷰·영상 생성 API (Blueprint)
  routes_auth.py  — YouTube OAuth·업로드 API (Blueprint)
  routes_band.py  — BAND OAuth·게시 API (Blueprint)
  tray.py         — Windows 시스템 트레이 아이콘
  config.py       — .env 파싱·공용 설정 헬퍼

이 파일은 Flask 앱 조립, 메인 페이지 라우트, 서버 시작만 담당한다.
"""

import logging
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import config
import jobs
import routes_auth
import routes_band
import soccer_highlights as sh
from routes_auth import bp_auth
from routes_band import bp_band
from routes_jobs import bp_jobs

app = Flask(__name__)
app.register_blueprint(bp_jobs)
app.register_blueprint(bp_auth)
app.register_blueprint(bp_band)

config.ensure_data_dirs()

LOG_PATH = Path(__file__).parent / "app.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(str(LOG_PATH), maxBytes=2 * 1024 * 1024,
                            backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("hl")


@app.route("/")
def index():
    yt_auth = routes_auth.HAS_YT and routes_auth.yt_up.is_authenticated()
    yt_has_secrets = routes_auth.HAS_YT and routes_auth.yt_up.has_client_secrets()
    band_auth = routes_band.HAS_BAND and routes_band.bp.is_authenticated()
    band_has_creds = routes_band.HAS_BAND and routes_band.bp.has_credentials()
    return render_template("index.html",
                           conf_auto=sh.CONF_AUTO,
                           conf_maybe=sh.CONF_MAYBE,
                           workers=sh.VISION_WORKERS,
                           has_key=bool(config.load_gemini_api_key()),
                           ffmpeg_ok=config.ffmpeg_available(),
                           default_title=config.get_default_title(),
                           sensitivities=sh.SENSITIVITY_PRESETS,
                           qualities=sh.QUALITY_PRESETS,
                           yt_auth=yt_auth,
                           yt_has_secrets=yt_has_secrets,
                           band_auth=band_auth,
                           band_has_creds=band_has_creds)


@app.route("/api/logs")
def api_logs():
    """서버 로그(app.log) 최근 N줄. 문제 발생 시 UI에서 원인 파악용."""
    tail = max(1, min(int(request.args.get("tail", 100)), 1000))
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    return jsonify({"lines": lines[-tail:]})


def _check_ffmpeg():
    """ffmpeg/ffprobe 설치 여부를 확인하고 없으면 경고."""
    missing = config.missing_ffmpeg_tools()
    if missing:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  [경고] {', '.join(missing)} 를 찾을 수 없습니다!")
        print("  영상 처리를 하려면 ffmpeg 설치가 필요합니다.")
        print("")
        print("  Windows 설치 방법 (관리자 PowerShell):")
        print("    winget install Gyan.FFmpeg")
        print("  또는: https://ffmpeg.org/download.html")
        print(f"{sep}\n")
    return len(missing) == 0


def _cleanup_stale_temp_dirs():
    """이전 실행에서 남은 hl_* 임시 작업폴더를 시작 시 정리한다.

    프로세스가 막 시작된 시점엔 JOBS가 항상 비어 있으므로(잡 상태는 재시작 시
    영속화되지 않음), 이 시점에 발견되는 hl_* 폴더는 전부 이전 세션의 잔여물이다
    (예: 강제 종료·크래시로 정리 코드가 실행되지 못한 경우).
    """
    import shutil
    import tempfile
    tmp_root = Path(tempfile.gettempdir())
    removed = 0
    for d in tmp_root.glob("hl_*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        log.info("시작 시 이전 임시 작업폴더 %d개 정리", removed)


if __name__ == "__main__":
    _check_ffmpeg()
    _cleanup_stale_temp_dirs()
    jobs.restore_recent_results()
    jobs.start_worker()
    port = config.get_port()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  ▶ 하이라이트 추출기 UI: {url}\n")

    try:
        import pystray  # noqa: F401 — availability check
        from PIL import Image  # noqa: F401

        import tray

        # Flask를 백그라운드 스레드로 실행, pystray를 메인 스레드에서 실행 (Windows 요구사항)
        flask_t = threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
            daemon=True,
            name="hl-flask",
        )
        flask_t.start()
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        print("  트레이 아이콘이 실행 중입니다. 작업 표시줄에서 HL 아이콘을 찾으세요.\n")
        tray.run(url)
    except ImportError:
        # pystray / Pillow 미설치 시 일반 모드로 실행
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        app.run(host="127.0.0.1", port=port, debug=False)
