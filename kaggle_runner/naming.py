"""인박스 러너 전용 이름 규칙 — MONTHLY_PLAN.md 3-3.

YouTube 제목·Drive 파일·BAND 글이 같은 경기 이름 `날짜-경기번호-상대팀`을 쓰게 한다.
`youtube_uploader.make_title`은 PC 웹 UI(static/js/build.js `ytTitleFor`)와 규칙을 공유하므로
건드리지 않고, 러너만 이 모듈을 쓴다.

주의: `youtube_uploader.generate_description`과 `band_poster.format_post_content`는 이름의 마지막 `.` 뒤를
확장자로 보고 자른다. 지금(Phase 1)은 상대 이름이 없어 `.`이 없지만, 상대 이름에 `.`이 들어갈 수
있게 되면(Phase 2) 이 두 곳에 넘길 때 확장자를 붙여 넘기거나 그 함수를 고쳐야 한다.
"""

from __future__ import annotations

import re

# `kind` 값 — Phase 2의 태깅(games_*.json)과 같은 값을 쓴다. None이면 아직 태깅 전이다.
KIND_SCRIMMAGE = "scrimmage"      # 한울타리 청백전
KIND_OPPONENT = "opponent"        # 다른 팀과의 경기
SCRIMMAGE_LABEL = "청백전"

FILENAME_MAX = 30                 # 파일 이름에 넣는 상대 이름 길이 제한
TITLE_MAX = 100                   # YouTube 제목 한도

# 파일 이름에 못 쓰는 문자(Windows 기준) + 제어 문자
_BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_filename(text: str | None, max_len: int = FILENAME_MAX) -> str:
    """파일 이름에 못 쓰는 문자(`/\\:*?"<>|`)를 `_`로 바꾸고 앞뒤 공백을 지운 뒤 max_len자로 자른다."""
    cleaned = _BAD_CHARS.sub("_", text or "").strip()
    return cleaned[:max_len].strip()


def game_name(date: str, label: str, kind: str | None = None, opponent: str = "",
              *, for_file: bool = False) -> str:
    """경기 이름: `{날짜}-{n경기}-{상대}` / 청백전 `{날짜}-{n경기}-청백전` / 태깅 전 `{날짜}-{n경기}`.

    YouTube 제목·설명·BAND 글에는 상대 이름을 그대로 쓴다(100자 제한). 파일 이름에 쓸 때는
    for_file=True — 상대 이름의 금지 문자를 `_`로 바꾸고 30자로 자른다.
    """
    base = f"{date}-{label}"
    if kind in (KIND_SCRIMMAGE, SCRIMMAGE_LABEL):
        suffix = SCRIMMAGE_LABEL
    else:
        suffix = (opponent or "").strip()
        if suffix and for_file:
            suffix = safe_filename(suffix)
    return (f"{base}-{suffix}" if suffix else base)[:TITLE_MAX]


def original_name(game: str, k: int = 1, n: int = 1, ext: str = ".mp4") -> str:
    """03_done으로 옮긴 원본의 이름: `{game}_원본{ext}`, 나뉜 파일이면 `{game}_원본{k}{ext}`(k는 1부터, n은 파일 수).

    확장자는 원본 것을 그대로 쓴다(.mov를 .mp4로 바꾸면 거짓이 된다). 비어 있으면 .mp4.
    """
    ext = (ext or ".mp4").lower()
    if not ext.startswith("."):
        ext = "." + ext
    return f"{game}_원본{k if n > 1 else ''}{ext}"


def clip_name(date: str, label: str, idx: int) -> str:
    """06_clips에 보관하는 클립 이름: `{날짜}-{n경기}_c{번호3자리}.mp4`. 상대 이름은 넣지 않는다(태깅 전에 만들어진다)."""
    return f"{date}-{label}_c{idx:03d}.mp4"


def highlight_name(game: str) -> str:
    """05_output에 남기는 하이라이트 파일 이름(YouTube 실패 백업·로컬 시험): `{game}.mp4`."""
    return f"{game}.mp4"


def twod_name(date: str, label: str) -> str:
    """05_output에 올리는 2D 결과 이름: `{날짜}-{n경기}-2D.mp4`."""
    return f"{date}-{label}-2D.mp4"
