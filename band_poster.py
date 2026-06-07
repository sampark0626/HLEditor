#!/usr/bin/env python3
"""
band_poster.py — BAND 게시글 작성 모듈

필요 패키지:
  pip install requests
"""

import json
import os
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests  # type: ignore

# ─── 경로 및 상수 ─────────────────────────────────────────────────────────────
_BASE      = Path(__file__).parent
TOKEN_FILE = _BASE / "band_token.json"

BAND_AUTH_URL  = "https://auth.band.us/oauth2/authorize"
BAND_TOKEN_URL = "https://auth.band.us/oauth2/token"
BAND_API_BASE  = "https://openapi.band.us"


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


# ─── 공개 API ─────────────────────────────────────────────────────────────────
def has_credentials() -> bool:
    """BAND_CLIENT_ID / BAND_CLIENT_SECRET가 .env에 있는지 확인."""
    return bool(_get_env("BAND_CLIENT_ID")) and bool(_get_env("BAND_CLIENT_SECRET"))


def get_auth_url(redirect_uri: str) -> str:
    """BAND OAuth 인증 URL 반환."""
    client_id = _get_env("BAND_CLIENT_ID")
    if not client_id:
        raise ValueError("BAND_CLIENT_ID가 .env에 없습니다.")
    params = {
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
    }
    return f"{BAND_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> None:
    """인증 코드 → 토큰 교환 후 band_token.json 저장."""
    client_id     = _get_env("BAND_CLIENT_ID")
    client_secret = _get_env("BAND_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("BAND_CLIENT_ID / BAND_CLIENT_SECRET가 .env에 없습니다.")
    resp = requests.post(
        BAND_TOKEN_URL,
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  redirect_uri,
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"BAND 토큰 오류: {data}")
    TOKEN_FILE.write_text(json.dumps({
        "access_token": data["access_token"],
        "expires_at":   time.time() + int(data.get("expires_in", 3600 * 24 * 30)),
    }), encoding="utf-8")


def is_authenticated() -> bool:
    """현재 유효한 BAND 인증 토큰이 있는지 확인."""
    if not TOKEN_FILE.exists():
        return False
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        expires_at = data.get("expires_at", 0)
        # 1분 여유
        if expires_at and time.time() > float(expires_at) - 60:
            return False
        return bool(data.get("access_token"))
    except Exception:
        return False


def _get_token() -> str:
    if not TOKEN_FILE.exists():
        raise RuntimeError("BAND 인증이 필요합니다. /auth/band 로 인증하세요.")
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    expires_at = data.get("expires_at", 0)
    if expires_at and time.time() > float(expires_at) - 60:
        raise RuntimeError("BAND 토큰이 만료되었습니다. /auth/band 로 재인증하세요.")
    return data["access_token"]


def revoke() -> None:
    """토큰 파일 삭제 (로그아웃)."""
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


# ─── BAND API 호출 ────────────────────────────────────────────────────────────
def get_bands() -> list:
    """가입한 밴드 목록 반환. [{band_key, name, member_count}]"""
    token = _get_token()
    resp = requests.get(
        f"{BAND_API_BASE}/v2/bands",
        params={"access_token": token},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json().get("result_data", {})
    bands = result.get("bands", [])
    return [
        {
            "band_key":     b["band_key"],
            "name":         b["name"],
            "member_count": b.get("member_count", 0),
        }
        for b in bands
    ]


def write_post(band_key: str, content: str) -> str:
    """
    밴드에 공개 게시글 작성.
    Returns: 게시글 URL (band_key + post_key 기반) 또는 빈 문자열
    """
    token = _get_token()
    resp = requests.post(
        f"{BAND_API_BASE}/v2.1/band/post/create",
        params={
            "access_token": token,
            "band_key":     band_key,
            "content":      content,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    result_code = data.get("result_code", -1)
    if result_code != 1:
        raise RuntimeError(f"BAND API 오류 (result_code={result_code}): {data}")

    result_data = data.get("result_data", {})
    post_key = result_data.get("post_key", "")
    return f"https://band.us/band/{band_key}/post/{post_key}" if post_key else ""


# ─── 콘텐츠 포맷 ──────────────────────────────────────────────────────────────
def format_post_content(
    video_links: list,
    match_date: Optional[str] = None,
) -> str:
    """
    YouTube 링크 목록 → BAND 게시글 텍스트 포맷.
    video_links: [(video_name, youtube_url), ...]
    match_date: "2026-06-07" 형식 문자열 (없으면 오늘)
    """
    if match_date:
        try:
            dt = datetime.strptime(match_date, "%Y-%m-%d")
            date_str = dt.strftime("%Y년 %m월 %d일")
        except ValueError:
            date_str = match_date
    else:
        date_str = datetime.today().strftime("%Y년 %m월 %d일")

    lines = [f"{date_str} 경기 하이라이트 영상입니다.", ""]
    for name, url in video_links:
        base = name.rsplit(".", 1)[0]
        lines.append(f"· {base}")
        lines.append(url)
        lines.append("")
    lines.append("HLEditor 자동 생성")
    return "\n".join(lines)


def group_by_date(jobs: list) -> dict:
    """
    yt_url이 있는 잡들을 match_date 기준으로 그룹핑.
    jobs: [{video_name, yt_url, match_date, ...}, ...]
    Returns: {"2026-06-07": [(video_name, yt_url), ...]}
    """
    today_str = date.today().isoformat()
    groups: dict = defaultdict(list)
    for j in jobs:
        if j.get("yt_url"):
            d = j.get("match_date") or today_str
            groups[d].append((j["video_name"], j["yt_url"]))
    return dict(groups)
