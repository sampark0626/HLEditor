"""kaggle_runner.inbox_runner — 인박스 처리 흐름(로컬 저장소 + 무거운 단계는 가짜로 대체)."""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaggle_runner import inbox_runner as ir
from kaggle_runner.notify import Notifier
from kaggle_runner.stores import FOLDERS, LocalStore

T0 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)      # 2026-09-27(일) 12:00 KST 녹화 시작
CLIP_DUR = 13.02                                   # 가짜 클립의 ffprobe 길이(PRE 8 + POST 5초에 가깝게)


class Clock:
    def __init__(self, start):
        self.t = start
        self.timers = []      # (시각, 함수) — sleep으로 시간이 흐르다 그 시각이 되면 실행(업로드 도착 흉내)

    def now(self):
        return self.t

    def after(self, minutes, fn):
        self.timers.append((self.t + timedelta(minutes=minutes), fn))

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)
        due = [fn for at, fn in self.timers if at <= self.t]
        self.timers = [(at, fn) for at, fn in self.timers if at > self.t]
        for fn in due:
            fn()


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
        self.logs = []
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
                                log=self.logs.append)
        self.runner = runner
        return runner.run()

    # 가짜 단계 -----------------------------------------------------------------
    def _probe(self, path):
        if re.fullmatch(r"clip\d{3}\.mp4", Path(path).name):      # build_output이 만든 클립
            return ir.MediaInfo(duration=CLIP_DUR)
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
        merged, _ = ir.sh.get_merged_timeline(selected, ir.sh.PRE_SEC, ir.sh.POST_SEC)
        clips = []
        for i, _m in enumerate(merged):                           # 진짜 build_output처럼 클립 파일을 남긴다
            path = Path(workdir) / f"clip{i:03d}.mp4"
            path.write_bytes(f"clip{i}".encode())
            clips.append(path)
        return ir.sh.BuildResult(clips=clips, merged=merged)

    def _publish(self, output, source, cands, approved, video_name, title, **kw):
        if self.publish_error:
            raise RuntimeError(self.publish_error)
        label = video_name.rsplit("-", 1)[-1]                     # "2026-09-27-1경기" → "1경기"
        self.published.append({"label": label, "video_name": video_name, "title": title,
                               "approved": approved, "privacy": kw.get("privacy")})
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
    assert h.published[0]["title"] == "2026-09-27-1경기"        # YouTube 제목 = 경기 이름(날짜-경기번호)
    assert h.published[0]["video_name"] == "2026-09-27-1경기"   # 설명 첫 줄에 쓰이는 이름도 같다
    assert h.published[0]["approved"] == [0, 1]
    assert "merged.mp4" in h.built[0]["video"]                 # 1경기는 두 파트를 합친 영상
    assert h.built[0]["title"] == "한울타리 FC 경기영상"        # 워터마크는 그대로
    assert h.files("done") == ["2026-09-27-1경기_원본1.mp4", "2026-09-27-1경기_원본2.mp4",
                               "2026-09-27-2경기_원본.mp4"]   # 원본 이름이 경기 이름으로(나뉜 파일은 1·2)
    assert h.files("inbox") == [] and h.files("processing") == []

    assert len(h.notes) == 1                                    # 경기마다가 아니라 한 통
    note = h.notes[0]
    assert note["title"] == "하이라이트 2경기 완료 (영상 3개 처리)"
    msg = note["message"]                                       # 그날 BAND 글 전체
    assert "2026년 09월 27일" in msg
    assert "https://youtu.be/1경기" in msg and "https://youtu.be/2경기" in msg
    assert "· 2026-09-27-1경기\nhttps://youtu.be/1경기" in msg      # BAND 글 줄도 같은 이름
    assert "· 2026-09-27-2경기\nhttps://youtu.be/2경기" in msg
    assert "※ 1경기: 나뉜 파일 2개를 한 경기로 합쳤습니다" in msg
    assert "click" not in note                                  # 탭하면 복사되도록

    day = h.store.read_json("day_2026-09-27.json")
    assert [g["label"] for g in day["games"]] == ["1경기", "2경기"]
    assert day["games"][0]["sources"] == ["p1.mp4", "p2.mp4"]
    assert h.store.read_json("results_2026-09-27_1경기.json")["candidates"]
    claim = h.store.read_json("claims.json")["2026-09-27-1경기_원본1.mp4"]   # 로컬 저장소는 id가 파일명이라 키도 따라간다
    assert claim["status"] == "done" and claim["renamed_from"] == "p1.mp4"
    assert len(summary["games"]) == 2 and summary["failed"] == []


def test_label_continues_from_earlier_run_same_day(h):
    h.store.write_json("day_2026-09-27.json", {"date": "2026-09-27", "games": [
        {"label": "1경기", "yt_url": "https://youtu.be/old"}]})
    h.add("g.mp4", 1500, start_min=90)
    h.run()
    assert h.published[0]["label"] == "2경기"
    assert len(h.notes) == 1
    assert "https://youtu.be/old" in h.notes[0]["message"]      # 이전 실행의 경기도 같은 글에 들어간다


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
    assert h.notes[0]["title"] == "올라간 하이라이트 없음 · 확인 필요 1건 (영상 1개 처리)"
    assert h.notes[0]["tags"] == ["warning"]
    assert "처리 실패 — clip.mp4" in h.notes[0]["message"]
    assert "너무 짧아" in h.notes[0]["message"] and "04_failed" in h.notes[0]["message"]
    assert h.published == []


def test_no_highlights_still_takes_a_game_number(h):
    h.verdict = lambda c: {"highlight": False, "confidence": 0.2, "type": "other"}
    h.add("quiet.mp4", 1500, start_min=0)
    h.run()
    assert h.notes[0]["title"] == "올라간 하이라이트 없음 · 확인 필요 1건 (영상 1개 처리)"
    assert "※ 1경기: 하이라이트 없음" in h.notes[0]["message"]
    assert h.files("done") == ["2026-09-27-1경기_원본.mp4"] and h.published == []   # 하이라이트 없는 경기도 이름 변경
    assert h.store.read_json("day_2026-09-27.json")["games"][0]["yt_url"] is None


def test_all_judge_errors_fail_the_game_instead_of_saying_no_highlights(h):
    err = "api_error: 404 NOT_FOUND. models/x is not found"
    h.verdict = lambda c: {"highlight": False, "type": "other", "confidence": 0.0, "reason": err}
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.files("failed") == ["g.mp4"] and h.files("done") == []
    msg = h.notes[0]["message"]
    assert "처리 실패 — g.mp4" in msg and "AI 판별이 후보 2개 중 2개에서 실패" in msg and err in msg
    assert "하이라이트 없음" not in msg
    assert h.published == [] and h.store.read_json("day_2026-09-27.json") is None   # 경기 번호도 쓰지 않음
    cands = h.store.read_json("results_2026-09-27_1경기.json")["candidates"]       # 사유는 결과 JSON에 남는다
    assert {c["reason"] for c in cands} == {err}


def test_partial_judge_errors_are_skipped_with_a_warning(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    monkeypatch.setattr(ir.sh, "detect_spikes", lambda video, wd, **k: [
        {"peak": float(p), "delta_db": 10.0} for p in (100, 200, 300, 400)])
    h.verdict = lambda c: (
        {"highlight": False, "type": "other", "confidence": 0.0, "reason": "clip_error: boom"}
        if c["peak"] == 100.0 else {"highlight": True, "confidence": 0.9, "type": "shot", "reason": "슛"})
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.published[0]["approved"] == [1, 2, 3]
    assert "※ 1경기: AI 판별 오류 1개는 제외하고 처리했습니다 — clip_error: boom" in h.notes[0]["message"]


def test_judge_errors_ignored_when_vision_was_not_used(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)

    def no_vision(video, workdir, cands, gemini_key=None, log=print):
        for c in cands:
            c["reason"] = "api_error: 이전 실행의 흔적"
        return {"vision_used": False, "usage": None, "pan": None}

    monkeypatch.setattr(ir, "analyze_candidates", no_vision)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.files("failed") == [] and h.files("done") == ["2026-09-27-1경기_원본.mp4"]


def test_youtube_failure_backs_up_to_drive_and_says_how_to_fix_auth(h):
    h.publish_error = "YouTube 인증이 만료되었거나 취소되었습니다. 다시 로그인해주세요."
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    note = h.notes[0]
    assert note["title"] == "올라간 하이라이트 없음 · 확인 필요 1건 (영상 1개 처리)"
    assert "※ 1경기: YouTube 업로드 실패" in note["message"]
    assert "make_kaggle_secrets.py" in note["message"]
    assert "2026-09-27-1경기.mp4" in note["message"]             # Drive 백업 링크(click 대신 본문에)
    assert h.files("output") == ["2026-09-27-1경기.mp4"]          # 백업 이름 = 경기 이름
    assert h.files("done") == ["2026-09-27-1경기_원본.mp4"]


def test_processing_error_fails_that_game_and_continues(h):
    h.build_fail = {"bad.mp4"}
    h.add("bad.mp4", 1500, start_min=0)
    h.add("good.mp4", 1500, start_min=60)
    summary = h.run()
    assert h.files("failed") == ["bad.mp4"] and h.files("done") == ["2026-09-27-1경기_원본.mp4"]
    assert "ffmpeg 실패" in summary["failed"][0]["reason"]
    assert [p["label"] for p in h.published] == ["1경기"]      # 실패한 경기는 번호를 쓰지 않음
    assert len(h.notes) == 1                                    # 성공과 실패가 한 통에 같이 온다
    assert h.notes[0]["title"] == "하이라이트 1경기 완료 · 확인 필요 1건 (영상 2개 처리)"
    assert h.notes[0]["tags"] == ["soccer", "warning"]
    assert "https://youtu.be/1경기" in h.notes[0]["message"] and "처리 실패 — bad.mp4" in h.notes[0]["message"]


def test_quality_warning_is_appended(h):
    h.add("g.mp4", 1500, start_min=0, height=720)
    h.run()
    assert "720p" in h.notes[0]["message"] and "사진 및 동영상" in h.notes[0]["message"]


def test_head_part_waits_for_next_part_then_times_out(h):
    h.add("head.mp4", 1800, start_min=0, arrived=h.clock.now())
    h.run()
    note = h.notes[0]
    assert note["title"].startswith("하이라이트 1경기 완료")
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
    assert h.files("done") == ["2026-09-27-1경기_원본.mp4"]
    assert h.store.read_json("claims.json")["2026-09-27-1경기_원본.mp4"]["attempts"] == 1


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
    assert h.files("done") == ["2026-09-27-1경기_원본.mp4", "2026-09-27-2경기_원본.mp4"]
    assert calls == ["1경기"]                                     # 2D 요청 경기만


def test_2d_failure_only_notifies(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, gpu=True)
    import kaggle_runner.run_match as rm
    monkeypatch.setattr(rm, "install_packages", lambda pkgs: (_ for _ in ()).throw(RuntimeError("pip 실패")))
    h.add("two_d.mp4", 1500, start_min=0, folder="inbox_2d")
    h.run()
    assert [n["title"] for n in h.notes] == ["하이라이트 1경기 완료 · 확인 필요 1건 (영상 1개 처리)"]
    assert "https://youtu.be/1경기" in h.notes[0]["message"]
    assert "※ 1경기: 2D 변환 실패 — pip 실패" in h.notes[0]["message"]
    assert h.files("done") == ["2026-09-27-1경기_원본.mp4"]


def test_dry_run_keeps_output_locally_and_skips_youtube(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, dry_run=True)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.published == []
    assert h.files("output") == ["2026-09-27-1경기.mp4"]
    assert "https://youtu.be/DRYRUN-1경기" in h.notes[0]["message"]


def test_max_segments_keeps_strongest(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, max_segments=1)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.built[0]["n"] == 1 and h.published[0]["approved"] == [0]   # delta_db 12 쪽


# ─── 클립 보관·이름 규칙 (MONTHLY_PLAN.md Phase 1) ───────────────────────────
ORIGINAL_1 = "2026-09-27-1경기_원본.mp4"


def test_names_apply_even_when_clip_storage_is_off(h):
    """HL_CLIPS가 꺼져 있어도 이름 규칙은 적용되고, 클립·JSON은 아무것도 남기지 않는다."""
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.published[0]["title"] == "2026-09-27-1경기"
    assert h.files("done") == [ORIGINAL_1]
    assert h.files("clips") == []
    assert h.store.read_json("clips_2026-09-27_1경기.json") is None
    assert h.store.read_json("games_2026-09-27_1경기.json") is None


def test_clips_and_json_are_stored_when_enabled(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    h.add("g.mp4", 1500, start_min=0)
    h.run()

    names = ["2026-09-27-1경기_c001.mp4", "2026-09-27-1경기_c002.mp4"]
    assert h.files("clips") == names
    assert (h.root / "06_clips" / names[0]).read_bytes() == b"clip0"
    doc = h.store.read_json("clips_2026-09-27_1경기.json")
    assert (doc["date"], doc["label"]) == ("2026-09-27", "1경기") and doc["created_at"]
    assert doc["encode"] == {"preset": "fast", "crf": 25, "height": 1080}      # 기본 품질 balanced
    assert doc["originals"] == [{"file_id": ORIGINAL_1, "name": ORIGINAL_1}]
    c1, c2 = doc["clips"]
    assert (c1["idx"], c1["name"], c1["file_id"]) == (1, names[0], names[0])
    assert (c1["src_start"], c1["src_end"], c1["dur"], c1["end"]) == (92.0, 105.0, CLIP_DUR, None)
    assert c1["cands"] == [{"peak": 100.0, "type": "goal", "confidence": 0.9, "delta_db": 12.0, "pan": None}]
    assert (c2["idx"], c2["src_start"], c2["src_end"]) == (2, 392.0, 405.0)

    game = h.store.read_json("games_2026-09-27_1경기.json")
    assert (game["status"], game["tag_source"]) == ("auto_published", "auto")
    assert game["published_clips"] == [1, 2]                                    # 자동 게시 = 전체
    assert (game["yt_url"], game["yt_title"]) == ("https://youtu.be/1경기", "2026-09-27-1경기")
    assert (game["kind"], game["opponent"], game["attack_end"], game["switch_at_idx"]) == (None, "", None, None)
    assert game["clip_tags"] == {} and game["tagged_at"] is None and game["error"] is None
    assert game["published_at"] and game["attempts"] == 0
    assert len(h.notes) == 1 and "클립 보관" not in h.notes[0]["message"]      # 성공은 조용히
    assert not list(h.workdir.glob("game_*"))                                   # 작업 폴더 정리는 그대로


def test_each_game_gets_its_own_clip_numbers(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    h.add("g1.mp4", 1500, start_min=0)
    h.add("g2.mp4", 1500, start_min=60)
    h.run()
    assert len(h.files("clips")) == 4
    assert h.files("clips")[2] == "2026-09-27-2경기_c001.mp4"
    assert h.store.read_json("clips_2026-09-27_2경기.json")["originals"][0]["name"] == "2026-09-27-2경기_원본.mp4"


def test_clips_are_kept_even_when_youtube_upload_fails(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    h.publish_error = "quota exceeded"
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert len(h.files("clips")) == 2 and h.store.read_json("clips_2026-09-27_1경기.json")
    game = h.store.read_json("games_2026-09-27_1경기.json")
    assert (game["status"], game["published_clips"]) == ("failed", [])
    assert (game["yt_url"], game["yt_title"], game["published_at"]) == (None, None, None)
    assert game["error"] == "quota exceeded"
    assert h.files("output") == ["2026-09-27-1경기.mp4"]                        # 백업은 그대로


def test_game_without_highlights_stores_nothing(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    h.verdict = lambda c: {"highlight": False, "confidence": 0.2, "type": "other"}
    h.add("quiet.mp4", 1500, start_min=0)
    h.run()
    assert h.files("clips") == [] and h.store.read_json("clips_2026-09-27_1경기.json") is None
    assert h.store.read_json("games_2026-09-27_1경기.json") is None
    assert h.files("done") == [ORIGINAL_1]                                      # 이름은 바뀐다


def test_clip_upload_failure_is_only_a_warning_and_leaves_nothing(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    real, calls = h.store.upload_with_id, []

    def flaky(path, key, name):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("boom")
        return real(path, key, name)

    h.store.upload_with_id = flaky
    h.add("g.mp4", 1500, start_min=0)
    h.run()

    assert [p["label"] for p in h.published] == ["1경기"]                        # 게시는 이미 끝났다
    assert h.files("clips") == []                                               # 올라간 첫 클립도 치웠다(일부만 두지 않음)
    assert (h.root / ".trash" / "2026-09-27-1경기_c001.mp4").exists()
    assert h.store.read_json("clips_2026-09-27_1경기.json") is None
    assert h.store.read_json("games_2026-09-27_1경기.json") is None
    assert h.files("done") == [ORIGINAL_1]
    note = h.notes[0]
    assert note["title"] == "하이라이트 1경기 완료 (영상 1개 처리)"              # 경고일 뿐 확인 필요 건수는 아니다
    assert "※ 1경기: 클립 보관 실패 — boom" in note["message"]
    assert "https://youtu.be/1경기" in note["message"]


def test_games_json_failure_keeps_clips_and_warns(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, hl_clips=True)
    real = h.store.write_json

    def write_json(name, data):
        if name.startswith("games_"):
            raise OSError("state down")
        return real(name, data)

    h.store.write_json = write_json
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert len(h.files("clips")) == 2 and h.store.read_json("clips_2026-09-27_1경기.json")
    assert "※ 1경기: 경기 기록 저장 실패 — state down (클립은 보관됨)" in h.notes[0]["message"]


def test_auth_error_while_storing_clips_propagates_but_the_game_is_already_finished(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError
    h = Harness(tmp_path, monkeypatch, hl_clips=True)

    def dead(*args):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    h.store.upload_with_id = dead
    h.add("g.mp4", 1500, start_min=0)
    with pytest.raises(RefreshError):
        h.run()
    # 다시 처리해 YouTube에 두 번 올리지 않도록 원본은 이미 03_done에, 경기는 day 기록에 있다
    assert h.files("done") == [ORIGINAL_1] and h.files("processing") == []
    assert h.store.read_json("day_2026-09-27.json")["games"][0]["yt_url"] == "https://youtu.be/1경기"
    h.runner.flush_report()                                                     # main()이 오류 알림 전에 하는 일
    assert "https://youtu.be/1경기" in h.notes[0]["message"]


def test_original_rename_failure_is_only_a_warning(h):
    (h.root / "03_done" / ORIGINAL_1).write_bytes(b"x")                         # 같은 이름이 이미 있다
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.files("done") == [ORIGINAL_1, "g.mp4"]                             # 못 바꿔도 03_done에는 들어간다
    assert [p["label"] for p in h.published] == ["1경기"]
    assert "※ 1경기: 원본 이름을 바꾸지 못했습니다(g.mp4)" in h.notes[0]["message"]
    assert h.store.read_json("claims.json")["g.mp4"]["status"] == "done"


def test_done_renames_a_drive_original_keeping_its_id(tmp_path):
    from fake_drive import FOLDER, FakeDrive, FakeSession

    from kaggle_runner.stores import DriveStore, RemoteFile

    drive = FakeDrive()
    root = drive.add("HLEditor", "root", mime=FOLDER)
    store = DriveStore(None, root, service=drive, session=FakeSession(drive))
    fid = drive.add("IMG_0042.MOV", store.folder_id("processing"), content=b"v")
    rf = RemoteFile(id=fid, name="IMG_0042.MOV", size=1, created_utc=T0)
    runner = ir.InboxRunner(store, Notifier("t", dry_run=True, log=lambda m: None),
                            ir.Settings(gemini_key=None, default_title="t"), workdir=tmp_path,
                            now=lambda: T0, sleep=lambda s: None, log=lambda m: None)
    warnings = []
    originals = runner._done([SimpleNamespace(remote=rf)], "2026-09-27-1경기", warnings)
    assert drive.children(store.folder_id("done")) == ["2026-09-27-1경기_원본.mov"]   # 확장자는 원본 것 그대로
    assert originals == [{"file_id": fid, "name": "2026-09-27-1경기_원본.mov"}] and warnings == []
    assert runner.claims[fid]["status"] == "done" and runner.claims[fid]["name"] == "2026-09-27-1경기_원본.mov"


def test_dry_run_logs_the_youtube_title(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, dry_run=True)
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert "[1경기] (dry-run) YouTube 제목: 2026-09-27-1경기" in h.logs


def test_clip_cand_keeps_only_what_the_clips_json_needs():
    cand = {"peak": 100.123, "type": "shot", "confidence": 0.8123, "delta_db": 10.3456, "reason": "긴 설명",
            "pan": {"pos": -0.59, "speed": 1.4, "speed_max": 8.9, "state": "goal_end_dwell"}, "pan_bonus": 0.1}
    assert ir._clip_cand(cand) == {"peak": 100.12, "type": "shot", "confidence": 0.812, "delta_db": 10.35,
                                   "pan": {"pos": -0.59, "state": "goal_end_dwell"}}
    assert ir._clip_cand({"peak": 5.0}) == {"peak": 5.0, "type": None, "confidence": None,
                                            "delta_db": None, "pan": None}    # --no-vision


def test_encode_info_matches_what_build_output_uses():
    assert ir._encode_info(ir.sh.QUALITY_PRESETS["quality"]) == {"preset": "medium", "crf": 23, "height": 1080}
    assert ir._encode_info({}) == {"preset": ir.sh.ENCODE_PRESET, "crf": ir.sh.ENCODE_CRF,
                                   "height": ir.sh.MAX_HEIGHT}
    assert ir._encode_info(ir.sh.QUALITY_PRESETS["copy"])["copy"] is True


def test_hl_clips_setting_comes_from_secret_or_cli_flag():
    assert ir.settings_from({}, ir.parse_args([])).hl_clips is False            # 기본 끔
    for on in ("1", 1, "true", "TRUE", "yes", "on"):
        assert ir.settings_from({"HL_CLIPS": on}, ir.parse_args([])).hl_clips is True
    for off in ("0", "", "false", "no", None, "maybe"):
        assert ir.settings_from({"HL_CLIPS": off}, ir.parse_args([])).hl_clips is False
    assert ir.settings_from({}, ir.parse_args(["--clips"])).hl_clips is True    # 로컬 시험용 스위치


# ─── 알림은 올린 영상을 다 처리한 뒤 한 통만 ────────────────────────────────
def test_trickling_uploads_give_one_notification_at_the_end(h):
    """영상이 띄엄띄엄 올라와도(처리 중에, 인박스가 빈 뒤에) 한 실행·한 알림으로 끝난다."""
    start = h.clock.now()
    h.add("g1.mp4", 1500, start_min=0)
    h.clock.after(10, lambda: h.add("g2.mp4", 1500, start_min=60, arrived=h.clock.now()))
    h.clock.after(10 + 3 + 15, lambda: h.add("g3.mp4", 1500, start_min=120, arrived=h.clock.now()))
    h.run()

    assert [p["label"] for p in h.published] == ["1경기", "2경기", "3경기"]
    assert len(h.notes) == 1
    note = h.notes[0]
    assert note["title"] == "하이라이트 3경기 완료 (영상 3개 처리)"
    assert all(f"https://youtu.be/{n}경기" in note["message"] for n in (1, 2, 3))
    assert h.files("done") == [f"2026-09-27-{n}경기_원본.mp4" for n in (1, 2, 3)]
    assert h.files("inbox") == []
    # 마지막 경기를 끝낸 뒤 LINGER만큼 더 기다렸다가 알린다
    assert h.clock.now() - start >= timedelta(minutes=10 + 3 + 15) + timedelta(seconds=ir.LINGER_SEC)


def test_linger_waits_after_the_last_game_before_notifying(h):
    start = h.clock.now()
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.clock.now() - start >= timedelta(seconds=ir.LINGER_SEC)
    assert len(h.notes) == 1


def test_upload_after_the_linger_window_is_a_separate_run(h):
    h.add("g1.mp4", 1500, start_min=0)
    h.run()
    h.add("g2.mp4", 1500, start_min=60, arrived=h.clock.now() - timedelta(minutes=5))
    h.run()
    assert [n["title"] for n in h.notes] == ["하이라이트 1경기 완료 (영상 1개 처리)",
                                             "하이라이트 1경기 완료 (영상 1개 처리)"]
    assert "https://youtu.be/1경기" in h.notes[1]["message"]    # 둘째 알림에도 그날 글 전체
    assert "https://youtu.be/2경기" in h.notes[1]["message"]


def test_zero_linger_ends_right_after_the_last_game(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, linger_sec=0)
    start = h.clock.now()
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.clock.now() == start and len(h.notes) == 1


def test_empty_inbox_ends_immediately_without_notification(h):
    start = h.clock.now()
    summary = h.run()
    assert h.notes == [] and h.clock.now() == start and summary["games"] == []


def test_report_survives_unreadable_day_file(h):
    runner = ir.InboxRunner(h.store, h.notifier, h.settings, workdir=h.workdir,
                            now=h.clock.now, sleep=h.clock.sleep, log=lambda m: None)

    def drive_down(name):
        raise RuntimeError("drive down")

    h.store.read_json = drive_down
    runner._record("game", date="2026-09-27", label="1경기", files=1,
                   yt_url="https://youtu.be/x", warnings=[])
    runner.flush_report()
    assert h.notes[0]["title"] == "하이라이트 1경기 완료 (영상 1개 처리)"
    assert "https://youtu.be/x" in h.notes[0]["message"]
    runner.flush_report()                                     # 보낸 뒤에는 비어 있어 다시 보내지 않는다
    assert len(h.notes) == 1


def test_build_report_orders_band_post_before_notes():
    days = {"2026-09-27": {"date": "2026-09-27", "games": [
        {"label": "1경기", "yt_url": "https://youtu.be/a"}, {"label": "2경기", "yt_url": None}]}}
    events = [
        {"kind": "game", "date": "2026-09-27", "label": "1경기", "files": 2, "yt_url": "https://youtu.be/a",
         "warnings": ["나뉜 파일 2개를 한 경기로 합쳤습니다."]},
        {"kind": "none", "date": "2026-09-27", "label": "2경기", "files": 1, "warnings": [],
         "cands": 3, "maybe": 1},
        {"kind": "fail", "names": "x.mp4", "files": 1, "reason": "영상이 짧습니다"},
    ]
    title, message, tags = ir.build_report(events, days)
    assert title == "하이라이트 1경기 완료 · 확인 필요 2건 (영상 4개 처리)"
    assert tags == ["soccer", "warning"]
    assert message.startswith("2026년 09월 27일 경기 하이라이트 영상입니다.")
    assert message.index("https://youtu.be/a") < message.index("※ 1경기: 나뉜 파일")
    assert "※ 2경기: 하이라이트 없음" in message and "※ 처리 실패 — x.mp4" in message


def test_main_reports_finished_games_before_the_fatal_error(tmp_path, monkeypatch):
    sent = []
    _, _, path = _kaggle_env(tmp_path, monkeypatch, sent)

    def die_after_one_game(self):
        self._record("game", date="2026-09-27", label="1경기", files=1,
                     yt_url="https://youtu.be/a", warnings=[])
        raise RuntimeError("disk full")

    monkeypatch.setattr(ir.InboxRunner, "run", die_after_one_game)
    with pytest.raises(RuntimeError, match="disk full"):
        ir.main(["--secrets", str(path), "--workdir", str(tmp_path)])
    assert sent == ["하이라이트 1경기 완료 (영상 1개 처리)", "HLEditor 처리기 오류"]


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
    assert "· 2026-09-27-1경기\nhttps://youtu.be/a" in text and "2026-09-27-2경기" not in text
    assert "· 2026-09-27-3경기\nhttps://youtu.be/c" in text
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
               "LINGER_MIN": "5", "GEMINI_API_KEY": "k"}
    s = ir.settings_from(secrets, ir.parse_args([]))
    assert (s.default_title, s.youtube_privacy, s.retention_days) == ("우리 팀", "unlisted", 3)
    assert (s.wait_next_part_sec, s.settle_sec, s.linger_sec) == (1800, 60, 300)
    assert (s.sensitivity, s.quality) == ("strict", "balanced")   # 모르는 값은 기본값으로
    s = ir.settings_from(secrets, ir.parse_args(["--settle-sec", "0", "--wait-part-min", "0",
                                                  "--min-game-sec", "60", "--once", "--no-vision",
                                                  "--linger-min", "2"]))
    assert (s.settle_sec, s.wait_next_part_sec, s.min_game_sec, s.once, s.no_vision) == (0, 0, 60, True, True)
    assert s.linger_sec == 120
    assert ir.settings_from({}, ir.parse_args([])).linger_sec == ir.LINGER_SEC          # 기본 20분
    assert ir.settings_from({}, ir.parse_args(["--local-inbox", "x"])).linger_sec == 0  # PC 시험은 안 기다림
    assert ir.settings_from({}, ir.parse_args(["--local-inbox", "x", "--linger-min", "1"])).linger_sec == 60


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


def test_credit_error_fails_the_game_with_a_top_up_hint_even_if_only_one(h):
    """선불 크레딧 소진(402)은 후보 하나만이어도 실패로 보낸다 — 남은 후보는 판별하지 않았기 때문이다."""
    billing = ir.sh.BILLING_ERROR_PREFIX + " 402 RESOURCE_EXHAUSTED. Your prepayment credits are depleted"
    h.verdict = lambda c: (
        {"highlight": False, "type": "other", "confidence": 0.0, "reason": billing}
        if c["peak"] == 100.0 else {"highlight": True, "confidence": 0.9, "type": "goal", "reason": "골"})
    h.add("g.mp4", 1500, start_min=0)
    h.run()
    assert h.files("failed") == ["g.mp4"] and h.files("done") == []
    msg = h.notes[0]["message"]
    assert "처리 실패 — g.mp4" in msg and ir.GEMINI_CREDIT_HELP in msg and "01_inbox로 옮기면" in msg
    assert h.published == [] and h.store.read_json("day_2026-09-27.json") is None


def test_upload_folder_is_created_and_left_alone(h):
    """00_upload는 만들어 두기만 한다 — 아직 올라가는 중인 영상을 처리하면 안 된다."""
    assert (h.root / "00_upload").is_dir()
    h.add("uploading.mp4", 1500, start_min=0, folder="upload")
    h.run()
    assert h.files("upload") == ["uploading.mp4"]
    assert h.files("done") == [] and h.files("failed") == [] and h.published == []
