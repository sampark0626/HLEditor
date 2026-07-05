"""youtube_uploader.generate_description()의 챕터 타임스탬프 생성 로직 테스트."""

import youtube_uploader as yt_up


def test_no_goals_shows_placeholder():
    candidates = [
        {"peak": 5.0, "type": "shot", "reason": "슛"},
    ]
    desc = yt_up.generate_description(candidates, approved=[0], video_name="match.mp4")
    assert "(득점 장면 없음)" in desc


def test_goal_timestamps_are_cumulative_not_original():
    # 승인된 각 구간은 pre_sec+post_sec 길이로 하이라이트 영상에 이어붙여지므로,
    # 원본 peak 시각이 아니라 하이라이트 영상 내 누적 위치가 타임스탬프로 나와야 한다.
    candidates = [
        {"peak": 100.0, "type": "goal", "reason": "선제골"},
        {"peak": 500.0, "type": "shot", "reason": "슛"},   # 승인 안 됨
        {"peak": 900.0, "type": "goal", "reason": "쐐기골"},
    ]
    desc = yt_up.generate_description(
        candidates, approved=[0, 2], video_name="match.mp4", pre_sec=8.0, post_sec=5.0)
    lines = desc.splitlines()
    # 첫 구간: 누적 0:00, 두 번째 승인 구간(idx=2, shot 제외하고 goal만 순회하지만
    # cumulative는 approved 순서대로 매 구간마다 seg_dur(13초)씩 누적된다.
    assert "0:00 선제골" in "\n".join(lines)
    assert "(원본 1:40)" in "\n".join(lines)  # peak=100s = 1:40


def test_only_goal_type_becomes_chapter():
    candidates = [
        {"peak": 10.0, "type": "save", "reason": "선방"},
        {"peak": 20.0, "type": "goal", "reason": "득점"},
    ]
    desc = yt_up.generate_description(candidates, approved=[0, 1], video_name="match.mp4")
    assert "선방" not in desc
    assert "득점" in desc


def test_reason_fallback_when_missing():
    candidates = [{"peak": 10.0, "type": "goal", "reason": ""}]
    desc = yt_up.generate_description(candidates, approved=[0], video_name="match.mp4")
    assert "득점 #1" in desc


def test_footer_and_title_present():
    desc = yt_up.generate_description([], approved=[], video_name="2026-07-05_경기.mp4")
    assert desc.startswith("축구 하이라이트 — 2026-07-05_경기")
    assert "HLEditor" in desc
