#!/usr/bin/env python3
"""
youtube_uploader.py — 축구 하이라이트 YouTube 업로드 모듈

필요 패키지:
  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
"""

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import config

log = logging.getLogger("hl")

# ─── 경로 및 상수 ─────────────────────────────────────────────────────────────
_BASE = Path(__file__).parent
TOKEN_FILE = _BASE / "youtube_token.json"
_DEFAULT_SECRETS = _BASE / "client_secrets.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",          # 썸네일 업로드 포함
]

# 액세스 토큰 만료 이 시간 전이면 미리 갱신한다 (업로드 도중 만료 방지)
_REFRESH_MARGIN_SEC = 300

# 토큰이 실제로 무효화됐음을 뜻하는 OAuth 오류 코드.
# 이 경우에만 토큰을 폐기한다 — 네트워크 순단이나 구글 5xx까지 폐기 대상으로 삼으면
# 일시적 장애 한 번에 refresh token이 지워져 매번 재인증해야 한다.
_HARD_AUTH_HINTS = (
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "invalid_token",
    "token has been expired or revoked",
)


# ─── 내부 헬퍼 ────────────────────────────────────────────────────────────────
def _get_secrets_path() -> str | None:
    env_path = config.get_env("YOUTUBE_CLIENT_SECRETS")
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
    expiry = getattr(creds, "expiry", None)
    TOKEN_FILE.write_text(json.dumps({
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes or SCOPES),
        # expiry를 저장해야 다음 로드 때 creds.expired가 제대로 계산된다.
        # 저장하지 않으면 만료 여부를 알 수 없어(expiry=None → expired=False)
        # 이미 죽은 액세스 토큰으로 업로드를 시작하게 된다.
        "expiry":        expiry.isoformat() if isinstance(expiry, datetime) else None,
    }), encoding="utf-8")


def _parse_expiry(raw):
    """저장된 expiry 문자열 → naive UTC datetime (google-auth가 쓰는 형식)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _is_hard_auth_error(exc) -> bool:
    """토큰 폐기가 필요한 인증 오류인지 판별한다.

    네트워크 순단·DNS 실패·구글 5xx·할당량 초과(403)는 토큰과 무관하므로 False.
    """
    try:
        from googleapiclient.errors import HttpError  # type: ignore
        resp = getattr(exc, "resp", None)
        if isinstance(exc, HttpError) and resp is not None:
            return getattr(resp, "status", None) == 401
    except Exception:
        pass
    try:
        from google.auth.exceptions import RefreshError  # type: ignore
        if isinstance(exc, RefreshError):
            return any(h in str(exc).lower() for h in _HARD_AUTH_HINTS)
    except Exception:
        pass
    return False


def _utcnow_naive() -> datetime:
    """google-auth의 creds.expiry는 naive UTC이므로 비교 대상도 naive로 맞춘다."""
    return datetime.now(UTC).replace(tzinfo=None)


def _needs_refresh(creds) -> bool:
    if getattr(creds, "expired", False):
        return True
    expiry = getattr(creds, "expiry", None)
    if expiry is None:
        # expiry를 모르는 토큰(= 이 필드를 저장하지 않던 예전 버전이 만든 파일).
        # 만료 여부를 판단할 수 없으므로 한 번 갱신해 expiry를 채워 넣는다.
        # 이후로는 저장된 expiry로 정상 판단되므로 매번 갱신하지 않는다.
        return True
    if not isinstance(expiry, datetime):
        return False
    return expiry <= _utcnow_naive() + timedelta(seconds=_REFRESH_MARGIN_SEC)


def _get_credentials():
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
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
        expiry=_parse_expiry(data.get("expiry")),
    )
    if creds.refresh_token and _needs_refresh(creds):
        try:
            creds.refresh(Request())
            _save_token(creds)
        except Exception as e:
            if _is_hard_auth_error(e):
                log.warning("YouTube refresh token 무효 — 토큰 폐기: %s", e)
                revoke()
                return None
            # 일시적 장애: 토큰을 유지하고 기존 액세스 토큰으로 그대로 시도한다.
            log.warning("YouTube 토큰 갱신 일시 실패 (토큰 유지): %s", e)
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


def auth_status() -> dict:
    """인증 상태를 사유와 함께 반환한다.

    reason: "ok" | "no_token" | "unauthorized" | "no_channel" | "network"

    중요: 네트워크 오류(reason="network")일 때는 ok=True로 두고 토큰도 보존한다.
    여기서 미인증으로 단정하면 (1) 토큰이 지워져 재인증을 강요당하고
    (2) 원스톱 파이프라인이 업로드 단계를 조용히 건너뛴다.
    실제로 인증이 죽었는지는 업로드 시도가 판정하게 둔다.
    """
    if not TOKEN_FILE.exists():
        return {"ok": False, "reason": "no_token", "channel": None, "detail": ""}
    try:
        from googleapiclient.discovery import build  # type: ignore
        creds = _get_credentials()
        if not creds:
            return {"ok": False, "reason": "unauthorized", "channel": None,
                    "detail": "refresh token이 만료되었거나 취소되었습니다."}
        yt = build("youtube", "v3", credentials=creds)
        resp = yt.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if not items:
            return {"ok": False, "reason": "no_channel", "channel": None,
                    "detail": "이 계정에 연결된 YouTube 채널이 없습니다."}
        return {
            "ok": True, "reason": "ok", "detail": "",
            "channel": {"id": items[0]["id"], "title": items[0]["snippet"]["title"]},
        }
    except Exception as e:
        if _is_hard_auth_error(e):
            log.warning("YouTube 인증 무효 — 토큰 폐기: %s", e)
            revoke()
            return {"ok": False, "reason": "unauthorized", "channel": None,
                    "detail": str(e)[:200]}
        log.warning("YouTube 인증 확인 중 일시적 오류 (토큰 유지): %s", e)
        return {"ok": True, "reason": "network", "channel": None, "detail": str(e)[:200]}


def is_authenticated() -> bool:
    """현재 유효한 인증 토큰이 있는지 확인하고, 실제 API 호출을 통해 검증한다."""
    return bool(auth_status()["ok"])


def get_channel_info() -> dict | None:
    """인증된 YouTube 채널 정보 반환 (id, title). 실패 시 None (토큰은 건드리지 않음)."""
    return auth_status()["channel"]


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
    - 챕터 표기: type == 'goal' 인 구간만 표시
    - 타임스탬프: 하이라이트 영상 내 누적 위치 기준 (비-goal 구간도 시간은 누적)
    """
    from soccer_highlights import get_merged_timeline
    base_name = video_name.rsplit(".", 1)[0]
    lines = [f"축구 하이라이트 — {base_name}", ""]

    approved_cands = [candidates[idx] for idx in approved if idx < len(candidates)]
    _, cand_timestamps = get_merged_timeline(approved_cands, pre_sec, post_sec)

    goal_lines = []
    goal_count = 0

    for idx in sorted(approved):
        if idx >= len(candidates):
            continue
        c = candidates[idx]
        ctype = (c.get("type") or "").strip().lower()

        if ctype == "goal":
            goal_count += 1
            ts_hl_sec = cand_timestamps.get(id(c), 0.0)
            mins = int(ts_hl_sec // 60)
            secs = int(ts_hl_sec % 60)
            ts_hl = f"{mins}:{secs:02d}"

            peak  = float(c.get("peak", 0))
            orig_min = int(peak // 60)
            orig_sec = int(peak % 60)
            ts_orig  = f"{orig_min}:{orig_sec:02d}"

            reason = (c.get("reason") or "").strip()
            label  = reason if reason else f"득점 #{goal_count}"
            goal_lines.append(f"{ts_hl} {label}  (원본 {ts_orig})")

    if goal_lines:
        lines += goal_lines
    else:
        lines.append("(득점 장면 없음)")

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
    thumbnail_path: str | None = None,
    privacy: str = "public",
    on_progress=None,          # callable(bytes_done, total_bytes) or None
) -> str:
    """
    YouTube에 영상 업로드.
    완료 시 YouTube URL(https://youtu.be/<id>) 반환.
    """
    from googleapiclient.discovery import build  # type: ignore
    from googleapiclient.http import MediaFileUpload  # type: ignore

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

    from google.auth.exceptions import RefreshError  # type: ignore
    response = None
    try:
        while response is None:
            status, response = req.next_chunk()
            if status and on_progress:
                try:
                    on_progress(int(status.resumable_progress), file_size)
                except Exception:
                    pass
    except RefreshError as e:
        # 진짜 만료/취소일 때만 토큰을 폐기한다. 일시적 갱신 실패는 그대로 전파해
        # 재시도(파이프라인 [이어서 진행])로 복구할 수 있게 남겨둔다.
        if _is_hard_auth_error(e):
            revoke()
            raise RuntimeError(
                "YouTube 인증이 만료되었거나 취소되었습니다. 다시 로그인해주세요.") from e
        raise RuntimeError(f"YouTube 토큰 갱신 실패 (일시적 오류일 수 있음): {e}") from e

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
