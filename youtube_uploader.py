#!/usr/bin/env python3
"""
youtube_uploader.py — 축구 하이라이트 YouTube 업로드 모듈

필요 패키지:
  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# ─── 경로 및 상수 ─────────────────────────────────────────────────────────────
_BASE = Path(__file__).parent
TOKEN_FILE = _BASE / "youtube_token.json"
_DEFAULT_SECRETS = _BASE / "client_secrets.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",          # 썸네일 업로드 포함
]


# ─── 내부 헬퍼 ────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env_path = _BASE / ".env"
    if not env_path.exists():
        return {}
    result = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _load_env().get(key, default)


def _get_secrets_path() -> Optional[str]:
    env_path = _get_env("YOUTUBE_CLIENT_SECRETS")
    if env_path and Path(env_path).exists():
        return env_path
    if _DEFAULT_SECRETS.exists():
        return str(_DEFAULT_SECRETS)
    return None


def _get_flow(redirect_uri: str):
    from google_auth_oauthlib.flow import Flow  # type: ignore
    secrets_path = _get_secrets_path()
    if not secrets_path:
        raise FileNotFoundError(
            "client_secrets.json 파일 없음. "
            "Google Cloud Console > API 및 서비스 > OAuth 2.0 클라이언트 ID에서 다운로드하세요."
        )
    flow = Flow.from_client_secrets_file(
        secrets_path, scopes=SCOPES, redirect_uri=redirect_uri
    )
    return flow


def _save_token(creds) -> None:
    TOKEN_FILE.write_text(json.dumps({
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes or SCOPES),
    }), encoding="utf-8")


def _get_credentials():
    from google.oauth2.credentials import Credentials  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", SCOPES),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
    return creds


# PKCE: state → flow 보존 (get_auth_url ~ exchange_code 사이에 flow 유지)
_PENDING_FLOWS: dict = {}


# ─── 공개 API ─────────────────────────────────────────────────────────────────
def has_client_secrets() -> bool:
    """client_secrets.json 파일이 있는지 확인."""
    return _get_secrets_path() is not None


def get_auth_url(redirect_uri: str) -> str:
    """OAuth 인증 URL 반환 (브라우저에서 열어 인증)."""
    flow = _get_flow(redirect_uri)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    # PKCE code_verifier가 flow 안에 있으므로 state 키로 보존
    _PENDING_FLOWS[state] = flow
    return auth_url


def exchange_code(code: str, redirect_uri: str, state: str = "") -> None:
    """인증 코드 → 토큰 교환 후 youtube_token.json 저장."""
    # 같은 flow 객체를 재사용해야 PKCE code_verifier가 유효함
    flow = _PENDING_FLOWS.pop(state, None) or _get_flow(redirect_uri)
    flow.fetch_token(code=code)
    _save_token(flow.credentials)


def is_authenticated() -> bool:
    """현재 유효한 인증 토큰이 있는지 확인."""
    try:
        creds = _get_credentials()
        return creds is not None and (creds.valid or bool(creds.refresh_token))
    except Exception:
        return False


def get_channel_info() -> Optional[dict]:
    """인증된 YouTube 채널 정보 반환 (id, title)."""
    try:
        from googleapiclient.discovery import build  # type: ignore
        creds = _get_credentials()
        if not creds:
            return None
        yt = build("youtube", "v3", credentials=creds)
        resp = yt.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            return None
        return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}
    except Exception:
        return None


def revoke() -> None:
    """토큰 파일 삭제 (로그아웃)."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


# ─── 썸네일 추출 ──────────────────────────────────────────────────────────────
def extract_thumbnail(video_path: str, timestamp: float, out_path: str) -> bool:
    """
    원본 영상에서 timestamp 위치 프레임을 JPEG로 추출.
    YouTube 썸네일 권장 크기: 1280×720 (16:9).
    """
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(max(0.0, timestamp)),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black",
            "-q:v", "2",
            out_path,
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=30)
        return Path(out_path).exists()
    except Exception:
        return False


# ─── 설명 생성 ────────────────────────────────────────────────────────────────
def generate_description(
    candidates: list,
    approved: list,
    video_name: str,
    pre_sec: float = 8.0,
    post_sec: float = 5.0,
) -> str:
    """
    승인된 후보 구간으로 YouTube 챕터 마커 설명 생성.
    타임스탬프는 하이라이트 영상 내 누적 위치 기준.
    """
    base_name = video_name.rsplit(".", 1)[0]
    seg_dur = pre_sec + post_sec  # 각 구간 길이 (초)

    lines = [f"축구 하이라이트 — {base_name}", ""]

    cumulative = 0.0
    sorted_approved = sorted(approved)
    for rank, idx in enumerate(sorted_approved):
        if idx >= len(candidates):
            continue
        c = candidates[idx]
        peak   = float(c.get("peak", 0))
        reason = (c.get("reason") or "").strip()
        ctype  = c.get("type", "")
        conf   = float(c.get("confidence") or 0)

        mins = int(cumulative // 60)
        secs = int(cumulative % 60)
        ts_hl = f"{mins}:{secs:02d}"

        orig_min = int(peak // 60)
        orig_sec = int(peak % 60)
        ts_orig  = f"{orig_min}:{orig_sec:02d}"

        label = reason if reason else (ctype if ctype else f"하이라이트 #{rank + 1}")
        # YouTube 챕터: 0:00 형식
        lines.append(f"{ts_hl} {label}  (원본 {ts_orig})")
        cumulative += seg_dur

    lines += [
        "",
        "---",
        "이 영상은 HLEditor(https://github.com/sampark0626/HLEditor)로 자동 생성된 하이라이트입니다.",
    ]
    return "\n".join(lines)


# ─── 업로드 ───────────────────────────────────────────────────────────────────
def upload_video(
    video_path: str,
    title: str,
    description: str,
    thumbnail_path: Optional[str] = None,
    privacy: str = "public",
    on_progress=None,          # callable(bytes_done, total_bytes) or None
) -> str:
    """
    YouTube에 영상 업로드.
    완료 시 YouTube URL(https://youtu.be/<id>) 반환.
    """
    from googleapiclient.discovery import build            # type: ignore
    from googleapiclient.http import MediaFileUpload       # type: ignore

    creds = _get_credentials()
    if not creds:
        raise RuntimeError("YouTube 인증이 필요합니다. /auth/youtube 로 인증하세요.")

    yt = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": ["축구", "하이라이트", "soccer", "football", "highlight"],
            "categoryId": "17",   # Sports
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    file_size = Path(video_path).stat().st_size
    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,  # 10 MB
    )

    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = req.next_chunk()
        if status and on_progress:
            try:
                on_progress(int(status.resumable_progress), file_size)
            except Exception:
                pass

    video_id = response["id"]
    yt_url = f"https://youtu.be/{video_id}"

    # 썸네일 업로드 (채널 미인증 시 실패 가능 — 무시)
    if thumbnail_path and Path(thumbnail_path).exists():
        try:
            yt.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype="image/jpeg"),
            ).execute()
        except Exception:
            pass

    return yt_url
