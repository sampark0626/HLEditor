"""kaggle_runner.monthly — 월간 하이라이트 장면 고르기·조립·상태 전이 (MONTHLY_PLAN.md 3-6).

장면 고르기는 순수 함수라 ffmpeg 없이 시험하고, 조립은 가짜 ffmpeg(명령을 기록하고 파일만 만든다)·가짜 YouTube·
가짜 Drive 위에서 순서·챕터·Drive 우선 업로드·실패 처리를 확인한다.
"""

import json
import re
from pathlib import Path

import pytest
from test_inbox_runner import Harness

from kaggle_runner import inbox_runner as ir
from kaggle_runner import monthly as mo
from kaggle_runner import naming

CLIP_DUR = 13.02
M = "2026-12"


# ─── 장면 고르기(순수 함수) ──────────────────────────────────────────────────
def make_clip(idx, *, delta=10.0, conf=0.9, pan=None, end=None, date="2026-12-06", label="1경기", dur=CLIP_DUR):
    name = naming.clip_name(date, label, idx)
    return {"idx": idx, "name": name, "file_id": name, "src_start": 100.0 * idx, "src_end": 100.0 * idx + 13,
            "dur": dur, "end": end,
            "cands": [{"peak": 100.0 * idx + 8, "type": "goal", "confidence": conf, "delta_db": delta,
                       "pan": {"pos": 0.0, "state": pan} if pan else None}]}


def make_game(date="2026-12-06", label="1경기", kind="scrimmage", opponent="", **extra):
    return {"date": date, "label": label, "kind": kind, "opponent": opponent, "attack_end": None,
            "switch_at_idx": None, "clip_tags": {}, "status": "published", **extra}


def scene(date, label, idx, score, delta=10.0, dur=CLIP_DUR):
    return mo.Scene(date=date, label=label, idx=idx, clip={"idx": idx, "dur": dur}, game={}, delta=delta, score=score)


def test_score_is_normalised_delta_plus_confidence_plus_pan_boost():
    clips = [make_clip(1, delta=10.0, conf=0.5), make_clip(2, delta=20.0, conf=0.6, pan="goal_end_dwell"),
             make_clip(3, delta=30.0, conf=0.9, pan="moving")]
    pool, missing = mo.collect_pool([(make_game(), {"clips": clips})])
    by = {s.idx: s for s in pool}
    assert missing == {"expired": 0, "lost": 0}
    assert by[1].score == pytest.approx(0.0 + 0.5)
    assert by[2].score == pytest.approx(0.5 + 0.6 + 0.1)                   # 골대 앞 정지 가산
    assert by[3].score == pytest.approx(1.0 + 0.9)                          # 'moving'은 가산 없음
    assert [by[i].delta for i in (1, 2, 3)] == [10.0, 20.0, 30.0]


def test_normalisation_ignores_removed_clips_and_single_clips_get_half():
    clips = [make_clip(1, delta=10.0, conf=0), make_clip(2, delta=99.0, conf=0), make_clip(3, delta=20.0, conf=0)]
    game = make_game(clip_tags={"2": {"removed": True}})
    by = {s.idx: s for s in mo.collect_pool([(game, {"clips": clips})])[0]}
    assert set(by) == {1, 3} and by[1].score == 0.0 and by[3].score == 1.0   # 잘못 잡힘의 99dB는 범위에 안 든다
    only = mo.collect_pool([(make_game(), {"clips": [make_clip(1, conf=0)]})])[0]
    assert only[0].score == 0.5


def test_missing_values_count_as_zero():
    bare = {"idx": 1, "name": "n", "file_id": "f", "dur": 13, "cands": [{"peak": 1.0, "type": None}]}
    [s] = mo.collect_pool([(make_game(), {"clips": [bare]})])[0]
    assert s.delta == 0.0 and s.score == 0.5                                # 하나뿐이라 0.5, 신뢰도 없음 = 0
    empty = {"idx": 2, "name": "n2", "file_id": "f2", "dur": 13, "cands": []}
    assert mo.collect_pool([(make_game(), {"clips": [empty]})])[0][0].delta == 0.0


def test_pool_only_has_monthly_eligible_clips_of_tagged_games():
    opp = make_game(kind="opponent", attack_end="building", clip_tags={"2": {"conceded": True}, "3": {"removed": True}})
    clips = [make_clip(1, end="building"), make_clip(2, end="tree"), make_clip(3, end="building"),
             make_clip(4, end="tree"), make_clip(5)]
    untagged = make_game(date="2026-12-13", kind=None)
    pool, _ = mo.collect_pool([(opp, {"clips": clips}), (untagged, {"clips": [make_clip(1, date="2026-12-13")]})])
    assert [(s.date, s.idx) for s in pool] == [("2026-12-06", 1), ("2026-12-06", 4)]   # 공격 1·실점 아닌 수비 4만


def test_pool_skips_clips_that_are_no_longer_in_drive_and_tells_expired_from_lost():
    clips = [make_clip(1), make_clip(2), make_clip(3)]
    pool, missing = mo.collect_pool([(make_game(), {"clips": clips})], available={clips[0]["file_id"]})
    assert [s.idx for s in pool] == [1] and missing == {"expired": 0, "lost": 2}       # 정리됐다는 표시가 없으면 "못 찾음"
    pool, missing = mo.collect_pool([(make_game(), {"clips": clips, "expired": True})], available=set())
    assert pool == [] and missing == {"expired": 3, "lost": 0}                          # 보관 기간이 지나 정리된 경기
    other = make_game(date="2026-12-13")
    pool, missing = mo.collect_pool([(make_game(), {"clips": clips, "expired": True}),
                                     (other, {"clips": [make_clip(1, date="2026-12-13")]})], available=set())
    assert missing == {"expired": 3, "lost": 1}                                         # 경기마다 따로 센다


def test_describe_excluded_names_games_that_could_not_contribute():
    untagged = make_game(date="2026-12-13", kind=None)
    no_sides = make_game(date="2026-12-20", kind="opponent", opponent="XX FC")           # 공격/수비를 하나도 안 정했다
    all_removed = make_game(date="2026-12-27", kind="scrimmage", clip_tags={"1": {"removed": True}})
    fine = make_game(date="2026-12-06", kind="scrimmage")
    entries = [(untagged, {"clips": [make_clip(1, date="2026-12-13")]}),
               (no_sides, {"clips": [make_clip(1, date="2026-12-20")]}),
               (all_removed, {"clips": [make_clip(1, date="2026-12-27")]}),
               (fine, {"clips": [make_clip(1)]})]
    out = mo.describe_excluded(entries)
    assert out[0] == "2026-12-13-1경기: 경기 종류(청백전 / 다른 팀)를 태깅하지 않아 월간에서 빠졌습니다."
    assert out[1].startswith("2026-12-20-1경기-XX FC: 월간에 넣을 장면이 없어 빠졌습니다")
    assert out[2].startswith("2026-12-27-1경기-청백전: 월간에 넣을 장면이 없어 빠졌습니다")
    assert len(out) == 3                                                                 # 장면이 든 경기는 안 나온다


def test_describe_not_picked_lists_games_that_lost_the_score_competition():
    day = [scene("2026-12-06", f"{g}경기", i, 100 - g * 10 - i) for g in (1, 2, 3) for i in range(1, 5)]
    for s in day:
        s.game = make_game(date=s.date, label=s.label)
    picked = mo.pick_scenes(day, 6 * 60)                                                 # 경기일당 8개 — 3경기는 한 장면도 못 든다
    assert mo.describe_not_picked(day, picked) == [
        "2026-12-06-3경기-청백전: 다른 장면의 점수가 더 높아 이번 월간에는 들어가지 못했습니다."]
    assert mo.describe_not_picked(day, day) == [] and mo.describe_not_picked([], []) == []


def test_pick_takes_highest_scores_until_the_target_and_returns_chronological_order():
    pool = [scene("2026-12-13", "1경기", 1, 1.0), scene("2026-12-06", "1경기", 2, 3.0),
            scene("2026-12-06", "2경기", 1, 2.0), scene("2026-12-20", "1경기", 1, 0.5)]
    picked = mo.pick_scenes(pool, target_sec=30.0)                          # 13.02×3 = 39.06 ≥ 30 — 넷째는 안 넣는다
    assert [(s.date, s.label, s.idx) for s in picked] == [("2026-12-06", "1경기", 2), ("2026-12-06", "2경기", 1),
                                                          ("2026-12-13", "1경기", 1)]
    assert mo.pick_scenes(pool, target_sec=0) == []                         # 목표가 0이면 고를 필요가 없다
    assert len(mo.pick_scenes(pool, target_sec=6 * 60)) == 4                # 재료가 모자라면 있는 만큼(6분 미만)
    assert mo.pick_scenes([], 360) == []


def test_pick_limits_scenes_per_game_and_per_game_day():
    many = [scene("2026-12-06", "1경기", i, 10 - i) for i in range(1, 8)]                # 한 경기 7개
    assert [s.idx for s in mo.pick_scenes(many, 6 * 60)] == [1, 2, 3, 4]                 # 경기당 4개
    day = [scene("2026-12-06", f"{g}경기", i, 100 - g * 10 - i) for g in (1, 2, 3) for i in range(1, 5)]
    picked = mo.pick_scenes(day, 6 * 60)                                                 # 1경기 점수가 가장 높고 3경기가 가장 낮다
    assert len(picked) == 8 and not any(s.label == "3경기" for s in picked)             # 경기일당 8개(1·2경기 4개씩)
    other_day = day + [scene("2026-12-13", "1경기", 1, 1.0)]
    assert len(mo.pick_scenes(other_day, 6 * 60)) == 9                                   # 다른 경기일은 따로 센다
    assert [s.idx for s in mo.pick_scenes(many, 6 * 60, per_game=2)] == [1, 2]


def test_pick_is_deterministic_with_ties_going_to_the_earlier_scene():
    pool = [scene("2026-12-13", "1경기", 1, 1.0), scene("2026-12-06", "1경기", 3, 1.0),
            scene("2026-12-06", "1경기", 2, 1.0)]
    first = mo.pick_scenes(pool, target_sec=13.0)                           # 한 장면이면 충분하다
    assert [(s.date, s.idx) for s in first] == [("2026-12-06", 2)]
    assert mo.pick_scenes(list(reversed(pool)), 13.0) == first              # 입력 순서와 무관


def test_shout_number_one_is_the_loudest_picked_scene():
    picked = [scene("2026-12-06", "1경기", 1, 5.0, delta=12.0), scene("2026-12-06", "1경기", 2, 1.0, delta=25.0),
              scene("2026-12-13", "1경기", 1, 9.0, delta=25.0)]
    assert mo.shout_scene(picked).order == ("2026-12-13", 1, 1)             # 함성(25dB)이 같으면 점수 높은 쪽
    assert mo.shout_scene(picked[:2]).order == ("2026-12-06", 1, 2)
    assert mo.shout_scene(picked[:1]).idx == 1


def test_game_cards_description_and_report_texts():
    assert mo.game_card_lines("2026-12-06", "1경기", "opponent", " OO FC ") == ["12월 6일 · 1경기", "vs OO FC"]
    assert mo.game_card_lines("2026-12-06", "2경기", "scrimmage", "OO FC") == ["12월 6일 · 2경기", "청백전"]
    assert mo.game_card_lines("2026-12-06", "2경기", "opponent", "") == ["12월 6일 · 2경기"]
    text = mo.month_description(M, [(0.0, "12월 6일 · 1경기 vs OO FC"), (83.5, mo.SHOUT_TITLE)])
    lines = text.splitlines()
    assert lines[0] == "한울타리 FC 2026년 12월 하이라이트"
    assert lines[2:4] == ["0:00 12월 6일 · 1경기 vs OO FC", "1:23 이 달의 함성 1위"] and lines[-1].startswith("이 영상은 HLEditor")

    ok = {"kind": "monthly", "month": M, "yt_url": "https://youtu.be/a", "games": ["12월 6일 · 1경기 vs OO FC"], "warnings": []}
    title, msg, tags = mo.build_monthly_report(ok)
    assert title == "2026년 12월 월간 하이라이트 완료" and tags == ["soccer"]
    assert msg.startswith("2026년 12월 하이라이트입니다.\n\nhttps://youtu.be/a") and "· 12월 6일 · 1경기 vs OO FC" in msg
    title, msg, tags = mo.build_monthly_report({**ok, "yt_url": None, "yt_error": "quota", "drive_link": "https://d/1",
                                                "warnings": ["클립 1개 뺌"]})
    assert title.endswith("완료 · YouTube 실패") and tags == ["soccer", "warning"]
    assert "※ YouTube 업로드 실패 — quota" in msg and "https://d/1" in msg and "※ 클립 1개 뺌" in msg
    title, msg, tags = mo.build_monthly_report({"kind": "monthly_fail", "month": M, "reason": "장면 없음"})
    assert title == "2026년 12월 월간 하이라이트를 만들지 못했습니다" and "※ 장면 없음" in msg and "월간 다시 만들기" in msg


def test_concat_line_escapes_quotes_and_uses_forward_slashes():
    assert mo.concat_line(Path("a") / "b.mp4") == "file 'a/b.mp4'\n"
    assert mo.concat_line("x/it's.mp4") == "file 'x/it'\\''s.mp4'\n"


def test_probe_video_reads_size_and_fps_and_makes_the_size_even(monkeypatch):
    out = {"streams": [{"width": 1921, "height": 1081, "r_frame_rate": "30000/1001"}]}
    monkeypatch.setattr(mo.sh, "run", lambda cmd, cwd=None: json.dumps(out))
    assert mo.probe_video("x.mp4") == (1920, 1080, "30000/1001")
    out["streams"][0]["r_frame_rate"] = "0/0"
    assert mo.probe_video("x.mp4")[2] == "30/1"


# ─── 조립: 가짜 ffmpeg · YouTube ─────────────────────────────────────────────
class FF:
    """sh.run을 가짜로 — 명령을 기록하고, 출력 파일만 만든다. 카드는 글(textfile)과 길이를 기억한다."""

    def __init__(self, monkeypatch):
        self.calls, self.dur, self.cards = [], {}, []
        monkeypatch.setattr(mo.sh, "run", self.run)

    def run(self, cmd, cwd=None):
        self.calls.append((list(cmd), cwd))
        if cmd[0] == "ffprobe":
            if "format=duration" in cmd:
                return f"{self.dur.get(cmd[-1], CLIP_DUR)}\n"
            return json.dumps({"streams": [{"width": 1920, "height": 1080, "r_frame_rate": "30000/1001"}]})
        out = Path(cmd[-3])
        out.parent.mkdir(parents=True, exist_ok=True)
        if "concat" in cmd:                                         # 이어 붙인 순서가 바이트에 남는다
            names = re.findall(r"file '(.*)'", Path(cmd[cmd.index("-i") + 1]).read_text(encoding="utf-8"))
            out.write_bytes(b"|".join(Path(n).read_bytes() for n in names))
        elif "lavfi" in cmd:                                        # 카드
            sec = float(cmd[cmd.index("-t") + 1])
            files = re.findall(r"textfile=([^:]+):", cmd[cmd.index("-vf") + 1])     # 줄마다 drawtext 하나
            self.cards.append([Path(cwd, f).read_text(encoding="utf-8") for f in files] if files else None)
            self.dur[str(out)] = sec
            out.write_bytes(b"card")
        else:                                                       # 클립 인코딩
            self.dur[str(out)] = CLIP_DUR
            out.write_bytes(b"enc:" + Path(cmd[cmd.index("-i") + 1]).read_bytes())
        return ""

    def of(self, kind):
        return [c for c, _ in self.calls if c[0] == "ffmpeg" and (kind == "card") == ("lavfi" in c)
                and "concat" not in c]


class YT:
    def __init__(self, monkeypatch):
        self.uploads, self.thumbs, self.error = [], [], None
        monkeypatch.setattr(mo.yt_up, "upload_video", self.upload)
        monkeypatch.setattr(mo.yt_up, "extract_thumbnail", self.thumb)

    def upload(self, path, title, description, thumbnail_path=None, privacy="public", on_progress=None):
        if self.error:
            raise RuntimeError(self.error)
        self.uploads.append({"title": title, "description": description, "thumb": thumbnail_path,
                             "privacy": privacy, "bytes": Path(path).read_bytes()})
        return f"https://youtu.be/MONTH{len(self.uploads)}"

    def thumb(self, video_path, timestamp, out_path):
        self.thumbs.append({"clip": Path(video_path).name, "ts": timestamp})
        Path(out_path).write_bytes(b"jpg")
        return True


@pytest.fixture
def ff(monkeypatch):
    return FF(monkeypatch)


@pytest.fixture
def yt(monkeypatch):
    return YT(monkeypatch)


def seed_game(h, date, label, kind, ends, *, opponent="", attack_end=None, tags=None, deltas=None, status="published"):
    n = len(ends)
    deltas = deltas or [10.0 + i for i in range(n)]
    clips = [make_clip(i + 1, delta=deltas[i], end=ends[i], date=date, label=label) for i in range(n)]
    h.store.write_json(f"games_{date}_{label}.json", make_game(date, label, kind, opponent, attack_end=attack_end,
                                                              clip_tags=tags or {}, status=status))
    h.store.write_json(f"clips_{date}_{label}.json", {"date": date, "label": label,
                                                     "created_at": "2026-12-20T00:00:00+00:00", "clips": clips})
    day = h.store.read_json(f"day_{date}.json") or {"date": date, "games": []}
    day["games"].append({"label": label, "yt_url": "https://youtu.be/w"})
    h.store.write_json(f"day_{date}.json", day)
    for c in clips:
        (h.root / "06_clips" / c["name"]).write_bytes(c["name"].encode())
    return clips


def seed_month(h, status="build", attempts=0):
    """A(12/6 1경기, 다른 팀) + B(12/6 2경기, 청백전) + C(12/13, 태깅 안 됨) + D(12/20, 다른 팀)."""
    seed_game(h, "2026-12-06", "1경기", "opponent", ["building", "tree", "building", "tree", "building"],
              opponent="OO FC", attack_end="building", deltas=[10, 14, 20, 8, 30],
              tags={"2": {"conceded": True}, "3": {"removed": True}})        # 대상: 1(공격)·4(수비)·5(공격)
    seed_game(h, "2026-12-06", "2경기", "scrimmage", [None, None, None])
    seed_game(h, "2026-12-13", "1경기", None, ["building", "tree"])           # 태깅 안 됨 — 월간 대상 없음
    seed_game(h, "2026-12-20", "1경기", "opponent", ["tree", "building"], opponent="XX FC", attack_end="tree")
    h.store.write_json(f"monthly_{M}.json", {"month": M, "status": status, "picked": [], "yt_url": None,
                                             "drive_file_id": None, "drive_link": None, "done_at": None,
                                             "attempts": attempts, "error": None})
    h.store.write_json("requests.json", {"items": [{"id": "r1", "type": "monthly", "month": M}]})


def doc(h, month=M):
    return h.store.read_json(f"monthly_{month}.json")


A1, A4, A5 = (naming.clip_name("2026-12-06", "1경기", i) for i in (1, 4, 5))
B1, B2, B3 = (naming.clip_name("2026-12-06", "2경기", i) for i in (1, 2, 3))
D1, D2 = (naming.clip_name("2026-12-20", "1경기", i) for i in (1, 2))


def test_monthly_build_orders_cards_and_clips_and_publishes(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch, monthly_crf=18, youtube_privacy="unlisted")
    seed_month(h)
    h.run()

    d = doc(h)
    assert d["status"] == "done" and d["attempts"] == 1 and d["error"] is None
    assert [(p["date"], p["label"], p["idx"]) for p in d["picked"]] == [
        ("2026-12-06", "1경기", 1), ("2026-12-06", "1경기", 4), ("2026-12-06", "1경기", 5),
        ("2026-12-06", "2경기", 1), ("2026-12-06", "2경기", 2), ("2026-12-06", "2경기", 3),
        ("2026-12-20", "1경기", 1), ("2026-12-20", "1경기", 2)]            # 날짜순 — 태깅 안 된 12/13 경기는 없다
    assert d["yt_url"] == "https://youtu.be/MONTH1" and d["done_at"] and d["drive_file_id"] == f"{M}-월간.mp4"

    # 시작 카드 → 경기마다 [경기일 카드 + 클립] → 함성 1위 카드 + 그 장면 한 번 더(A5가 delta 30으로 가장 큼)
    expected = [b"card", b"card", f"enc:{A1}".encode(), f"enc:{A4}".encode(), f"enc:{A5}".encode(), b"card",
                f"enc:{B1}".encode(), f"enc:{B2}".encode(), f"enc:{B3}".encode(), b"card",
                f"enc:{D1}".encode(), f"enc:{D2}".encode(), b"card", f"enc:{A5}".encode()]
    assert yt.uploads[0]["bytes"] == b"|".join(expected)
    assert ff.cards == [["한울타리 FC", "2026년 12월 하이라이트"], ["12월 6일 · 1경기", "vs OO FC"],
                        ["12월 6일 · 2경기", "청백전"], ["12월 20일 · 1경기", "vs XX FC"], ["이 달의 함성 1위"]]
    assert (h.root / "07_monthly" / f"{M}-월간.mp4").read_bytes() == yt.uploads[0]["bytes"]

    up = yt.uploads[0]
    assert up["title"] == "2026-12-월간 하이라이트" and up["privacy"] == "unlisted"
    start, card = mo.START_CARD_SEC, mo.GAME_CARD_SEC
    t_b = start + card + 3 * CLIP_DUR
    t_d = t_b + card + 3 * CLIP_DUR
    t_shout = t_d + card + 2 * CLIP_DUR
    assert up["description"].splitlines()[2:6] == [
        "0:00 12월 6일 · 1경기 vs OO FC", f"{mo.mmss(t_b)} 12월 6일 · 2경기 청백전",
        f"{mo.mmss(t_d)} 12월 20일 · 1경기 vs XX FC", f"{mo.mmss(t_shout)} 이 달의 함성 1위"]
    assert d["duration_sec"] == pytest.approx(t_shout + card + CLIP_DUR, abs=0.1)
    assert d["chapters"] == [[0.0, "12월 6일 · 1경기 vs OO FC"], [round(t_b, 1), "12월 6일 · 2경기 청백전"],
                             [round(t_d, 1), "12월 20일 · 1경기 vs XX FC"], [round(t_shout, 1), "이 달의 함성 1위"]]
    assert yt.thumbs == [{"clip": A5, "ts": pytest.approx(8.0)}] and up["thumb"]    # 함성 1위 장면의 peak 프레임


def test_every_segment_is_reencoded_to_the_same_spec_and_cards_are_silent(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch, monthly_crf=18)
    seed_month(h)
    h.run()
    for cmd in ff.of("clip"):
        vf = cmd[cmd.index("-vf") + 1]
        assert "scale=1920:1080" in vf and "fps=30000/1001" in vf and "pad=1920:1080" in vf and "format=yuv420p" in vf
        assert cmd[cmd.index("-crf") + 1] == "18" and cmd[cmd.index("-preset") + 1] == "medium"
        assert cmd[cmd.index("-c:v") + 1] == "libx264" and cmd[cmd.index("-ar") + 1] == "48000"
    assert len(ff.of("clip")) == 8                                         # 함성 1위 반복은 다시 인코딩하지 않는다
    for cmd in ff.of("card"):
        assert "expansion=none" in cmd[cmd.index("-vf") + 1]               # 글 안의 %{...}를 확장하지 않는다
        assert any(a.startswith("anullsrc=") for a in cmd)                 # 소리 없는 카드
        assert "color=c=0x14202b:s=1920x1080:r=30000/1001" in " ".join(cmd) and cmd[cmd.index("-crf") + 1] == "18"
    concat = next(c for c, _ in ff.calls if "concat" in c)
    assert concat[concat.index("-c") + 1] == "copy"                        # 같은 규격으로 만들었으니 복사로 잇는다


def test_opponent_names_with_percent_signs_reach_the_card_text_untouched(tmp_path, monkeypatch, ff, yt):
    """drawtext는 textfile 안의 `%{...}`도 확장한다 — expansion=none이 없으면 상대 이름이 깨지거나 카드 인코딩이 실패한다."""
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    game = h.store.read_json("games_2026-12-20_1경기.json")
    game["opponent"] = "50%{localtime} FC"
    h.store.write_json("games_2026-12-20_1경기.json", game)
    h.run()
    assert doc(h)["status"] == "done"
    assert ["12월 20일 · 1경기", "vs 50%{localtime} FC"] in ff.cards                  # 글자 그대로 카드 파일에 들어갔다
    assert all("expansion=none" in c[c.index("-vf") + 1] for c in ff.of("card"))


def test_status_changed_while_building_does_not_overwrite_or_notify(tmp_path, monkeypatch, ff, yt):
    """만드는 동안 사용자가 "다시 만들기"를 눌러 build로 돌렸다 — 완료로 덮어쓰지도, 완료 알림을 보내지도 않는다."""
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    real = yt.upload

    def upload_then_state_changes(*args, **kw):
        url = real(*args, **kw)
        d = doc(h)
        d.update(status="build", attempts=0)                                  # Apps Script가 다시 만들기를 눌렀다
        h.store.write_json(f"monthly_{M}.json", d)
        return url

    monkeypatch.setattr(mo.yt_up, "upload_video", upload_then_state_changes)
    h.run()
    assert doc(h)["status"] == "build" and doc(h)["done_at"] is None          # 덮어쓰지 않았다
    assert h.notes == []                                                      # 완료 알림도 없다
    assert any("상태가 만드는 동안 바뀌어 완료를 기록·알리지 않았습니다" in m and "https://youtu.be/MONTH1" in m
               for m in h.logs)                                               # 올라간 파일은 로그에 남는다
    assert not list(h.workdir.glob("monthly_*"))


def test_dry_run_keeps_the_file_and_skips_youtube(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch, dry_run=True)
    seed_month(h)
    h.run()
    assert yt.uploads == [] and doc(h)["yt_url"] == f"{mo.DRYRUN_PREFIX}{M}"
    assert (h.root / "07_monthly" / f"{M}-월간.mp4").exists()
    assert f"(dry-run) YouTube 제목: {M}-월간 하이라이트" in h.logs


def test_notification_is_a_copyable_post_without_click(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    h.run()
    [note] = h.notes
    assert note["title"] == "2026년 12월 월간 하이라이트 완료" and "click" not in note
    assert note["message"].startswith("2026년 12월 하이라이트입니다.\n\nhttps://youtu.be/MONTH1")
    for line in ("· 12월 6일 · 1경기 vs OO FC", "· 12월 6일 · 2경기 청백전", "· 12월 20일 · 1경기 vs XX FC"):
        assert line in note["message"]
    post = note["message"].split("\n\n※")[0]                                 # 복사해 붙일 BAND 글 — ※ 줄은 그 뒤
    assert "· 12월 13일" not in post and post.rstrip().endswith("HLEditor 자동 생성")
    # 월간에 못 들어간 경기는 이름과 이유가 ※ 줄로 나온다(태깅 안 된 12/13 경기)
    assert ("※ 2026-12-13-1경기: 경기 종류(청백전 / 다른 팀)를 태깅하지 않아 월간에서 빠졌습니다."
            in note["message"])


def test_tagged_game_without_any_usable_scene_is_named_in_the_notification(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    seed_game(h, "2026-12-27", "1경기", "opponent", [None, None], opponent="YY FC")   # 다른 팀인데 공격/수비 미지정
    h.run()
    msg = h.notes[0]["message"]
    assert "※ 2026-12-27-1경기-YY FC: 월간에 넣을 장면이 없어 빠졌습니다" in msg
    assert "12월 27일" not in msg.split("※")[0]                                       # 본문 경기 목록에는 없다
    assert doc(h)["status"] == "done"


def test_empty_month_failure_lists_why_each_game_was_left_out(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-12-06", "1경기", None, ["building"])
    seed_game(h, "2026-12-13", "1경기", "opponent", [None], opponent="XX FC")
    h.store.write_json(f"monthly_{M}.json", {"status": "build", "attempts": 0})
    h.store.write_json("requests.json", {"items": [{"type": "monthly", "month": M}]})
    h.run()
    assert doc(h)["status"] == "failed"
    msg = h.notes[0]["message"]
    assert "2026-12-06-1경기: 경기 종류" in msg and "2026-12-13-1경기-XX FC: 월간에 넣을 장면이 없어" in msg


def test_target_length_limits_how_many_scenes_go_in(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch, monthly_target_min=0.5)             # 30초 — 장면 3개(39초)면 충분
    seed_month(h)
    h.run()
    assert len(doc(h)["picked"]) == 3


def test_drive_upload_comes_before_youtube(tmp_path, monkeypatch, ff, yt):
    """월간은 YouTube에서 다시 받을 수 없다 — Drive가 원본이므로 Drive에 먼저 올린다."""
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    order = []
    real_drive, real_yt = h.store.upload_with_id, yt.upload

    def drive(path, key, name):
        order.append(("drive", key, name))
        return real_drive(path, key, name)

    def youtube(*args, **kw):
        order.append(("youtube",))
        return real_yt(*args, **kw)

    h.store.upload_with_id = drive
    monkeypatch.setattr(mo.yt_up, "upload_video", youtube)
    h.run()
    assert order == [("drive", "monthly", f"{M}-월간.mp4"), ("youtube",)]


def test_youtube_failure_keeps_the_drive_file_and_reports_the_drive_link(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    yt.error = "quotaExceeded"
    h.run()
    d = doc(h)
    assert d["status"] == "done" and d["yt_url"] is None and d["drive_link"]
    assert (h.root / "07_monthly" / f"{M}-월간.mp4").exists()
    [note] = h.notes
    assert note["title"] == "2026년 12월 월간 하이라이트 완료 · YouTube 실패" and note["tags"] == ["soccer", "warning"]
    assert "※ YouTube 업로드 실패 — quotaExceeded" in note["message"] and d["drive_link"] in note["message"]


def test_no_eligible_scenes_fails_with_a_reason_and_can_be_rebuilt(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_game(h, "2026-12-06", "1경기", None, ["building", "tree"])          # 태깅이 하나도 없다
    h.store.write_json(f"monthly_{M}.json", {"status": "build", "attempts": 0})
    h.store.write_json("requests.json", {"items": [{"type": "monthly", "month": M}]})
    h.run()
    assert doc(h)["status"] == "failed" and "넣을 장면이 없습니다" in doc(h)["error"]
    assert yt.uploads == [] and h.files("monthly") == []                   # 만들지 않았다
    [note] = h.notes
    assert note["title"] == "2026년 12월 월간 하이라이트를 만들지 못했습니다" and note["tags"] == ["warning"]
    assert "월간 다시 만들기" in note["message"]

    seed_game(h, "2026-12-13", "1경기", "scrimmage", [None, None])           # 태깅을 고친 뒤 "다시 만들기"
    d = doc(h)
    d.update(status="build", attempts=0, error=None)
    h.store.write_json(f"monthly_{M}.json", d)
    h.notes.clear()
    h.run()
    assert doc(h)["status"] == "done" and len(doc(h)["picked"]) == 2


def test_monthly_build_waits_until_the_inbox_videos_are_processed(tmp_path, monkeypatch, ff, yt):
    """월간은 수 분~수십 분이 걸린다 — 같은 실행의 경기 영상 처리를 늦추지 않도록 인박스를 다 처리한 뒤에 한다."""
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    h.add("g.mp4", 1500, start_min=0)
    seen = []
    real = mo.MonthlyBuilder._build_month

    def spy(self, month):
        seen.append({"published": len(h.published), "built": len(h.built), "files_left": h.files("inbox")})
        return real(self, month)

    monkeypatch.setattr(mo.MonthlyBuilder, "_build_month", spy)
    h.run()
    assert seen == [{"published": 1, "built": 1, "files_left": []}]          # 경기 영상(분석·게시)을 끝낸 다음에 월간 시작
    assert doc(h)["status"] == "done"
    assert [n["title"] for n in h.notes] == ["하이라이트 1경기 완료 (영상 1개 처리)",
                                             "2026년 12월 월간 하이라이트 완료"]    # 알림 순서도 그렇다


def test_monthly_request_alone_still_runs_and_publish_requests_do_not_trigger_it(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    h.run()                                                                    # 인박스가 비어 있어도 끝에서 월간을 만든다
    assert doc(h)["status"] == "done"
    h.runner.monthly._tried.clear()
    calls = []
    monkeypatch.setattr(mo.MonthlyBuilder, "handle", lambda self, items: calls.append(items))
    h.runner.process_requests()                                                # 시작·기다리는 동안의 호출 — 월간은 안 건드린다
    assert calls == []
    h.runner.process_requests(monthly=True)
    assert len(calls) == 1


def test_only_build_and_running_are_processed(tmp_path, monkeypatch, ff, yt):
    for status in ("waiting_tags", "done", "failed"):
        h = Harness(tmp_path / status, monkeypatch)
        seed_month(h, status=status)
        h.run()
        assert doc(h)["status"] == status and yt.uploads == [] and ff.calls == [] and h.notes == []
    h = Harness(tmp_path / "none", monkeypatch)
    h.store.write_json("requests.json", {"items": [{"type": "monthly", "month": M}]})    # monthly 파일이 아직 없다
    h.run()
    assert yt.uploads == []


def test_dead_running_month_is_retried_and_gives_up_after_three_attempts(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h, status="running", attempts=1)                             # 지난 실행이 중간에 죽었다
    h.run()
    assert doc(h)["status"] == "done" and doc(h)["attempts"] == 2

    h2 = Harness(tmp_path / "again", monkeypatch)
    seed_month(h2, status="running", attempts=mo.MAX_ATTEMPTS)
    h2.run()
    assert doc(h2)["status"] == "failed" and len(yt.uploads) == 1           # 더 만들지 않는다
    assert f"{mo.MAX_ATTEMPTS}번 만들다 중간에 멈춰서" in h2.notes[0]["message"]


def test_clips_that_were_cleaned_up_by_retention_are_reported_as_expired(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    (h.root / "06_clips" / D1).unlink()
    (h.root / "06_clips" / D2).unlink()
    clips_doc = h.store.read_json("clips_2026-12-20_1경기.json")
    clips_doc["expired"] = True                                              # retention.cleanup_clips가 남기는 표시
    h.store.write_json("clips_2026-12-20_1경기.json", clips_doc)
    h.run()
    assert [p["date"] for p in doc(h)["picked"]].count("2026-12-20") == 0
    msg = h.notes[0]["message"]
    assert "※ 보관 기간이 지나 정리된 클립 2개는 뺐습니다." in msg and "찾지 못해" not in msg
    assert "· 12월 20일" not in msg


def test_clips_missing_without_the_expired_mark_are_reported_as_not_found(tmp_path, monkeypatch, ff, yt):
    """정리된 게 아니라 사라진 클립(실수로 지움·Drive 문제)은 다른 문구로 — 사용자가 알아채야 한다."""
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    (h.root / "06_clips" / D1).unlink()
    (h.root / "06_clips" / D2).unlink()
    h.run()
    msg = h.notes[0]["message"]
    assert "※ 클립 파일 2개를 Drive 06_clips에서 찾지 못해 뺐습니다." in msg and "보관 기간이 지나" not in msg


def test_all_clips_gone_fails_the_month(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    for p in (h.root / "06_clips").iterdir():
        p.unlink()
    h.run()
    assert doc(h)["status"] == "failed" and "넣을 장면이 없습니다" in doc(h)["error"]


def test_missing_font_builds_cards_without_text_and_warns(tmp_path, monkeypatch, ff, yt):
    monkeypatch.setattr(mo.sh, "TITLE_FONT", str(tmp_path / "no-such-font.ttf"))
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    h.run()
    assert doc(h)["status"] == "done" and ff.cards == [None] * 5
    assert not any("drawtext" in c[c.index("-vf") + 1] for c in ff.of("card"))
    assert "※ 카드에 쓸 한글 글꼴을 찾지 못해 카드에 글자가 없습니다." in h.notes[0]["message"]


def test_a_month_is_built_once_per_run_even_if_the_request_is_listed_twice(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    h.store.write_json("requests.json", {"items": [{"type": "monthly", "month": M}, {"type": "monthly", "month": M},
                                                   {"type": "monthly", "month": "bogus"}]})
    h.run()
    assert len(yt.uploads) == 1 and len(h.notes) == 1 and doc(h)["attempts"] == 1


def test_a_build_error_marks_the_month_failed_and_reports_it(tmp_path, monkeypatch, ff, yt):
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)
    real = ff.run

    def boom(cmd, cwd=None):
        if cmd[0] == "ffmpeg" and "concat" in cmd:
            raise RuntimeError("command failed: ffmpeg -y -f")
        return real(cmd, cwd)

    monkeypatch.setattr(mo.sh, "run", boom)
    h.run()
    assert doc(h)["status"] == "failed" and "처리 중 오류가 났습니다" in doc(h)["error"]
    assert not list(h.workdir.glob("monthly_*"))                            # 작업 폴더는 치운다
    assert h.notes[0]["title"].endswith("월간 하이라이트를 만들지 못했습니다")


def test_drive_auth_error_propagates_and_leaves_the_month_running(tmp_path, monkeypatch, ff, yt):
    from google.auth.exceptions import RefreshError
    h = Harness(tmp_path, monkeypatch)
    seed_month(h)

    def dead(*args):
        raise RefreshError("invalid_grant")

    h.store.upload_with_id = dead
    with pytest.raises(RefreshError):
        h.run()
    assert doc(h)["status"] == "running" and yt.uploads == []              # 인증을 고친 뒤 다음 실행이 이어서


def test_rebuilding_replaces_the_old_drive_file_and_trashes_it(tmp_path, monkeypatch, ff, yt):
    from fake_drive import FOLDER, FakeDrive, FakeSession

    from kaggle_runner.notify import Notifier
    from kaggle_runner.stores import DriveStore

    drive = FakeDrive()
    root = drive.add("HLEditor", "root", mime=FOLDER)
    store = DriveStore(None, root, service=drive, session=FakeSession(drive), sleep=lambda s: None)
    h = Harness(tmp_path, monkeypatch)                                       # 설정·시계·알림만 빌려 쓴다
    clips_dir = store.folder_id("clips")
    for date, label, kind, ends, opp in (("2026-12-06", "1경기", "scrimmage", [None, None], ""),
                                          ("2026-12-13", "1경기", "opponent", ["tree", "building"], "OO FC")):
        clips = [make_clip(i + 1, end=e, date=date, label=label) for i, e in enumerate(ends)]
        for c in clips:                                                      # 실제 업로드처럼 file_id는 Drive가 준 아이디
            c["file_id"] = drive.add(c["name"], clips_dir, content=c["name"].encode())
        store.write_json(f"games_{date}_{label}.json", make_game(date, label, kind, opp, attack_end="tree"))
        store.write_json(f"clips_{date}_{label}.json", {"clips": clips})
        store.write_json(f"day_{date}.json", {"date": date, "games": [{"label": label}]})
    store.write_json(f"monthly_{M}.json", {"status": "build", "attempts": 0})
    store.write_json("requests.json", {"items": [{"type": "monthly", "month": M}]})

    def run_once():
        runner = ir.InboxRunner(store, Notifier("t", post=h._post, log=lambda m: None), h.settings,
                                workdir=tmp_path / "work", now=h.clock.now, sleep=h.clock.sleep, log=h.logs.append)
        runner.run()

    run_once()
    monthly_dir = store.folder_id("monthly")
    first = doc_of(store)["drive_file_id"]
    assert drive.children(monthly_dir) == [f"{M}-월간.mp4"] and doc_of(store)["status"] == "done"

    d = doc_of(store)                                                        # 태깅을 고친 뒤 "월간 다시 만들기"
    d.update(status="build", attempts=0)
    store.write_json(f"monthly_{M}.json", d)
    run_once()
    second = doc_of(store)["drive_file_id"]
    assert second != first and drive.items[first]["trashed"] is True        # 같은 이름 옛 파일은 휴지통
    assert drive.children(monthly_dir) == [f"{M}-월간.mp4"] and drive.items[second]["trashed"] is False
    assert len(yt.uploads) == 2                                              # YouTube에는 새로 올린다


def doc_of(store, month=M):
    return store.read_json(f"monthly_{month}.json")
