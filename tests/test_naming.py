"""kaggle_runner.naming — 경기 이름 규칙(MONTHLY_PLAN.md 3-3)."""

import pytest

from kaggle_runner import naming


def test_game_name_untagged_is_date_and_game_number():
    assert naming.game_name("2026-12-06", "1경기") == "2026-12-06-1경기"        # 태깅 전(Phase 1)
    assert naming.game_name("2026-12-06", "1경기", None, "") == "2026-12-06-1경기"
    assert naming.game_name("2026-12-06", "1경기", None, "   ") == "2026-12-06-1경기"


def test_game_name_with_opponent_and_scrimmage():
    assert naming.game_name("2026-12-06", "2경기", naming.KIND_OPPONENT, "OO FC") == "2026-12-06-2경기-OO FC"
    assert naming.game_name("2026-12-06", "2경기", None, " OO FC ") == "2026-12-06-2경기-OO FC"
    assert naming.game_name("2026-12-06", "2경기", naming.KIND_SCRIMMAGE) == "2026-12-06-2경기-청백전"
    assert naming.game_name("2026-12-06", "2경기", "청백전") == "2026-12-06-2경기-청백전"
    # 청백전이면 상대 이름이 남아 있어도 쓰지 않는다
    assert naming.game_name("2026-12-06", "2경기", naming.KIND_SCRIMMAGE, "OO FC") == "2026-12-06-2경기-청백전"
    # 다른 팀인데 이름을 안 적었으면 태깅 전과 같다
    assert naming.game_name("2026-12-06", "2경기", naming.KIND_OPPONENT, "") == "2026-12-06-2경기"


def test_youtube_title_keeps_the_opponent_as_typed_but_stays_within_100_chars():
    assert naming.game_name("2026-12-06", "1경기", None, 'A/B:C?') == "2026-12-06-1경기-A/B:C?"
    long = naming.game_name("2026-12-06", "1경기", None, "가" * 200)
    assert len(long) == naming.TITLE_MAX == 100 and long.startswith("2026-12-06-1경기-가")


def test_game_name_for_file_sanitizes_and_shortens_only_the_opponent():
    name = naming.game_name("2026-12-06", "1경기", None, 'A/B:C*D?"E<F>G|H\\I', for_file=True)
    assert name == "2026-12-06-1경기-A_B_C_D__E_F_G_H_I"
    name = naming.game_name("2026-12-06", "1경기", None, "나" * 50, for_file=True)
    assert name == "2026-12-06-1경기-" + "나" * 30
    assert naming.game_name("2026-12-06", "1경기", for_file=True) == "2026-12-06-1경기"   # 상대 없으면 같다
    assert naming.game_name("2026-12-06", "1경기", naming.KIND_SCRIMMAGE, for_file=True) == "2026-12-06-1경기-청백전"


def test_safe_filename():
    assert naming.safe_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert naming.safe_filename("  OO FC  ") == "OO FC"                          # 앞뒤 공백 제거, 안쪽은 유지
    assert naming.safe_filename("가" * 31) == "가" * 30                           # 30자
    assert naming.safe_filename("x" * 10 + " " + "y" * 30) == "x" * 10 + " " + "y" * 19   # 자른 뒤 끝 공백은 없다
    assert naming.safe_filename("가나다", max_len=2) == "가나"
    assert naming.safe_filename("a\nb\tc\x00d") == "a_b_c_d"                      # 제어 문자
    assert naming.safe_filename(None) == "" and naming.safe_filename("   ") == ""
    assert naming.safe_filename("한울타리 FC (2군)") == "한울타리 FC (2군)"         # 괄호·한글은 그대로


def test_safe_filename_cut_does_not_leave_a_trailing_space():
    assert naming.safe_filename("a" * 29 + " b") == "a" * 29


@pytest.mark.parametrize("k, n, want", [
    (1, 1, "2026-12-06-1경기_원본.mp4"),          # 한 파일
    (1, 2, "2026-12-06-1경기_원본1.mp4"),         # 나뉜 파일은 1부터 번호
    (2, 2, "2026-12-06-1경기_원본2.mp4"),
    (3, 3, "2026-12-06-1경기_원본3.mp4"),
])
def test_original_name(k, n, want):
    assert naming.original_name("2026-12-06-1경기", k, n) == want


def test_original_name_keeps_the_original_extension():
    assert naming.original_name("2026-12-06-1경기", 1, 1, ".MOV") == "2026-12-06-1경기_원본.mov"
    assert naming.original_name("2026-12-06-1경기", 1, 1, "mp4") == "2026-12-06-1경기_원본.mp4"
    assert naming.original_name("2026-12-06-1경기", 1, 1, "") == "2026-12-06-1경기_원본.mp4"
    assert naming.original_name("2026-12-06-1경기") == "2026-12-06-1경기_원본.mp4"


def test_clip_name_has_no_opponent_and_pads_to_three_digits():
    assert naming.clip_name("2026-12-06", "1경기", 1) == "2026-12-06-1경기_c001.mp4"
    assert naming.clip_name("2026-12-06", "12경기", 42) == "2026-12-06-12경기_c042.mp4"
    assert naming.clip_name("2026-12-06", "1경기", 1000) == "2026-12-06-1경기_c1000.mp4"


def test_output_file_names():
    assert naming.highlight_name("2026-12-06-1경기-OO FC") == "2026-12-06-1경기-OO FC.mp4"
    assert naming.twod_name("2026-12-06", "1경기") == "2026-12-06-1경기-2D.mp4"
