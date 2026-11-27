"""클립 보관 규칙과 정리 — 06_clips에 쌓이는 클립을 언제 휴지통으로 보내나 (MONTHLY_PLAN.md 3-8).

  · 그 경기가 속한 달의 월간 영상이 `done`이 된 뒤 7일(`CLIP_KEEP_AFTER_DONE_DAYS`)이 지나면 휴지통.
  · 월간이 안 끝나도 경기일로부터 75일(`CLIP_MAX_DAYS`)이면 휴지통.
  · 월간 영상(07_monthly)과 `_state` JSON은 지우지 않는다. 휴지통으로 보내기만 하고 영구 삭제는 하지 않는다.

계획서에 없는 안전장치 두 가지:
  · 클립은 만들어진 지 7일(`CLIP_MIN_KEEP_DAYS`)은 지우지 않는다 — 월간이 끝난 뒤에 늦게 올린 지난달 경기가
    올리자마자 정리되는 걸 막는다.
  · 아직 게시 전(pending·publish·running)인 경기의 클립은 지우지 않는다 — 게시할 원본이 그것뿐이다.

만료 시각 규칙(`clip_expires_at`)은 Apps Script(Code.gs의 clipExpiresAt_ — 7일 전 재알림)와 같아야 한다.
`tests/data/clip_rules.json` 하나를 pytest와 code_test.js가 같이 읽어 고정한다. 그래서 보관 기간은 **상수로 고정**하고
러너 비밀값(hl_secrets.json)으로 바꿀 수 없게 했다 — Apps Script는 Kaggle 비밀값을 못 읽어 같은 날짜를 계산할 수 없다.
기간을 바꾸려면 이 상수와 Code.gs의 CLIP_KEEP_AFTER_DONE_DAYS·CLIP_MAX_DAYS, clip_rules.json을 함께 고친다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from kaggle_runner.common import (
    KST,
    clips_file,
    from_iso,
    games_file,
    is_auth_error,
    iso,
    monthly_file,
)

CLIP_KEEP_AFTER_DONE_DAYS = 7     # 월간이 done이 된 뒤 이만큼 지나면 정리
CLIP_MAX_DAYS = 75                # 월간이 안 끝나도 경기일로부터 이만큼 지나면 정리
CLIP_MIN_KEEP_DAYS = 7            # 만든 지 이 기간이 안 된 클립은 지우지 않는다
UNPUBLISHED_STATES = ("pending", "publish", "running")

# 06_clips의 클립 이름 `{날짜}-{n경기}_c{번호}.mp4`(naming.clip_name) — 이 이름이 아닌 파일은 건드리지 않는다
CLIP_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d+경기)_c(\d+)\.mp4$")


def clip_expires_at(game_date: str, monthly_done_at: datetime | None, *,
                    keep_after_done_days: int = CLIP_KEEP_AFTER_DONE_DAYS,
                    max_days: int = CLIP_MAX_DAYS) -> datetime:
    """경기 클립이 정리되는 시각. 상한은 경기일(KST 자정) + max_days, 월간이 끝났으면 그 뒤 keep일과 중 이른 쪽."""
    cap = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=KST) + timedelta(days=max_days)
    if monthly_done_at is None:
        return cap
    return min(cap, monthly_done_at + timedelta(days=keep_after_done_days))


def monthly_done_at(doc: dict | None) -> datetime | None:
    """월간이 done이면 끝난 시각. done이 아니거나 시각을 못 읽으면 None(= 상한 규칙만)."""
    if not doc or doc.get("status") != "done":
        return None
    return from_iso(doc.get("done_at"))


def cleanup_clips(runner) -> int:
    """만료된 클립을 06_clips에서 휴지통으로 보내고 clips_*.json에 expired를 적는다. 보낸 파일 수를 돌려준다.

    한 경기에서 실패해도 다른 경기는 계속한다(인증 오류만 위로). 이름이 규칙에 맞지 않는 파일은 건드리지 않는다.
    """
    r = runner
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for remote in r.store.list("clips"):
        m = CLIP_NAME_RE.match(remote.name)
        if m:
            groups[(m.group(1), m.group(2))].append(remote)
    now = r.now()
    monthly_docs: dict[str, dict | None] = {}
    trashed = 0
    for (date, label), files in sorted(groups.items()):
        try:
            month = date[:7]
            if month not in monthly_docs:
                monthly_docs[month] = r.store.read_json(monthly_file(month))
            expires = clip_expires_at(date, monthly_done_at(monthly_docs[month]))
            if now < expires:
                continue
            game = r.store.read_json(games_file(date, label)) or {}
            if game.get("status") in UNPUBLISHED_STATES:
                continue                                    # 아직 게시 전 — 이 클립이 유일한 재료
            clips_doc = r.store.read_json(clips_file(date, label))
            created = from_iso((clips_doc or {}).get("created_at"))
            if created and now < created + timedelta(days=CLIP_MIN_KEEP_DAYS):
                continue
            for remote in files:
                r.store.trash(remote, "clips")
                trashed += 1
            if clips_doc is not None:
                clips_doc["expired"] = True
                clips_doc["expired_at"] = iso(now)
                r.store.write_json(clips_file(date, label), clips_doc)
            r.log(f"[{date} {label}] 보관 기간이 지나 클립 {len(files)}개를 휴지통으로: 만료 {expires:%Y-%m-%d}")
        except Exception as e:
            if is_auth_error(e):
                raise
            r.log(f"[{date} {label}] 클립 정리 실패(다음 실행에서 다시): {e!r}")
    return trashed
