"""러너 여러 모듈이 같이 쓰는 도우미 — 시각, _state 파일 이름, Google 인증 오류 판별.

`inbox_runner`와 `publish`가 서로를 import하면 순환이 되므로(`python -m kaggle_runner.inbox_runner`로 직접
실행하면 모듈이 두 번 올라간다) 공용 부분을 여기에 둔다. `inbox_runner`는 같은 이름으로 다시 내보낸다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")   # 한국은 서머타임이 없어 고정 오프셋으로 충분

AUTH_HELP = ("PC에서 `python tools/make_kaggle_secrets.py`를 다시 실행한 뒤, "
             "Kaggle 데이터셋 hl-secrets에 새 hl_secrets.json을 새 버전으로 올려 주세요.")

REQUESTS_FILE = "requests.json"     # 검토 페이지(Apps Script)가 쓰는 요청 대기열 — 러너는 읽기만 한다


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def kst_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


# ─── _state 파일 이름 ────────────────────────────────────────────────────────
# 내부 키는 `{날짜}_{n경기}` 그대로 — 상대팀은 바뀔 수 있어 이름에 넣지 않는다(MONTHLY_PLAN.md 3-3).
def day_file(date_str: str) -> str:
    return f"day_{date_str}.json"


def clips_file(date_str: str, label: str) -> str:
    return f"clips_{date_str}_{label}.json"


def games_file(date_str: str, label: str) -> str:
    return f"games_{date_str}_{label}.json"


def is_auth_error(exc: BaseException) -> bool:
    """Google 자격 증명이 죽은 경우(refresh token 만료·취소)인지."""
    try:
        from google.auth.exceptions import RefreshError
        if isinstance(exc, RefreshError):
            return True
    except ImportError:
        pass
    return "invalid_grant" in str(exc)
