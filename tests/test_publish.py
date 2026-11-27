"""검토 모드(HL_REVIEW)와 게시 단계 — kaggle_runner.publish + inbox_runner의 pending 처리 (MONTHLY_PLAN.md Phase 2).

분석 단계는 YouTube에 올리지 않고 클립을 06_clips에 남긴 채 경기를 pending으로 두고, 게시 단계는 Apps Script가
requests.json에 넣은 요청(과 games_*.json의 status)을 보고 "잘못 잡힘"만 뺀 전체 클립을 올린다.
Apps Script 쪽은 일부러 가짜로 흉내 낸다 — games_*.json·requests.json을 직접 써서 사용자가 누른 것처럼 만든다.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_inbox_runner import T0, Harness

from kaggle_runner import inbox_runner as ir
from kaggle_runner import publish as pub

D = "2026-09-27"
GAME = "2026-09-27-1경기"
REVIEW_URL = "https://script.google.com/macros/s/AKfy/exec"


class YT:
    """youtube_uploader의 업로드·썸네일·제목 변경을 가짜로 바꾼다."""

    def __init__(self, monkeypatch):
        self.uploads, self.updates, self.thumbs = [], [], []
        self.upload_error = None
        self.update_error = None
        monkeypatch.setattr(ir.yt_up, "upload_video", self.upload)
        monkeypatch.setattr(ir.yt_up, "extract_thumbnail", self.thumb)
        monkeypatch.setattr(ir.yt_up, "update_video_snippet", self.update)

    def upload(self, path, title, description, thumbnail_path=None, privacy="public", on_progress=None):
        if self.upload_error:
            raise RuntimeError(self.upload_error)
        self.uploads.append({"title": title, "description": description, "thumb": thumbnail_path,
                             "privacy": privacy, "bytes": Path(path).read_bytes()})
        return f"https://youtu.be/VID{len(self.uploads)}"

    def thumb(self, video_path, timestamp, out_path):
        self.thumbs.append({"clip": Path(video_path).name, "ts": timestamp})
        Path(out_path).write_bytes(b"jpg")
        return True

    def update(self, video_id, title, first_line=None):
        if self.update_error:
            raise RuntimeError(self.update_error)
        self.updates.append({"id": video_id, "title": title, "first_line": first_line})


@pytest.fixture
def yt(monkeypatch):
    return YT(monkeypatch)


def review_harness(tmp_path, monkeypatch, peaks=(100.0, 400.0), **settings):
    settings.setdefault("hl_review", True)
    settings.setdefault("review_url", REVIEW_URL)
    h = Harness(tmp_path, monkeypatch, **settings)
    monkeypatch.setattr(ir.sh, "detect_spikes", lambda video, wd, **k: [
        {"peak": p, "delta_db": 12.0 - i} for i, p in enumerate(peaks)])
    return h


def analyzed(tmp_path, monkeypatch, **kw):
    """분석 단계를 한 번 돌려 pending 경기 하나(클립 2~3개)를 만든다."""
    h = review_harness(tmp_path, monkeypatch, **kw)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    h.notes.clear()
    return h


def games(h, label="1경기"):
    return h.store.read_json(f"games_{D}_{label}.json")


def set_game(h, label="1경기", **fields):
    game = games(h, label)
    game.update(fields)
    h.store.write_json(f"games_{D}_{label}.json", game)


def request(h, *items):
    h.store.write_json("requests.json", {"items": [dict(id=f"r{i}", at="2026-09-27T06:00:00Z", **it)
                                                   for i, it in enumerate(items)]})


def publish_request(h, label="1경기", **fields):
    """검토 페이지에서 "게시"를 누른 것처럼: 태그를 쓰고 status=publish로 바꾸고 대기열에 넣는다."""
    set_game(h, label, status="publish", tag_source=fields.pop("tag_source", "manual"), **fields)
    request(h, {"type": "publish", "date": D})


# ─── 분석 단계: 검토 모드 ────────────────────────────────────────────────────
def test_review_mode_keeps_the_game_pending_and_does_not_publish(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    h.add("g.mp4", 1500, start_min=0)
    h.run()

    assert h.published == [] and yt.uploads == []                       # YouTube에 올리지 않았다
    game = games(h)
    assert game["status"] == "pending" and game["tag_source"] is None
    assert game["auto_at"] == "2026-09-28T09:00:00+09:00"               # 경기 다음 날 09:00 KST
    assert (game["kind"], game["yt_url"], game["published_clips"]) == (None, None, [])
    assert h.files("clips") == [f"{GAME}_c001.mp4", f"{GAME}_c002.mp4"]
    assert h.store.read_json(f"clips_{D}_1경기.json")["clips"]
    assert h.files("done") == [f"{GAME}_원본.mp4"]
    day = h.store.read_json(f"day_{D}.json")
    assert day["games"][0]["label"] == "1경기" and day["games"][0]["yt_url"] is None
    assert h.files("output") == []


def test_review_mode_sends_a_review_notification_instead_of_the_band_post(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    h.add("g.mp4", 1500, start_min=0)
    h.run()

    assert len(h.notes) == 1
    note = h.notes[0]
    assert note["title"] == "9/27 하이라이트 검토 준비 — 1경기"
    assert f"· {GAME} (클립 2개)" in note["message"]
    assert "9/28 오전 9시까지 검토가 없으면 전체 장면으로 자동 게시합니다." in note["message"]
    assert "경기 하이라이트 영상입니다" not in note["message"]               # BAND 글이 아니다
    assert note["click"] == f"{REVIEW_URL}?page=review&date={D}"            # 누르면 그날 검토 화면
    assert note["tags"] == ["soccer"]


def test_review_notification_carries_the_analysis_warnings_and_problems(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    h.add("p1.mp4", 1800, start_min=0)
    h.add("p2.mp4", 600, start_min=30)                                     # 나뉜 파일 → 합쳤다는 경고
    h.add("bad.mp4", 100, start_min=120)                                   # 너무 짧아 실패
    h.run()
    assert len(h.notes) == 1
    msg = h.notes[0]["message"]
    assert "※ 1경기: 나뉜 파일 2개를 한 경기로 합쳤습니다" in msg
    assert "※ 처리 실패 — bad.mp4" in msg
    assert h.notes[0]["tags"] == ["soccer", "warning"]


def test_review_notification_without_review_url_has_no_click(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch, review_url="")
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert "click" not in h.notes[0]


def test_review_report_with_two_dates_opens_the_list_page():
    events = [{"kind": "pending", "date": "2026-12-06", "label": "1경기", "name": "2026-12-06-1경기",
               "clips": 18, "auto_at": "2026-12-07T09:00:00+09:00", "warnings": []},
              {"kind": "pending", "date": "2026-12-13", "label": "1경기", "name": "2026-12-13-1경기",
               "clips": 9, "auto_at": "2026-12-14T09:00:00+09:00", "warnings": []}]
    title, message, tags, click = ir.build_review_report(events, REVIEW_URL + "?x=1")
    assert title == "12/6·12/13 하이라이트 검토 준비 — 2경기"
    assert "12/7 오전 9시까지" in message                                   # 가장 이른 자동 게시 시각
    assert click == f"{REVIEW_URL}?x=1&page=review"                         # 날짜가 여럿이면 목록 화면


def test_auto_publish_at_gives_at_least_six_hours_to_review():
    kst = lambda *a: datetime(*a, tzinfo=ir.KST)                            # noqa: E731
    assert ir.auto_publish_at("2026-12-06", kst(2026, 12, 6, 22, 0)) == "2026-12-07T09:00:00+09:00"
    assert ir.auto_publish_at("2026-12-06", kst(2026, 12, 7, 2, 59)) == "2026-12-07T09:00:00+09:00"   # 6시간 남음
    assert ir.auto_publish_at("2026-12-06", kst(2026, 12, 7, 3, 1)) == "2026-12-08T09:00:00+09:00"    # 6시간 안 남음
    assert ir.auto_publish_at("2026-12-06", kst(2026, 12, 9, 10, 0)) == "2026-12-10T09:00:00+09:00"   # 밀린 경기
    assert ir.auto_publish_at("2026-12-06", datetime(2026, 12, 6, 13, 0, tzinfo=UTC)) == "2026-12-07T09:00:00+09:00"


def test_review_mode_falls_back_to_publishing_when_clips_cannot_be_stored(tmp_path, monkeypatch, yt):
    """클립이 없으면 검토도 게시 단계도 쓸 수 없다 — 영상을 잃지 않도록 예전처럼 바로 올린다."""
    h = review_harness(tmp_path, monkeypatch)
    real = h.store.upload_with_id

    def flaky(path, key, name):
        if name.endswith("_c002.mp4"):
            raise OSError("boom")
        return real(path, key, name)

    h.store.upload_with_id = flaky
    h.add("g.mp4", 1500, start_min=0)
    h.run()

    assert [p["title"] for p in h.published] == [GAME]                      # 바로 게시됐다
    assert h.files("clips") == [] and games(h) is None
    assert h.files("done") == [f"{GAME}_원본.mp4"]                          # 원본 이름 변경은 한 번만
    msg = h.notes[0]["message"]
    assert "※ 1경기: 클립 보관 실패 — boom" in msg and "※ 1경기: 클립 보관에 실패해 검토 없이 바로 게시했습니다." in msg
    assert f"· {GAME}\nhttps://youtu.be/1경기" in msg                        # BAND 글이 온다
    assert "검토 준비" not in h.notes[0]["title"]


def test_review_mode_falls_back_when_the_game_record_cannot_be_saved(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    real = h.store.write_json

    def write_json(name, data):
        if name.startswith("games_"):
            raise OSError("state down")
        return real(name, data)

    h.store.write_json = write_json
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert [p["title"] for p in h.published] == [GAME] and h.files("clips") == []   # 클립도 치웠다
    assert "경기 기록 저장 실패 — state down" in h.notes[0]["message"]


def test_review_mode_off_is_the_old_flow_even_with_clips_on(tmp_path, monkeypatch, yt):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert [p["title"] for p in h.published] == [GAME]
    assert games(h)["status"] == "auto_published" and "click" not in h.notes[0]


# ─── 게시 단계 ───────────────────────────────────────────────────────────────
def test_publish_uploads_all_clips_and_sends_the_band_post(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    publish_request(h, tag_source="auto")                                   # 검토 없이 자동 게시
    h.run()

    [up] = yt.uploads
    assert up["title"] == GAME and up["privacy"] == "public"
    assert up["bytes"] == b"clip0clip1"                                     # 클립 전부, 원래 순서
    assert up["description"].startswith(f"축구 하이라이트 — {GAME}")
    assert "0:00 슈팅 #1  (원본 1:40)" in up["description"]
    assert "0:13 슈팅 #2  (원본 6:40)" in up["description"]                  # 둘째 클립은 첫 클립 길이(13.02초)만큼 뒤
    assert "HLEditor" in up["description"] and up["thumb"]

    game = games(h)
    assert (game["status"], game["yt_url"], game["yt_title"]) == ("auto_published", "https://youtu.be/VID1", GAME)
    assert game["published_clips"] == [1, 2] and game["published_at"] and game["error"] is None
    assert game["attempts"] == 1
    day = h.store.read_json(f"day_{D}.json")
    assert day["games"][0]["yt_url"] == "https://youtu.be/VID1" and day["games"][0]["name"] == GAME

    [note] = h.notes
    assert note["title"] == "하이라이트 1경기 완료" and "click" not in note        # 영상을 새로 처리하지 않아 개수는 없다
    assert f"· {GAME}\nhttps://youtu.be/VID1" in note["message"]
    assert "2026년 09월 27일 경기 하이라이트 영상입니다." in note["message"]
    assert not list(h.workdir.glob("pub_*")) and h.files("output") == []


def test_manual_tags_do_not_remove_opponent_goals_but_removed_clips_are_dropped(tmp_path, monkeypatch, yt):
    """주간 영상은 "잘못 잡힘"만 뺀다. 수비·실점으로 표시한 상대 득점 장면은 그대로 올라간다."""
    h = analyzed(tmp_path, monkeypatch, peaks=(100.0, 400.0, 700.0))
    publish_request(h, kind="opponent", opponent="OO FC", attack_end="building",
                    tagged_at="2026-09-27T07:00:00+00:00",
                    clip_tags={"2": {"removed": True}, "3": {"side_override": "defense", "conceded": True}})
    h.run()

    [up] = yt.uploads
    assert up["title"] == f"{GAME}-OO FC"
    assert up["bytes"] == b"clip0clip2"                                     # 2번(잘못 잡힘)만 빠졌고 실점 3번은 남음
    assert "0:00 슈팅 #1  (원본 1:40)" in up["description"] and "0:13 슈팅 #2  (원본 11:40)" in up["description"]
    assert "슈팅 #3" not in up["description"]
    game = games(h)
    assert (game["status"], game["published_clips"]) == ("published", [1, 3])      # 검토를 거쳤으니 published
    assert h.notes[0]["message"].count("https://youtu.be/VID1") == 1
    assert f"· {GAME}-OO FC\nhttps://youtu.be/VID1" in h.notes[0]["message"]       # BAND 글도 상대 이름까지


def test_originals_get_the_final_name_with_the_opponent(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    h.add("p1.mp4", 1800, start_min=0)
    h.add("p2.mp4", 600, start_min=30)
    h.run()
    assert h.files("done") == [f"{GAME}_원본1.mp4", f"{GAME}_원본2.mp4"]
    publish_request(h, kind="opponent", opponent="OO FC")
    h.run()
    assert h.files("done") == [f"{GAME}-OO FC_원본1.mp4", f"{GAME}-OO FC_원본2.mp4"]
    doc = h.store.read_json(f"clips_{D}_1경기.json")
    assert [o["name"] for o in doc["originals"]] == h.files("done")
    assert [o["file_id"] for o in doc["originals"]] == h.files("done")        # 로컬 저장소는 id가 파일명
    assert f"{GAME}-OO FC_원본1.mp4" in h.store.read_json("claims.json")


def test_scrimmage_name_and_opponent_free_title(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    publish_request(h, kind="scrimmage", opponent="OO FC")
    h.run()
    assert yt.uploads[0]["title"] == f"{GAME}-청백전" and games(h)["yt_title"] == f"{GAME}-청백전"


def test_single_clip_game_is_copied_not_concatenated(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch, peaks=(100.0,))
    publish_request(h)
    h.run()
    assert yt.uploads[0]["bytes"] == b"clip0" and games(h)["published_clips"] == [1]


def test_all_clips_removed_means_nothing_is_published(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    publish_request(h, kind="scrimmage", clip_tags={"1": {"removed": True}, "2": {"removed": True}})
    h.run()
    assert yt.uploads == []
    game = games(h)
    assert (game["status"], game["yt_url"], game["published_clips"]) == ("published", None, [])
    [note] = h.notes
    assert "※ 1경기: 모든 장면이 '잘못 잡힘'으로 표시돼 YouTube에 올리지 않았습니다." in note["message"]
    assert "경기 하이라이트 영상입니다" not in note["message"]                # BAND 글에서 뺀다


def test_youtube_failure_backs_up_and_can_be_republished(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    yt.upload_error = "YouTube 인증이 만료되었거나 취소되었습니다. 다시 로그인해주세요."
    publish_request(h, kind="opponent", opponent="A/B")
    h.run()

    game = games(h)
    assert game["status"] == "failed" and "인증이 만료" in game["error"] and game["yt_url"] is None
    assert h.files("output") == [f"{GAME}-A_B.mp4"]                          # 백업 이름은 파일에 못 쓰는 문자를 _로
    [note] = h.notes
    assert "※ 1경기: YouTube 업로드 실패 — YouTube 인증이 만료" in note["message"]
    assert "make_kaggle_secrets.py" in note["message"] and f"{GAME}-A_B.mp4" in note["message"]
    assert note["tags"] == ["warning"]

    h.notes.clear()
    yt.upload_error = None
    set_game(h, status="publish")                                            # 검토 페이지의 "다시 게시"
    h.run()
    assert games(h)["status"] == "published" and games(h)["yt_url"] == "https://youtu.be/VID1"
    assert games(h)["attempts"] == 2 and games(h)["error"] is None
    assert yt.uploads[0]["title"] == f"{GAME}-A/B"                           # YouTube 제목에는 원래 이름


def test_a_game_that_is_not_in_publish_state_is_left_alone(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    request(h, {"type": "publish", "date": D})                               # 대기열에 있어도 status가 pending
    h.run()
    assert yt.uploads == [] and games(h)["status"] == "pending" and h.notes == []

    publish_request(h)
    h.run()
    h.run()                                                                  # 요청이 남아 있어도 한 번만 올린다
    assert len(yt.uploads) == 1 and games(h)["status"] == "published"


def test_each_publish_game_of_the_day_is_uploaded_in_order(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    h.add("g1.mp4", 1500, start_min=0)
    h.add("g2.mp4", 1500, start_min=60)
    h.run()
    set_game(h, "1경기", status="publish", tag_source="auto")
    set_game(h, "2경기", status="publish", tag_source="auto")
    request(h, {"type": "publish", "date": D})
    h.notes.clear()
    h.run()
    assert [u["title"] for u in yt.uploads] == [GAME, "2026-09-27-2경기"]
    [note] = h.notes
    assert note["title"] == "하이라이트 2경기 완료"
    assert note["message"].index("2026-09-27-1경기") < note["message"].index("2026-09-27-2경기")


def test_publish_requests_go_out_before_new_videos_are_analysed(tmp_path, monkeypatch, yt):
    """게시는 빨리 나가야 한다 — 실행 시작 때 대기열의 게시 요청을 새 영상 분석보다 먼저 처리한다."""
    h = analyzed(tmp_path, monkeypatch)
    publish_request(h, tag_source="auto")
    h.add("g2.mp4", 1500, start_min=60)
    built_when_uploaded = []
    real = yt.upload

    def spy(*args, **kw):
        built_when_uploaded.append(len(h.built))
        return real(*args, **kw)

    monkeypatch.setattr(ir.yt_up, "upload_video", spy)
    h.run()
    assert built_when_uploaded == [1]                                        # 새 영상(g2)은 아직 분석 전이었다
    assert len(h.built) == 2 and games(h, "2경기")["status"] == "pending"


def test_missing_clip_file_fails_the_game_with_a_reason(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    (h.root / "06_clips" / f"{GAME}_c002.mp4").unlink()
    publish_request(h)
    h.run()
    assert yt.uploads == [] and games(h)["status"] == "failed"
    assert "클립 파일이 Drive 06_clips에 없습니다" in h.notes[0]["message"]
    assert "검토 페이지에서 다시 게시" in h.notes[0]["message"]


def test_missing_clips_json_fails_the_game(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    (h.root / "_state" / f"clips_{D}_1경기.json").unlink()
    publish_request(h)
    h.run()
    assert games(h)["status"] == "failed" and "clips_*.json" in games(h)["error"]


def test_dead_running_game_is_retried_and_gives_up_after_three_attempts(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    set_game(h, status="running", attempts=1, tag_source="auto")             # 지난 실행이 중간에 죽었다
    request(h, {"type": "publish", "date": D})
    h.run()
    assert games(h)["status"] == "auto_published" and games(h)["attempts"] == 2 and len(yt.uploads) == 1

    h2 = analyzed(tmp_path / "again", monkeypatch)
    set_game(h2, status="running", attempts=pub.MAX_ATTEMPTS)
    request(h2, {"type": "publish", "date": D})
    h2.run()
    assert games(h2)["status"] == "failed" and len(yt.uploads) == 1           # 더 올리지 않는다
    assert f"게시를 {pub.MAX_ATTEMPTS}번 시도하다 중간에 멈춰서" in h2.notes[0]["message"]


def test_video_already_uploaded_before_a_crash_is_not_uploaded_twice(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    set_game(h, status="running", attempts=1, tag_source="auto", yt_url="https://youtu.be/ALREADY",
             yt_title=GAME)
    request(h, {"type": "publish", "date": D})
    h.run()
    assert yt.uploads == []                                                  # 다시 올리지 않았다
    assert games(h)["status"] == "auto_published" and games(h)["yt_url"] == "https://youtu.be/ALREADY"
    assert "https://youtu.be/ALREADY" in h.notes[0]["message"]


def test_dry_run_publish_keeps_the_result_locally(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch, dry_run=True)
    publish_request(h)
    h.run()
    assert yt.uploads == [] and games(h)["yt_url"] == "https://youtu.be/DRYRUN-1경기"
    assert h.files("output") == [f"{GAME}.mp4"]
    assert "[1경기] (dry-run) YouTube 제목: 2026-09-27-1경기" in h.logs


def test_unreadable_requests_do_not_stop_the_run(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    real = h.store.read_json

    def read_json(name):
        if name == "requests.json":
            raise OSError("drive hiccup")
        return real(name)

    h.store.read_json = read_json
    h.add("g2.mp4", 1500, start_min=60)
    summary = h.run()                                                        # 영상 처리는 계속된다
    assert len(summary["games"]) == 1 and any("요청 대기열을 읽지 못했습니다" in m for m in h.logs)


def test_auth_error_reading_requests_propagates(tmp_path, monkeypatch, yt):
    from google.auth.exceptions import RefreshError
    h = analyzed(tmp_path, monkeypatch)                                      # 러너(h.runner)를 한 번 돌려 만든다

    def dead(name):
        raise RefreshError("invalid_grant")

    h.store.read_json = dead
    with pytest.raises(RefreshError):
        h.runner.process_requests()


def test_one_failing_game_does_not_block_the_others(tmp_path, monkeypatch, yt):
    h = review_harness(tmp_path, monkeypatch)
    h.add("g1.mp4", 1500, start_min=0)
    h.add("g2.mp4", 1500, start_min=60)
    h.run()
    (h.root / "_state" / f"clips_{D}_1경기.json").unlink()
    set_game(h, "1경기", status="publish")
    set_game(h, "2경기", status="publish", tag_source="auto")
    request(h, {"type": "publish", "date": D})
    h.notes.clear()
    h.run()
    assert games(h, "1경기")["status"] == "failed" and games(h, "2경기")["status"] == "auto_published"
    assert [u["title"] for u in yt.uploads] == ["2026-09-27-2경기"]
    assert "하이라이트 1경기 완료 · 확인 필요 1건" == h.notes[0]["title"]


# ─── 늦은 태깅 → 제목 변경 ───────────────────────────────────────────────────
def late_tag(h, tagged_at="2026-09-27T05:00:00+00:00", **fields):
    """게시 뒤에 태깅한 것처럼(가짜 시계는 06:00Z 근처) — 태깅 시각이 반영(retitled_at)보다 앞서면 이미 반영된 것이다."""
    set_game(h, tagged_at=tagged_at, tag_source="manual", **fields)
    request(h, {"type": "retitle", "date": D, "label": "1경기"})


def published_game(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    publish_request(h, tag_source="auto")
    h.run()
    h.notes.clear()
    assert games(h)["status"] == "auto_published"
    return h


def test_late_tagging_retitles_the_video_without_uploading_again(tmp_path, monkeypatch, yt):
    h = published_game(tmp_path, monkeypatch, yt)
    late_tag(h, kind="opponent", opponent="OO FC")
    h.run()

    assert yt.updates == [{"id": "VID1", "title": f"{GAME}-OO FC", "first_line": f"축구 하이라이트 — {GAME}-OO FC"}]
    assert len(yt.uploads) == 1                                              # 다시 올리지 않는다
    game = games(h)
    assert game["yt_title"] == f"{GAME}-OO FC" and game["retitled_at"] and game["status"] == "auto_published"
    assert h.store.read_json(f"day_{D}.json")["games"][0]["name"] == f"{GAME}-OO FC"
    assert h.files("done") == [f"{GAME}-OO FC_원본.mp4"]
    assert h.notes == []                                                     # 이미 보낸 BAND 글은 고치지 않는다

    h.run()
    assert len(yt.updates) == 1                                              # 반영된 요청은 다시 하지 않는다


def test_retag_without_a_name_change_only_marks_it_done(tmp_path, monkeypatch, yt):
    h = published_game(tmp_path, monkeypatch, yt)
    late_tag(h, clip_tags={"1": {"removed": True}})                          # 월간에만 반영 — 이름은 그대로
    h.run()
    assert yt.updates == [] and games(h)["retitled_at"]


def test_second_retag_after_a_retitle_updates_again(tmp_path, monkeypatch, yt):
    h = published_game(tmp_path, monkeypatch, yt)
    late_tag(h, kind="opponent", opponent="OO FC")
    h.run()
    set_game(h, tagged_at="2026-09-27T07:00:00+00:00", opponent="XX FC")      # 태깅 고치기: 상대 이름을 바꿈
    h.run()
    assert [u["title"] for u in yt.updates] == [f"{GAME}-OO FC", f"{GAME}-XX FC"]
    assert h.files("done") == [f"{GAME}-XX FC_원본.mp4"]


def test_retitle_failure_is_reported_and_stops_after_three_attempts(tmp_path, monkeypatch, yt):
    h = published_game(tmp_path, monkeypatch, yt)
    late_tag(h, kind="scrimmage")
    yt.update_error = "YouTube에서 영상을 찾지 못했습니다: VID1"
    h.run()
    assert games(h)["retitle_attempts"] == 1 and not games(h).get("retitled_at")
    assert games(h)["yt_title"] == GAME                                       # 바뀌지 않았다
    assert "※ 1경기: YouTube 제목을 바꾸지 못했습니다 — YouTube에서 영상을 찾지 못했습니다" in h.notes[0]["message"]
    h.run()
    h.run()
    h.run()
    assert games(h)["retitle_attempts"] == pub.MAX_ATTEMPTS                   # 계속 두드리지 않는다


def test_retitle_ignores_games_that_are_not_published_or_not_tagged(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    request(h, {"type": "retitle", "date": D, "label": "1경기"})
    set_game(h, tagged_at="2026-09-27T05:00:00+00:00", kind="scrimmage")       # 아직 pending
    h.run()
    assert yt.updates == []
    set_game(h, status="auto_published", yt_url="https://youtu.be/V", yt_title=GAME, tagged_at=None)
    h.run()                                                                    # 태깅 기록이 없다
    assert yt.updates == []


def test_retitle_skips_renaming_originals_that_were_already_deleted(tmp_path, monkeypatch, yt):
    h = published_game(tmp_path, monkeypatch, yt)
    (h.root / "03_done" / f"{GAME}_원본.mp4").unlink()                          # 7일 뒤 휴지통으로 갔다
    late_tag(h, kind="opponent", opponent="OO FC")
    h.run()
    assert games(h)["yt_title"] == f"{GAME}-OO FC" and len(yt.updates) == 1
    assert h.notes == []                                                       # 경고도 없다


def test_dry_run_retitle_does_not_call_youtube(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch, dry_run=True)
    publish_request(h, tag_source="auto")
    h.run()
    late_tag(h, kind="scrimmage")
    h.run()
    assert yt.updates == [] and games(h)["yt_title"] == f"{GAME}-청백전"
    assert f"[1경기] (dry-run) YouTube 제목 변경: {GAME} → {GAME}-청백전" in h.logs


# ─── 순수 도우미 ─────────────────────────────────────────────────────────────
def test_description_positions_accumulate_clip_durations():
    clips = [
        {"idx": 1, "src_start": 92.0, "dur": 13.02, "cands": [{"peak": 100.0, "type": "goal"}]},
        {"idx": 3, "src_start": 692.0, "dur": 14.5,
         "cands": [{"peak": 700.0, "type": "shot"}, {"peak": 703.0, "type": "other"},
                   {"peak": 705.0, "type": "save"}]},
        {"idx": 4, "src_start": 900.0, "dur": 13.0, "cands": [{"peak": 910.0, "type": None}]},
    ]
    text = pub.description_from_clips("2026-12-06-1경기-OO FC", clips, 8.0)
    lines = text.splitlines()
    assert lines[0] == "축구 하이라이트 — 2026-12-06-1경기-OO FC"
    assert "슈팅 장면" in lines
    shots = [ln for ln in lines if "슈팅 #" in ln]
    assert shots == ["0:00 슈팅 #1  (원본 1:40)",
                     "0:13 슈팅 #2  (원본 11:40)",                              # 13.02초 뒤 = 둘째 클립의 시작
                     "0:18 슈팅 #3  (원본 11:45)"]                              # 705-8=697 → 둘째 클립 안 5초 뒤
    assert "---" in lines and lines[-1].startswith("이 영상은 HLEditor")


def test_description_without_shots_has_no_shot_list():
    text = pub.description_from_clips("x", [{"idx": 1, "src_start": 0, "dur": 13, "cands": [{"peak": 5, "type": "other"}]}], 8.0)
    assert "슈팅 장면" not in text and text.startswith("축구 하이라이트 — x")


def test_generate_description_and_format_description_agree():
    """PC 앱이 쓰는 generate_description이 같은 포맷터를 거쳐도 출력이 같다."""
    cands = [{"peak": 100.0, "type": "goal"}]
    text = ir.yt_up.generate_description(cands, [0], "2026-12-06-1경기.mp4", pre_sec=8.0, post_sec=5.0)
    assert text == ir.yt_up.format_description("2026-12-06-1경기", ["0:00 슈팅 #1  (원본 1:40)"])


def test_build_report_leaves_out_the_video_count_when_nothing_was_processed():
    days = {D: {"date": D, "games": [{"label": "1경기", "yt_url": "https://youtu.be/a"}]}}
    events = [{"kind": "game", "date": D, "label": "1경기", "files": 0, "yt_url": "https://youtu.be/a", "warnings": []}]
    title, message, tags = ir.build_report(events, days)
    assert title == "하이라이트 1경기 완료" and tags == ["soccer"]
    assert "· 2026-09-27-1경기\nhttps://youtu.be/a" in message


def test_band_text_prefers_the_recorded_final_name():
    day = {"date": D, "games": [{"label": "1경기", "yt_url": "https://youtu.be/a", "name": f"{GAME}-OO FC"},
                                {"label": "2경기", "yt_url": "https://youtu.be/b"}]}
    text = ir.band_text(day)
    assert f"· {GAME}-OO FC\nhttps://youtu.be/a" in text and "· 2026-09-27-2경기\nhttps://youtu.be/b" in text


def test_settings_for_review_mode():
    assert ir.settings_from({}, ir.parse_args([])).hl_review is False
    s = ir.settings_from({"HL_REVIEW": "1", "REVIEW_URL": " https://x/exec "}, ir.parse_args([]))
    assert s.hl_review is True and s.review_url == "https://x/exec"
    assert ir.settings_from({}, ir.parse_args(["--review"])).hl_review is True
    assert ir.settings_from({"HL_REVIEW": "0"}, ir.parse_args([])).hl_review is False


def test_publisher_uses_the_runners_clock_for_published_at(tmp_path, monkeypatch, yt):
    h = analyzed(tmp_path, monkeypatch)
    publish_request(h)
    h.run()
    published = datetime.fromisoformat(games(h)["published_at"])
    assert published >= T0 + timedelta(hours=3) and games(h)["status"] == "published"
    assert json.dumps(games(h), ensure_ascii=False)                          # JSON으로 쓸 수 있는 값만 남았다
