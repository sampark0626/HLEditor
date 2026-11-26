"""kaggle_runner.tags — 주간/월간 클립 규칙 표(MONTHLY_PLAN.md 3-2).

규칙 표는 tests/data/tag_rules.json 하나이고 Apps Script 쪽(code_test.js)도 같은 파일을 읽는다 —
Python과 JS 구현이 어긋나지 않게 고정하는 테스트다.
"""

import json
from pathlib import Path

import pytest

from kaggle_runner import naming, tags

CASES = json.loads((Path(__file__).parent / "data" / "tag_rules.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES["sideCases"], ids=lambda c: c["name"])
def test_side_of_rule_table(case):
    assert tags.side_of(case["clip"], case["game"]) == case["side"]


@pytest.mark.parametrize("case", CASES["eligibleCases"], ids=lambda c: c["name"])
def test_monthly_eligible_and_weekly_rule_table(case):
    clips, game = case["clips"], case["game"]
    assert [c["idx"] for c in tags.monthly_eligible(clips, game)] == case["eligible"]
    assert [c["idx"] for c in tags.weekly_clips(clips, game)] == case["weekly"]


@pytest.mark.parametrize("case", CASES["nameCases"], ids=lambda c: c["name"])
def test_game_name_rule_table(case):
    args = (case["date"], case["label"], case["kind"], case["opponent"])
    assert naming.game_name(*args) == case["title"]
    assert naming.game_name(*args, for_file=True) == case["file"]


def test_opponent_goals_stay_in_the_weekly_video():
    """주간 영상은 팀·골대·실점으로 아무것도 빼지 않는다 — 상대 득점 장면(수비·실점)도 그대로 남는다."""
    game = {"kind": "opponent", "attack_end": "building",
            "clip_tags": {"2": {"conceded": True}, "3": {"side_override": "defense", "conceded": True}}}
    clips = [{"idx": 1, "end": "building"}, {"idx": 2, "end": "tree"}, {"idx": 3, "end": None}]
    assert [c["idx"] for c in tags.weekly_clips(clips, game)] == [1, 2, 3]        # 실점 클립도 주간에는 있다
    assert [c["idx"] for c in tags.monthly_eligible(clips, game)] == [1]          # 월간에서만 빠진다


def test_helpers_read_string_keyed_tags():
    game = {"clip_tags": {"7": {"removed": True, "conceded": True}}}
    assert tags.is_removed(game, 7) and not tags.is_removed(game, 8)
    assert tags.clip_tag(game, 7) == {"removed": True, "conceded": True} and tags.clip_tag(game, 1) == {}
    assert tags.clip_tag({}, 1) == {} and tags.clip_tag({"clip_tags": None}, 1) == {}
