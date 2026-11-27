"""kaggle_runner.retention — 06_clips 클립 보관 규칙과 정리 (MONTHLY_PLAN.md 3-8).

만료 시각 규칙 표는 tests/data/clip_rules.json 하나이고 Apps Script(code_test.js)도 같은 파일을 읽는다.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_inbox_runner import Harness

from kaggle_runner import inbox_runner as ir
from kaggle_runner import naming, retention
from kaggle_runner.common import iso

RULES = json.loads((Path(__file__).parent / "data" / "clip_rules.json").read_text(encoding="utf-8"))
NOW = datetime(2027, 1, 20, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize("case", RULES["expiryCases"], ids=lambda c: c["name"])
def test_clip_expiry_rule_table(case):
    done = ir.from_iso(case["monthlyDoneAt"])
    assert iso(retention.clip_expires_at(case["gameDate"], done)) == case["expiresAt"]


def test_expiry_parameters_can_be_changed():
    done = datetime(2027, 1, 1, tzinfo=UTC)
    assert retention.clip_expires_at("2026-12-06", done, keep_after_done_days=3) == done + timedelta(days=3)
    assert retention.clip_expires_at("2026-12-06", None, max_days=10) == (
        datetime(2026, 12, 16, tzinfo=ir.KST))


def test_monthly_done_at_only_counts_a_finished_monthly():
    assert retention.monthly_done_at(None) is None
    assert retention.monthly_done_at({"status": "build", "done_at": "2027-01-01T00:00:00+00:00"}) is None
    assert retention.monthly_done_at({"status": "failed"}) is None
    assert retention.monthly_done_at({"status": "done"}) is None                      # 시각을 못 읽으면 상한 규칙만
    assert retention.monthly_done_at({"status": "done", "done_at": "2027-01-01T00:00:00+00:00"}) == datetime(
        2027, 1, 1, tzinfo=UTC)


# ─── 정리 ────────────────────────────────────────────────────────────────────
def runner(h, now=NOW):
    h.clock.t = now
    return ir.InboxRunner(h.store, h.notifier, h.settings, workdir=h.workdir, now=h.clock.now,
                          sleep=h.clock.sleep, log=h.logs.append)


def seed_game(h, date, label="1경기", *, clips=3, status="auto_published", created="2026-12-01T00:00:00+00:00",
              clips_json=True):
    """06_clips에 클립 파일, _state에 games·clips JSON을 둔다."""
    for i in range(1, clips + 1):
        (h.root / "06_clips" / naming.clip_name(date, label, i)).write_bytes(b"clip")
    h.store.write_json(f"games_{date}_{label}.json", {"date": date, "label": label, "status": status})
    if clips_json:
        h.store.write_json(f"clips_{date}_{label}.json", {"date": date, "label": label, "created_at": created,
                                                         "clips": [{"idx": i} for i in range(1, clips + 1)]})


def left(h):
    return sorted(p.name for p in (h.root / "06_clips").iterdir())


def trashed(h):
    d = h.root / ".trash"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_clips_are_trashed_a_week_after_their_monthly_is_done(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-12-06")
    seed_game(h, "2026-12-13", "2경기")
    h.store.write_json("monthly_2026-12.json", {"status": "done", "done_at": "2027-01-01T00:30:00+00:00"})
    n = retention.cleanup_clips(runner(h, datetime(2027, 1, 8, 0, 29, tzinfo=UTC)))
    assert n == 0 and len(left(h)) == 6                                    # 7일이 되기 1분 전
    n = retention.cleanup_clips(runner(h, datetime(2027, 1, 8, 0, 30, tzinfo=UTC)))
    assert n == 6 and left(h) == [] and len(trashed(h)) == 6              # 휴지통 — 영구 삭제가 아니다
    doc = h.store.read_json("clips_2026-12-06_1경기.json")
    assert doc["expired"] is True and doc["expired_at"] == "2027-01-08T00:30:00+00:00"
    assert h.store.read_json("games_2026-12-06_1경기.json")                 # _state는 지우지 않는다


def test_only_the_months_whose_monthly_is_done_are_cleaned_early(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-12-06")                                             # 12월 월간 done
    seed_game(h, "2027-01-03")                                             # 1월 월간은 아직
    h.store.write_json("monthly_2026-12.json", {"status": "done", "done_at": "2027-01-01T00:30:00+00:00"})
    h.store.write_json("monthly_2027-01.json", {"status": "waiting_tags"})
    retention.cleanup_clips(runner(h, datetime(2027, 1, 20, tzinfo=UTC)))
    assert left(h) == [naming.clip_name("2027-01-03", "1경기", i) for i in (1, 2, 3)]


def test_without_a_finished_monthly_the_75_day_cap_applies(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-12-06", created="2026-12-06T10:00:00+00:00")
    h.store.write_json("monthly_2026-12.json", {"status": "failed", "error": "x"})   # 실패한 월간은 끝난 게 아니다
    assert retention.cleanup_clips(runner(h, datetime(2027, 2, 18, 14, 59, tzinfo=UTC))) == 0
    assert retention.cleanup_clips(runner(h, datetime(2027, 2, 18, 15, 0, tzinfo=UTC))) == 3
    assert h.store.read_json("clips_2026-12-06_1경기.json")["expired"] is True


def test_unpublished_games_keep_their_clips_even_past_the_cap(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    for i, status in enumerate(("pending", "publish", "running"), start=1):
        seed_game(h, "2026-10-01", f"{i}경기", status=status)
    seed_game(h, "2026-10-01", "4경기", status="failed")                    # 실패는 지난 일 — 정리 대상
    n = retention.cleanup_clips(runner(h))
    assert n == 3 and all(n.startswith("2026-10-01-") for n in left(h)) and len(left(h)) == 9


def test_clips_are_kept_a_week_after_they_were_made(tmp_path, monkeypatch):
    """월간이 끝난 뒤 늦게 올린 지난달 경기가 올리자마자 정리되지 않는다."""
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-12-28", created="2027-01-14T00:00:00+00:00")        # 1/14에 늦게 올림
    h.store.write_json("monthly_2026-12.json", {"status": "done", "done_at": "2027-01-01T00:30:00+00:00"})
    assert retention.cleanup_clips(runner(h, datetime(2027, 1, 20, 23, 59, tzinfo=UTC))) == 0   # 만든 지 6일
    assert retention.cleanup_clips(runner(h, datetime(2027, 1, 21, 0, 0, tzinfo=UTC))) == 3       # 7일


def test_files_that_do_not_look_like_clips_are_never_touched(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-10-01", clips=1, status="auto_published")
    for name in ("memo.mp4", "2026-10-01-1경기_원본.mp4", "2026-10-01-1경기_c001.mov", "x_c001.mp4"):
        (h.root / "06_clips" / name).write_bytes(b"keep")
    retention.cleanup_clips(runner(h))
    assert left(h) == ["2026-10-01-1경기_c001.mov", "2026-10-01-1경기_원본.mp4", "memo.mp4", "x_c001.mp4"]


def test_a_failure_in_one_game_does_not_stop_the_others_and_auth_errors_propagate(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-10-01", "1경기", clips=1)
    seed_game(h, "2026-10-02", "1경기", clips=1)
    real = h.store.trash

    def flaky(rf, key):
        if rf.name.startswith("2026-10-01"):
            raise OSError("drive hiccup")
        return real(rf, key)

    h.store.trash = flaky
    assert retention.cleanup_clips(runner(h)) == 1                         # 두 번째 경기는 정리됐다
    assert left(h) == [naming.clip_name("2026-10-01", "1경기", 1)]
    assert any("클립 정리 실패" in m for m in h.logs)

    def dead(rf, key):
        raise RefreshError("invalid_grant")

    h.store.trash = dead
    with pytest.raises(RefreshError):
        retention.cleanup_clips(runner(h))


def test_missing_clips_json_still_trashes_the_expired_files(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-10-01", clips=2, clips_json=False)
    assert retention.cleanup_clips(runner(h)) == 2 and left(h) == []


def test_run_cleans_expired_clips_at_start_and_a_failure_does_not_stop_the_run(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-06-01", clips=1, created="2026-06-02T00:00:00+00:00")   # 75일이 한참 지난 경기(Harness 시계는 9/27)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert not any(n.startswith("2026-06-01") for n in left(h))
    assert [n["title"] for n in h.notes] == ["하이라이트 1경기 완료 (영상 1개 처리)"]

    monkeypatch.setattr(ir, "cleanup_clips", lambda r: (_ for _ in ()).throw(OSError("boom")))
    h2 = Harness(tmp_path / "again", monkeypatch)
    h2.add("g.mp4", 1500, start_min=0)
    h2.run()                                                               # 정리가 죽어도 영상 처리는 한다
    assert h2.published and any("클립 정리를 건너뜁니다" in m for m in h2.logs)


def test_monthly_settings_can_come_from_secrets():
    s = ir.settings_from({}, ir.parse_args([]))
    assert (s.monthly_target_min, s.monthly_crf) == (6.0, 20)
    s = ir.settings_from({"MONTHLY_TARGET_MIN": "8", "MONTHLY_CRF": "18"}, ir.parse_args([]))
    assert (s.monthly_target_min, s.monthly_crf) == (8.0, 18)
    s = ir.settings_from({"MONTHLY_CRF": "bogus", "MONTHLY_TARGET_MIN": ""}, ir.parse_args([]))
    assert (s.monthly_target_min, s.monthly_crf) == (6.0, 20)             # 모르는 값은 기본값으로


def test_retention_periods_are_fixed_and_cannot_be_overridden_by_secrets(tmp_path, monkeypatch):
    """Apps Script의 7일 전 알림과 어긋나지 않도록 보관 기간은 상수 — 비밀값에 적어도 무시한다."""
    assert (retention.CLIP_KEEP_AFTER_DONE_DAYS, retention.CLIP_MAX_DAYS) == (7, 75)
    h = Harness(tmp_path, monkeypatch)
    h.settings = ir.settings_from({"CLIP_MAX_DAYS": "30", "CLIP_KEEP_AFTER_DONE_DAYS": "1"}, ir.parse_args([]))
    assert not hasattr(h.settings, "clip_max_days") and not hasattr(h.settings, "clip_keep_after_done_days")
    seed_game(h, "2026-12-06", created="2026-12-06T10:00:00+00:00")
    h.store.write_json("monthly_2026-12.json", {"status": "done", "done_at": "2027-01-01T00:30:00+00:00"})
    assert retention.cleanup_clips(runner(h, datetime(2027, 1, 5, tzinfo=UTC))) == 0         # 1일이 아니라 7일 규칙
    assert retention.cleanup_clips(runner(h, datetime(2027, 1, 8, 0, 30, tzinfo=UTC))) == 3
