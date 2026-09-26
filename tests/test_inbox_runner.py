"""kaggle_runner.inbox_runner — 인박스 처리 흐름(로컬 저장소 + 무거운 단계는 가짜로 대체)."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaggle_runner import inbox_runner as ir
from kaggle_runner.notify import Notifier
from kaggle_runner.stores import FOLDERS, LocalStore

T0 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)      # 2026-09-27(일) 12:00 KST 녹화 시작


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


class _Ok:
    status_code = 200


class Harness:
    """LocalStore 위에서 InboxRunner를 돌린다. ffmpeg·Gemini·YouTube 단계는 가짜."""

    def __init__(self, tmp_path, monkeypatch, *, gpu=False, **settings):
        self.root = tmp_path / "store"
        self.store = LocalStore(self.root)
        self.workdir = tmp_path / "work"
        self.clock = Clock(T0 + timedelta(hours=3))
        self.gpu = gpu
        self.infos = {}
        self.notes = []
        self.published = []
        self.built = []
        self.publish_error = None
        self.build_fail = set()
        self.verdict = lambda c: {"highlight": True, "confidence": 0.9, "type": "goal", "reason": "골"}
        self.settings = ir.Settings(gemini_key="k", default_title="한울타리 FC 경기영상", **settings)
        self.notifier = Notifier("hl-topic", post=self._post, log=lambda m: None)
        monkeypatch.setattr(ir, "probe_media", self._probe)
        monkeypatch.setattr(ir, "analyze_candidates", self._analyze)
        monkeypatch.setattr(ir.sh, "concat_videos", self._concat)
        monkeypatch.setattr(ir.sh, "detect_spikes", lambda video, wd, **k: [
            {"peak": 100.0, "delta_db": 12.0}, {"peak": 400.0, "delta_db": 9.0}])
        monkeypatch.setattr(ir.sh, "build_output", self._build)
        monkeypatch.setattr(ir.yt_up, "publish_highlight", self._publish)

    # 준비 --------------------------------------------------------------------
    def add(self, name, duration, *, start_min=None, folder="inbox", height=1080,
            arrived=None):
        path = self.root / FOLDERS[folder] / name
        path.write_bytes(name.encode() * 10)
        ts = (arrived or T0 + timedelta(minutes=60)).timestamp()
        os.utime(path, (ts, ts))
        start = T0 + timedelta(minutes=start_min) if start_min is not None else None
        self.infos[name] = ir.MediaInfo(duration=duration, width=1920, height=height,
                                        video_bps=8_000_000, start_utc=start)

    def files(self, key):
        return sorted(p.name for p in (self.root / FOLDERS[key]).iterdir() if p.is_file())

    def run(self):
        runner = ir.InboxRunner(self.store, self.notifier, self.settings, workdir=self.workdir,
                                gpu=self.gpu, now=self.clock.now, sleep=self.clock.sleep,
                                log=lambda m: None)
        self.runner = runner
        return runner.run()

    # 가짜 단계 -----------------------------------------------------------------
    def _probe(self, path):
        return next(info for name, info in self.infos.items() if Path(path).name.endswith(name))

    def _analyze(self, video, workdir, cands, gemini_key=None, log=print):
        for c in cands:
            c.update(self.verdict(c))
        return {"vision_used": True, "usage": {"calls": len(cands), "cost_usd": 0.01},
                "pan": {"range_px": 500.0, "reliable": True, "boosted": 0}}

    def _concat(self, parts, out, workdir=None):
        Path(out).write_bytes(b"".join(Path(p).read_bytes() for p in parts))
        return str(out)

    def _build(self, video, selected, out, workdir, **kw):
        if any(name in str(video) for name in self.build_fail):
            raise RuntimeError("ffmpeg 실패(시험)")
        Path(out).write_bytes(b"mp4")
        self.built.append({"video": str(video), "n": len(selected), "title": kw.get("title")})

    def _publish(self, output, source, cands, approved, label, title, **kw):
        if self.publish_error:
            raise RuntimeError(self.publish_error)
        self.published.append({"label": label, "title": title, "approved": approved,
                               "privacy": kw.get("privacy")})
        return f"https://youtu.be/{label}"

    def _post(self, url, data, headers, timeout):
        self.notes.append(json.loads(data.decode("utf-8")))
        return _Ok()


@pytest.fixture
def h(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_split_game_and_single_game_are_processed_in_recording_order(h):
    h.add("g2.mp4", 1500, start_min=60)
    h.add("p2.mp4", 600, start_min=30)
    h.add("p1.mp4", 1800, start_min=0)
    summary = h.run()

    assert [p["label"] for p in h.published] == ["1경기", "2경기"]
    assert h.published[0]["title"] == "한울타리 FC 경기영상 | 1경기 | 2026-09-27"
    assert h.published[0]["approved"] == [0, 1]
    assert "merged.mp4" in h.built[0]["video"]                 # 1경기는 두 파트를 합친 영상
    assert h.built[0]["title"] == "한울타리 FC 경기영상"        # 워터마크
    assert h.files("done") == ["g2.mp4", "p1.mp4", "p2.mp4"]
    assert h.files("inbox") == [] and h.files("processing") == []

    done = [n for n in h.notes if "하이라이트 완료" in n["title"]]
    assert [n["title"] for n in done] == ["1경기 하이라이트 완료 (오늘 1개)",
                                          "2경기 하이라이트 완료 (오늘 2개)"]
    assert "나뉜 파일 2개를 한 경기로 합쳤습니다" in done[0]["message"]
    last = done[1]["message"]                                   # 그날 BAND 글 전체
    assert "2026년 09월 27일" in last
    assert "https://youtu.be/1경기" in last and "https://youtu.be/2경기" in last
    assert "click" not in done[1]                               # 탭하면 복사되도록

    day = h.store.read_json("day_2026-09-27.json")
    assert [g["label"] for g in day["games"]] == ["1경기", "2경기"]
    assert day["games"][0]["sources"] == ["p1.mp4", "p2.mp4"]
    assert h.store.read_json("results_2026-09-27_1경기.json")["candidates"]
    assert h.store.read_json("claims.json")["p1.mp4"]["status"] == "done"
    assert len(summary["games"]) == 2 and summary["failed"] == []


def test_label_continues_from_earlier_run_same_day(h):
    h.store.write_json("day_2026-09-27.json", {"date": "2026-09-27", "games": [
        {"label": "1경기", "yt_url": "https://youtu.be/old"}]})
    h.add("g.mp4", 1500, start_min=90)
    h.run()
    assert h.published[0]["label"] == "2경기"
    assert "https://youtu.be/old" in h.notes[-1]["message"]


def test_only_approved_candidates_are_passed_to_youtube(h):
    h.verdict = lambda c: ({"highlight": True, "confidence": 0.9, "type": "goal"}
                           if c["peak"] == 400.0 else {"highlight": False, "confidence": 0.1})
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.published[0]["approved"] == [1]


def test_too_short_video_goes_to_failed(h):
    h.add("clip.mp4", 100, start_min=0)
    h.run()
    assert h.files("failed") == ["clip.mp4"]
    assert h.notes[0]["title"] == "처리 실패: clip.mp4"
    assert "너무 짧아" in h.notes[0]["message"] and "04_failed" in h.notes[0]["message"]
    assert h.published == []


def test_no_highlights_still_takes_a_game_number(h):
    h.verdict = lambda c: {"highlight": False, "confidence": 0.2, "type": "other"}
    h.add("quiet.mp4", 1500, start_min=0)
    h.run()
    assert h.notes[0]["title"] == "1경기: 하이라이트 없음"
    assert h.files("done") == ["quiet.mp4"] and h.published == []
    assert h.store.read_json("day_2026-09-27.json")["games"][0]["yt_url"] is None


def test_youtube_failure_backs_up_to_drive_and_says_how_to_fix_auth(h):
    h.publish_error = "YouTube 인증이 만료되었거나 취소되었습니다. 다시 로그인해주세요."
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    note = h.notes[0]
    assert note["title"] == "1경기 YouTube 업로드 실패"
    assert "make_kaggle_secrets.py" in note["message"]
    assert note["click"].endswith("highlight_2026-09-27_1경기.mp4")
    assert h.files("output") == ["highlight_2026-09-27_1경기.mp4"]
    assert h.files("done") == ["g.mp4"]


def test_processing_error_fails_that_game_and_continues(h):
    h.build_fail = {"bad.mp4"}
    h.add("bad.mp4", 1500, start_min=0)
    h.add("good.mp4", 1500, start_min=60)
    summary = h.run()
    assert h.files("failed") == ["bad.mp4"] and h.files("done") == ["good.mp4"]
    assert "ffmpeg 실패" in summary["failed"][0]["reason"]
    assert [p["label"] for p in h.published] == ["1경기"]      # 실패한 경기는 번호를 쓰지 않음


def test_quality_warning_is_appended(h):
    h.add("g.mp4", 1500, start_min=0, height=720)
    h.run()
    assert "720p" in h.notes[0]["message"] and "사진 및 동영상" in h.notes[0]["message"]


def test_head_part_waits_for_next_part_then_times_out(h):
    h.add("head.mp4", 1800, start_min=0, arrived=h.clock.now())
    h.run()
    note = h.notes[0]
    assert note["title"].startswith("1경기 하이라이트 완료")
    assert "다음 파트가 오지 않아" in note["message"]
    assert h.clock.now() - (T0 + timedelta(hours=3)) >= timedelta(minutes=40)


def test_once_releases_waiting_parts_back_to_inbox(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, once=True)
    h.add("head.mp4", 1800, start_min=0, arrived=h.clock.now())
    summary = h.run()
    assert h.files("inbox") == ["head.mp4"] and summary["released"] == ["head.mp4"]
    assert h.store.read_json("claims.json")["head.mp4"]["status"] == "released"


def test_leftover_processing_file_returns_to_its_source(h):
    h.add("x.mp4", 1500, folder="processing")
    h.store.write_json("claims.json", {"x.mp4": {"source": "inbox_2d", "status": "processing",
                                                 "attempts": 1}})
    h.run()                                              # CPU 실행이라 2D 인박스는 건드리지 않음
    assert h.files("inbox_2d") == ["x.mp4"]
    assert h.store.read_json("claims.json")["x.mp4"]["status"] == "recovered"


def test_file_that_keeps_crashing_the_kernel_is_failed(h):
    h.add("crash.mp4", 1500, start_min=0)
    h.store.write_json("claims.json", {"crash.mp4": {"source": "inbox", "status": "recovered",
                                                     "attempts": ir.MAX_ATTEMPTS}})
    h.run()
    assert h.files("failed") == ["crash.mp4"]
    assert "중간에 멈춰서" in h.notes[0]["message"]


def test_user_retry_from_failed_folder_starts_fresh(h):
    h.add("retry.mp4", 1500, start_min=0)
    h.store.write_json("claims.json", {"retry.mp4": {"source": "inbox", "status": "failed",
                                                     "attempts": ir.MAX_ATTEMPTS}})
    h.run()
    assert h.files("done") == ["retry.mp4"]
    assert h.store.read_json("claims.json")["retry.mp4"]["attempts"] == 1


def test_old_done_originals_go_to_trash(h):
    h.add("old.mp4", 1500, folder="done")
    h.add("new.mp4", 1500, folder="done")
    now = h.clock.now()
    h.store.write_json("claims.json", {
        "old.mp4": {"status": "done", "done_at": ir.iso(now - timedelta(days=8)),
                    "updated_at": ir.iso(now - timedelta(days=8))},
        "new.mp4": {"status": "done", "done_at": ir.iso(now - timedelta(days=1)),
                    "updated_at": ir.iso(now - timedelta(days=1))}})
    h.run()
    assert h.files("done") == ["new.mp4"]
    assert (h.root / ".trash" / "old.mp4").exists()
    assert "old.mp4" not in h.store.read_json("claims.json")


def test_cpu_run_leaves_2d_requests_for_gpu_run(h):
    h.add("two_d.mp4", 1500, start_min=0, folder="inbox_2d")
    h.run()
    assert h.files("inbox_2d") == ["two_d.mp4"] and h.published == []


def test_gpu_run_processes_2d_request_and_runs_2d(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, gpu=True)
    calls = []
    monkeypatch.setattr(ir.InboxRunner, "_run_2d",
                        lambda self, video, selected, out, label, date, gd: calls.append(label))
    h.add("two_d.mp4", 1500, start_min=0, folder="inbox_2d")
    h.add("plain.mp4", 1500, start_min=60)
    h.run()
    assert h.files("done") == ["plain.mp4", "two_d.mp4"]
    assert calls == ["1경기"]                                     # 2D 요청 경기만


def test_2d_failure_only_notifies(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, gpu=True)
    import kaggle_runner.run_match as rm
    monkeypatch.setattr(rm, "install_packages", lambda pkgs: (_ for _ in ()).throw(RuntimeError("pip 실패")))
    h.add("two_d.mp4", 1500, start_min=0, folder="inbox_2d")
    h.run()
    titles = [n["title"] for n in h.notes]
    assert titles == ["1경기 하이라이트 완료 (오늘 1개)", "1경기 2D 변환 실패"]
    assert h.files("done") == ["two_d.mp4"]


def test_dry_run_keeps_output_locally_and_skips_youtube(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, dry_run=True)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.published == []
    assert h.files("output") == ["highlight_2026-09-27_1경기.mp4"]
    assert "https://youtu.be/DRYRUN-1경기" in h.notes[0]["message"]


def test_max_segments_keeps_strongest(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_segments=1)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.built[0]["n"] == 1 and h.published[0]["approved"] == [0]   # delta_db 12 쪽


# ─── 순수 도우미 ─────────────────────────────────────────────────────────────
def test_parse_creation_time_variants():
    want = datetime(2026, 9, 6, 4, 5, 12, tzinfo=UTC)
    assert ir.parse_creation_time("2026-09-06T04:05:12.000000Z") == want
    assert ir.parse_creation_time("2026-09-06T13:05:12+0900") == want
    assert ir.parse_creation_time("2026-09-06 04:05:12") == want
    assert ir.parse_creation_time("1970-01-01T00:00:00Z") is None
    assert ir.parse_creation_time("") is None and ir.parse_creation_time("garbage") is None


def test_parse_probe_reads_duration_size_and_creation_time():
    info = ir.parse_probe({
        "format": {"duration": "2003.228333", "tags": {"creation_time": "2026-09-06T04:05:12Z"}},
        "streams": [{"codec_type": "audio"},
                    {"codec_type": "video", "width": 1920, "height": 1080, "bit_rate": "8017342"}],
    })
    assert info.duration == pytest.approx(2003.228333)
    assert (info.width, info.height, info.video_bps) == (1920, 1080, 8017342)
    assert info.start_utc == datetime(2026, 9, 6, 4, 5, 12, tzinfo=UTC)
    assert ir.parse_probe({"format": {"duration": "10"}}).start_utc is None


def test_apple_creationdate_used_when_standard_tag_missing():
    info = ir.parse_probe({"format": {"duration": "1", "tags": {
        "com.apple.quicktime.creationdate": "2026-09-06T13:05:12+0900"}}})
    assert info.start_utc == datetime(2026, 9, 6, 4, 5, 12, tzinfo=UTC)


def test_quality_warning_thresholds():
    assert ir.quality_warning(ir.MediaInfo(duration=1, height=1080, video_bps=8_000_000)) is None
    assert "720p" in ir.quality_warning(ir.MediaInfo(duration=1, height=720))
    assert "Mbps" in ir.quality_warning(ir.MediaInfo(duration=1, height=1080, video_bps=2_000_000))


def test_kst_date_crosses_midnight():
    assert ir.kst_date(datetime(2026, 9, 26, 16, 0, tzinfo=UTC)) == "2026-09-27"
    assert ir.kst_date(datetime(2026, 9, 26, 14, 59, tzinfo=UTC)) == "2026-09-26"


def test_band_text_lists_only_uploaded_games():
    day = {"date": "2026-09-27", "games": [
        {"label": "1경기", "yt_url": "https://youtu.be/a"},
        {"label": "2경기", "yt_url": None},
        {"label": "3경기", "yt_url": "https://youtu.be/c"}]}
    text = ir.band_text(day)
    assert text.startswith("2026년 09월 27일 경기 하이라이트 영상입니다.")
    assert "· 1경기\nhttps://youtu.be/a" in text and "2경기" not in text
    assert ir.next_label(day) == "4경기"


def test_load_secrets_and_find_recursively(tmp_path):
    nested = tmp_path / "datasets" / "me" / "hl-secrets"
    nested.mkdir(parents=True)
    good = {k: "v" for k in ir.REQUIRED_SECRETS}
    (nested / ir.SECRETS_FILENAME).write_text(json.dumps(good), encoding="utf-8")
    found = ir.find_secrets_file(tmp_path)
    assert found == nested / ir.SECRETS_FILENAME
    assert ir.load_secrets(found)["NTFY_TOPIC"] == "v"
    (nested / ir.SECRETS_FILENAME).write_text(json.dumps({**good, "NTFY_TOPIC": ""}), encoding="utf-8")
    with pytest.raises(ValueError, match="NTFY_TOPIC"):
        ir.load_secrets(found)
    assert ir.find_secrets_file(tmp_path / "nope") is None


def test_settings_from_secrets_and_cli_overrides():
    secrets = {"DEFAULT_TITLE": "우리 팀", "YOUTUBE_PRIVACY": "unlisted", "WAIT_NEXT_PART_MIN": "30",
               "SETTLE_SEC": "60", "RETENTION_DAYS": "3", "SENSITIVITY": "strict", "QUALITY": "bogus",
               "GEMINI_API_KEY": "k"}
    s = ir.settings_from(secrets, ir.parse_args([]))
    assert (s.default_title, s.youtube_privacy, s.retention_days) == ("우리 팀", "unlisted", 3)
    assert (s.wait_next_part_sec, s.settle_sec) == (1800, 60)
    assert (s.sensitivity, s.quality) == ("strict", "balanced")   # 모르는 값은 기본값으로
    s = ir.settings_from(secrets, ir.parse_args(["--settle-sec", "0", "--wait-part-min", "0",
                                                  "--min-game-sec", "60", "--once", "--no-vision"]))
    assert (s.settle_sec, s.wait_next_part_sec, s.min_game_sec, s.once, s.no_vision) == (0, 0, 60, True, True)


def test_mask_never_reveals_secret():
    assert ir.mask("AIzaSecretValue") == "AIza…" and ir.mask("") == "(없음)"


def test_prepare_youtube_token_writes_uploader_format(tmp_path, monkeypatch):
    monkeypatch.setattr(ir.yt_up, "TOKEN_FILE", ir.yt_up.TOKEN_FILE)   # 테스트 후 원래대로
    creds = SimpleNamespace(token="at", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token",
                            client_id="cid", client_secret="cs", scopes=ir.GOOGLE_SCOPES,
                            expiry=datetime(2026, 9, 27, 4, 0))
    path = ir.prepare_youtube_token(creds, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["refresh_token"] == "rt" and data["expiry"] == "2026-09-27T04:00:00"
    assert ir.yt_up.TOKEN_FILE == path


def test_ensure_title_font_downloads_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ir.sh, "TITLE_FONT", str(tmp_path / "missing.ttf"))
    monkeypatch.delenv("HL_TITLE_FONT", raising=False)
    fetched = []

    class R:
        content = b"ttf"

        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.get", lambda url, timeout: fetched.append(url) or R())
    path = ir.ensure_title_font(tmp_path)
    assert Path(path).read_bytes() == b"ttf" and ir.sh.TITLE_FONT == path
    assert fetched == [ir.NANUM_FONT_URL]
    assert ir.ensure_title_font(tmp_path) == path and len(fetched) == 1   # 이미 있으면 다시 안 받음


def test_is_auth_error():
    from google.auth.exceptions import RefreshError
    assert ir.is_auth_error(RefreshError("invalid_grant: Token has been expired or revoked."))
    assert ir.is_auth_error(RuntimeError("invalid_grant"))
    assert not ir.is_auth_error(RuntimeError("timeout"))


def test_fatal_errors_are_notified(h):
    from google.auth.exceptions import RefreshError
    ir._notify_fatal(h.notifier, RefreshError("invalid_grant"))
    ir._notify_fatal(h.notifier, RuntimeError("disk full"))
    assert [n["title"] for n in h.notes] == ["Google 인증 만료", "HLEditor 처리기 오류"]
    assert "disk full" in h.notes[1]["message"]


# ─── main(): Kaggle 경로 연결(비밀값 → 자격 증명 → Drive 저장소 → 요약) ─────────
def _kaggle_env(tmp_path, monkeypatch, sent):
    from fake_drive import FOLDER, FakeDrive, FakeSession

    from kaggle_runner.stores import DriveStore

    drive = FakeDrive()
    root = drive.add("HLEditor", "root", mime=FOLDER)
    secrets = {k: f"{k.lower()}-value" for k in ir.REQUIRED_SECRETS}
    secrets["ROOT_FOLDER_ID"] = root
    path = tmp_path / ir.SECRETS_FILENAME
    path.write_text(json.dumps(secrets), encoding="utf-8")
    creds = SimpleNamespace(token="at", refresh_token="rt", token_uri="https://oauth2.googleapis.com/token",
                            client_id="cid", client_secret="cs", scopes=ir.GOOGLE_SCOPES, expiry=None)
    monkeypatch.setattr(ir, "ensure_runtime_deps", lambda: None)
    monkeypatch.setattr(ir, "google_credentials", lambda s: creds)
    monkeypatch.setattr(ir, "DriveStore", lambda c, root_id: DriveStore(
        c, root_id, service=drive, session=FakeSession(drive)))
    monkeypatch.setattr(ir, "ensure_title_font", lambda wd: None)
    monkeypatch.setattr(ir, "gpu_available", lambda: False)
    monkeypatch.setattr(ir.yt_up, "TOKEN_FILE", ir.yt_up.TOKEN_FILE)
    monkeypatch.setattr(ir.Notifier, "send", lambda self, title, message, **kw: sent.append(title) or True)
    return drive, root, path


def test_main_kaggle_branch_wires_everything(tmp_path, monkeypatch):
    sent = []
    drive, root, path = _kaggle_env(tmp_path, monkeypatch, sent)
    assert ir.main(["--secrets", str(path), "--workdir", str(tmp_path)]) == 0
    work = tmp_path / "hl_inbox"
    summary = json.loads((work / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["games"] == [] and summary["gpu"] is False and sent == []
    assert json.loads((work / "youtube_token.json").read_text(encoding="utf-8"))["refresh_token"] == "rt"
    assert drive.folder_named("01_inbox", root)                       # 하위 폴더를 만들었다


def test_main_notifies_when_google_auth_expired(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError

    sent = []
    _, _, path = _kaggle_env(tmp_path, monkeypatch, sent)

    def expired(secrets):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(ir, "google_credentials", expired)
    with pytest.raises(RefreshError):
        ir.main(["--secrets", str(path), "--workdir", str(tmp_path)])
    assert sent == ["Google 인증 만료"]


def test_main_without_secrets_dataset_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "ensure_runtime_deps", lambda: None)
    monkeypatch.setattr(ir, "find_secrets_file", lambda root=None: None)
    assert ir.main(["--workdir", str(tmp_path)]) == 2
