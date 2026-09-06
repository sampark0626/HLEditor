#!/usr/bin/env python3
"""
config.py — .env 파싱 및 공용 설정 헬퍼.

app.py / youtube_uploader.py / band_poster.py에 각각 흩어져 있던 동일한
.env 파일 파싱 로직과 ffmpeg 설치 확인 로직을 한 곳으로 모은다.
"""

import os
import subprocess
from pathlib import Path

_BASE = Path(__file__).parent
ENV_PATH = _BASE / ".env"

DEFAULT_TITLE = "한울타리 FC 경기영상"
DEFAULT_PORT = 5000

# 산출물 디렉터리 — 하이라이트 영상은 output/, 판별 결과 JSON은 results/ 로 분리.
# 예전 버전은 둘 다 프로젝트 루트에 흩어놓았다.
OUTPUT_DIR  = _BASE / "output"
RESULTS_DIR = _BASE / "results"

# dot-play(FM 스타일 2D 버드뷰) 산출물 — mp4 + 좌표 parquet
DOTPLAY_DIR = _BASE / "dotplay_output"

# roboflow 공개 호스팅 모델 기본값 (Universe). .env로 재정의 가능.
_DEFAULT_DOTPLAY_PLAYER_MODEL_ID = "football-players-detection-3zvbc/11"
_DEFAULT_DOTPLAY_FIELD_MODEL_ID = "football-field-detection-f07vi/14"


def ensure_data_dirs() -> None:
    """output/, results/, dotplay_output/ 디렉터리가 없으면 생성한다."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)
    DOTPLAY_DIR.mkdir(exist_ok=True)


def _load_env_file() -> dict:
    """.env 파일을 파싱해 dict로 반환 (주석·빈 줄 무시)."""
    if not ENV_PATH.exists():
        return {}
    result = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def get_env(key: str, default: str = "") -> str:
    """환경변수를 우선 사용하고, 없으면 .env 파일 값을 사용한다."""
    return os.environ.get(key) or _load_env_file().get(key, default)


def load_gemini_api_key() -> str | None:
    return get_env("GEMINI_API_KEY") or None


def load_roboflow_api_key() -> str | None:
    """dot-play 파이프라인의 선수/피치 키포인트 검출 모델(roboflow 호스팅)용 API 키."""
    return get_env("ROBOFLOW_API_KEY") or None


def get_dotplay_player_model_id() -> str:
    return get_env("DOTPLAY_PLAYER_MODEL_ID", _DEFAULT_DOTPLAY_PLAYER_MODEL_ID)


def get_dotplay_field_model_id() -> str:
    return get_env("DOTPLAY_FIELD_MODEL_ID", _DEFAULT_DOTPLAY_FIELD_MODEL_ID)


def get_youtube_privacy() -> str:
    """YOUTUBE_PRIVACY .env 값 (기본: public)."""
    return get_env("YOUTUBE_PRIVACY", "public")


def get_default_title() -> str:
    """영상 워터마크/YouTube 제목 기본값. .env의 DEFAULT_TITLE로 재정의 가능."""
    return get_env("DEFAULT_TITLE", DEFAULT_TITLE)


def get_port() -> int:
    """웹 서버 포트. .env/환경변수의 HL_PORT로 재정의 가능."""
    return int(get_env("HL_PORT", str(DEFAULT_PORT)))


def get_band_target_key() -> str:
    return get_env("BAND_TARGET_KEY", "")


def has_band_credentials() -> bool:
    return bool(get_env("BAND_CLIENT_ID")) and bool(get_env("BAND_CLIENT_SECRET"))


def missing_ffmpeg_tools() -> list:
    """PATH에 없는 ffmpeg 관련 실행파일 이름 목록 (캐시 없이 매번 체크)."""
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([tool, "-version"], capture_output=True, check=False)
        except FileNotFoundError:
            missing.append(tool)
    return missing


def ffmpeg_available() -> bool:
    """ffmpeg + ffprobe 가 PATH에 있는지 확인."""
    return not missing_ffmpeg_tools()
