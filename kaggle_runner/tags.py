"""태깅 규칙 — 주간 영상에 들어갈 클립과 월간 대상 클립 (MONTHLY_PLAN.md 3-2).

**주간 영상** = 그 경기의 클립 전부 − "잘못 잡힘"(`removed`). 팀·골대·실점 태그는 주간 내용에 쓰지 않는다
(상대 득점 장면이 주간 영상에서 빠지면 안 된다 — 사용자 결정 2026-11-26).

**월간 대상** = 아래를 만족하고 `removed`가 아닌 클립:
  청백전            → 모든 클립
  다른 팀 경기      → 공격(attack)이면 대상, 수비(defense)이면 실점(conceded)이 아닐 때만 대상,
                     판단 불가(side=None)이면 제외
  태깅 안 된 경기   → 월간 대상 없음(종류를 모르므로)

클립이 공격/수비인지(`side_of`):
  사람이 그 클립에 직접 누른 값(`side_override`)이 있으면 그것. 없으면 골대 쪽 판별(L1)의 `end`와 경기의
  `attack_end`를 비교한다. "여기부터 반대"(`switch_at_idx`) 이후 클립은 `attack_end`를 뒤집어 비교한다.

이 규칙은 Apps Script(Code.gs의 sideOf_·monthlyEligible_)에도 똑같이 있다. 두 곳이 어긋나지 않게
`tests/data/tag_rules.json` 하나를 양쪽 테스트가 읽는다.
"""

from __future__ import annotations

from kaggle_runner.naming import KIND_OPPONENT, KIND_SCRIMMAGE, SCRIMMAGE_LABEL

ATTACK = "attack"
DEFENSE = "defense"
# 골대 쪽 값(홈 구장의 두 골대 배경: 노란 학교 건물·유리 외벽 / 소나무·초록 철망·아파트 — MONTHLY_PLAN.md M4)
END_BUILDING = "building"
END_TREE = "tree"
FLIP_END = {END_BUILDING: END_TREE, END_TREE: END_BUILDING}


def clip_tag(game: dict, idx: int) -> dict:
    """사람이 그 클립에 누른 태그(side_override·conceded·removed). 없으면 빈 dict."""
    return (game.get("clip_tags") or {}).get(str(idx)) or {}


def is_removed(game: dict, idx: int) -> bool:
    return bool(clip_tag(game, idx).get("removed"))


def side_of(clip: dict, game: dict) -> str | None:
    """클립이 한울타리 공격(attack)인지 수비(defense)인지. 알 수 없으면 None."""
    idx = clip["idx"]
    override = clip_tag(game, idx).get("side_override")
    if override in (ATTACK, DEFENSE):
        return override
    end, attack_end = clip.get("end"), game.get("attack_end")
    if end not in FLIP_END or attack_end not in FLIP_END:
        return None
    switch = game.get("switch_at_idx")
    if switch is not None and idx >= switch:         # "여기부터 반대"
        attack_end = FLIP_END[attack_end]
    return ATTACK if end == attack_end else DEFENSE


def is_conceded(clip: dict, game: dict) -> bool:
    """실점 표시. 수비 클립에서만 뜻이 있다(공격·판단 불가 클립에 남은 표시는 무시)."""
    return bool(clip_tag(game, clip["idx"]).get("conceded")) and side_of(clip, game) == DEFENSE


def is_scrimmage(game: dict) -> bool:
    return game.get("kind") in (KIND_SCRIMMAGE, SCRIMMAGE_LABEL)


def weekly_clips(clips: list[dict], game: dict) -> list[dict]:
    """주간 영상에 올릴 클립 — 전부에서 "잘못 잡힘"만 뺀다(원래 순서)."""
    return [c for c in clips if not is_removed(game, c["idx"])]


def monthly_eligible(clips: list[dict], game: dict) -> list[dict]:
    """월간 영상 대상 클립(원래 순서). 태깅 안 된 경기는 비어 있다."""
    out = []
    for clip in weekly_clips(clips, game):
        if is_scrimmage(game):
            out.append(clip)
        elif game.get("kind") == KIND_OPPONENT:
            side = side_of(clip, game)
            if side == ATTACK or (side == DEFENSE and not is_conceded(clip, game)):
                out.append(clip)
    return out
