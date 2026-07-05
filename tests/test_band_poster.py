"""band_poster.py의 순수 로직(게시글 포맷·날짜별 그룹핑) 테스트."""

import band_poster as bp


class TestFormatPostContent:
    def test_includes_date_and_links(self):
        content = bp.format_post_content(
            [("match1.mp4", "https://youtu.be/aaa"), ("match2.mp4", "https://youtu.be/bbb")],
            match_date="2026-07-05",
        )
        assert "2026년 07월 05일" in content
        assert "match1" in content
        assert "https://youtu.be/aaa" in content
        assert "match2" in content
        assert "https://youtu.be/bbb" in content
        assert "HLEditor" in content

    def test_strips_extension_from_name(self):
        content = bp.format_post_content([("경기영상.mov", "https://youtu.be/xxx")], match_date="2026-07-05")
        assert "경기영상.mov" not in content
        assert "경기영상" in content

    def test_invalid_date_string_used_as_is(self):
        content = bp.format_post_content([], match_date="이상한날짜")
        assert "이상한날짜" in content

    def test_no_match_date_defaults_to_today(self):
        content = bp.format_post_content([], match_date=None)
        assert "경기 하이라이트 영상입니다." in content


class TestGroupByDate:
    def test_groups_jobs_by_match_date(self):
        jobs_list = [
            {"video_name": "a.mp4", "yt_url": "https://youtu.be/a", "match_date": "2026-07-01"},
            {"video_name": "b.mp4", "yt_url": "https://youtu.be/b", "match_date": "2026-07-01"},
            {"video_name": "c.mp4", "yt_url": "https://youtu.be/c", "match_date": "2026-07-02"},
        ]
        groups = bp.group_by_date(jobs_list)
        assert set(groups.keys()) == {"2026-07-01", "2026-07-02"}
        assert groups["2026-07-01"] == [("a.mp4", "https://youtu.be/a"), ("b.mp4", "https://youtu.be/b")]
        assert groups["2026-07-02"] == [("c.mp4", "https://youtu.be/c")]

    def test_jobs_without_yt_url_are_excluded(self):
        jobs_list = [
            {"video_name": "a.mp4", "yt_url": "", "match_date": "2026-07-01"},
            {"video_name": "b.mp4", "yt_url": None, "match_date": "2026-07-01"},
        ]
        assert bp.group_by_date(jobs_list) == {}

    def test_missing_match_date_falls_back_to_today(self):
        jobs_list = [{"video_name": "a.mp4", "yt_url": "https://youtu.be/a"}]
        groups = bp.group_by_date(jobs_list)
        assert len(groups) == 1
        (only_key,) = groups.keys()
        assert len(only_key) == len("YYYY-MM-DD")
