"""월간 하이라이트 — 그달 태깅에서 한울타리 장면만 골라 6분 영상 하나로 만든다 (MONTHLY_PLAN.md 3-6).

Apps Script(dailyJobs)가 매달 1일 09:00에 `monthly_{YYYY-MM}.json`을 만들고(태깅 안 된 경기가 있으면 waiting_tags로
기다렸다가), 준비되면 status=build + 대기열 `{type: "monthly", month}`를 넣는다. 러너(여기)는 그 요청을 읽어:

  1. 그달 경기의 clips_*·games_*를 읽고 **월간 대상 클립**(tags.monthly_eligible)만 모은다 — 청백전 전부, 다른 팀은
     공격 + 실점 아닌 수비. 태깅 안 된 경기는 대상이 없으므로 저절로 빠진다.
  2. 점수 순으로 경기당 4개·경기일당 8개 이내, 길이 합이 6분에 닿을 때까지 고른 뒤 날짜순으로 늘어놓는다(결정적).
  3. 시작 카드 → 경기마다 [경기일 카드 + 클립들] → "이 달의 함성 1위" 카드 + 그 장면 한 번 더. 전부 같은 규격으로
     재인코딩(CRF 20, 카드 구간은 무음)한 뒤 이어 붙인다.
  4. Drive `07_monthly`에 **먼저** 올리고(같은 이름 옛 파일은 휴지통) YouTube에 올린다. YouTube가 실패해도 Drive에
     있으니 done으로 두고 Drive 링크로 알린다(월간은 YouTube에서 다시 받을 수 없어 Drive가 원본이다).

상태 전이(쓰는 쪽): waiting_tags·build는 Apps Script, running → done·failed는 여기. 러너는 쓰기 직전에 다시 읽어
상태가 예상과 같을 때만 쓴다. 죽은 running은 3번까지 다시 한다.
"""

from __future__ import annotations

import calendar
import json
import re
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pan_signal
import soccer_highlights as sh
import youtube_uploader as yt_up
from kaggle_runner import naming, tags
from kaggle_runner.common import (
    clips_file,
    day_file,
    games_file,
    is_auth_error,
    iso,
    monthly_file,
)

MONTHLY_STATES = ("build", "running")
MAX_ATTEMPTS = 3
DRYRUN_PREFIX = "https://youtu.be/DRYRUN-"

TEAM_NAME = "한울타리 FC"
PER_GAME_MAX = 4                 # 한 경기에서 최대 이만큼
PER_DAY_MAX = 8                  # 한 경기일(같은 날짜의 모든 경기)에서 최대 이만큼
PRESET = "medium"
START_CARD_SEC = 2.0
GAME_CARD_SEC = 1.5
SHOUT_CARD_SEC = 1.5
CARD_BG = "0x14202b"
AUDIO_RATE = 48000
SHOUT_TITLE = "이 달의 함성 1위"


def label_no(label: str) -> int:
    return int(re.sub(r"\D", "", str(label)) or 0)


def kor_date(date: str) -> str:
    _, m, d = date.split("-")
    return f"{int(m)}월 {int(d)}일"


def kor_month(month: str) -> str:
    y, m = month.split("-")
    return f"{y}년 {int(m)}월"


def mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# ─── 장면 고르기(순수 함수 — ffmpeg·Drive 없이 시험한다) ─────────────────────────
@dataclass
class Scene:
    date: str
    label: str
    idx: int
    clip: dict            # clips_*.json의 클립
    game: dict            # games_*.json
    delta: float          # 클립 안 최대 함성 크기(delta_db) — "이 달의 함성 1위"의 기준
    score: float          # 고르기 점수

    @property
    def dur(self) -> float:
        return float(self.clip.get("dur") or 0)

    @property
    def order(self) -> tuple[str, int, int]:
        return (self.date, label_no(self.label), self.idx)


def _max_delta(clip: dict) -> float:
    vals = [float(c["delta_db"]) for c in clip.get("cands") or [] if c.get("delta_db") is not None]
    return max(vals) if vals else 0.0


def _max_conf(clip: dict) -> float:
    vals = [float(c["confidence"]) for c in clip.get("cands") or [] if c.get("confidence") is not None]
    return max(vals) if vals else 0.0


def _pan_boost(clip: dict) -> float:
    states = ((c.get("pan") or {}).get("state") for c in clip.get("cands") or [])
    return pan_signal.PAN_BONUS if any(s in pan_signal.BOOST_STATES for s in states) else 0.0


def collect_pool(entries: list[tuple[dict, dict]],
                 available: set[str] | None = None) -> tuple[list[Scene], dict[str, int]]:
    """(game, clips_doc) 목록 → (월간 대상 장면들, 빠진 클립 수 {"expired": 정리돼서, "lost": 파일을 못 찾아서}).

    점수 = 클립 안 최대 delta_db(그 경기의 "잘못 잡힘" 아닌 클립들 안에서 0~1로 정규화) + 최대 confidence
    + 팬 상태 가산(골대 앞 정지·빠른 스위프). 정규화 범위가 한 점이면 0.5.
    06_clips에 없는 클립은 그 경기의 clips JSON이 `expired`(보관 기간이 지나 정리됨)일 때만 "정리돼서"이고,
    나머지는 "파일을 못 찾아서"다(실수로 지웠거나 Drive 문제 — 사용자가 알아야 한다).
    """
    pool: list[Scene] = []
    missing = {"expired": 0, "lost": 0}
    for game, doc in entries:
        clips = doc.get("clips") or []
        deltas = [_max_delta(c) for c in tags.weekly_clips(clips, game)]
        lo, hi = (min(deltas), max(deltas)) if deltas else (0.0, 0.0)
        for clip in tags.monthly_eligible(clips, game):
            if available is not None and clip.get("file_id") not in available:
                missing["expired" if doc.get("expired") else "lost"] += 1
                continue
            d = _max_delta(clip)
            norm = (d - lo) / (hi - lo) if hi > lo else 0.5
            pool.append(Scene(date=game["date"], label=game["label"], idx=clip["idx"], clip=clip, game=game,
                              delta=d, score=norm + _max_conf(clip) + _pan_boost(clip)))
    return pool, missing


def pick_scenes(pool: list[Scene], target_sec: float, per_game: int = PER_GAME_MAX,
                per_day: int = PER_DAY_MAX) -> list[Scene]:
    """점수 높은 순으로 경기당·경기일당 상한 안에서 길이 합이 target_sec에 닿을 때까지 고르고 날짜순으로 돌려준다."""
    ranked = sorted(pool, key=lambda s: (-s.score, *s.order))      # 동점은 이른 장면 먼저 — 결정적
    picked: list[Scene] = []
    per_game_n: Counter = Counter()
    per_day_n: Counter = Counter()
    total = 0.0
    for s in ranked:
        if total >= target_sec:
            break
        if per_game_n[(s.date, s.label)] >= per_game or per_day_n[s.date] >= per_day:
            continue
        picked.append(s)
        per_game_n[(s.date, s.label)] += 1
        per_day_n[s.date] += 1
        total += s.dur
    return sorted(picked, key=lambda s: s.order)


def _game_title(game: dict) -> str:
    return naming.game_name(game["date"], game["label"], game.get("kind"), game.get("opponent") or "")


def describe_excluded(entries: list[tuple[dict, dict]]) -> list[str]:
    """월간에 한 장면도 못 넣은 경기를 이름과 이유로 — 완료 알림의 ※ 줄이 된다(말없이 빠지지 않게).

    · 경기 종류를 태깅하지 않은 경기
    · 종류는 태깅했지만 월간 대상 장면이 0개인 경기(다른 팀인데 공격/수비를 하나도 안 정했거나, 장면이 모두 실점·잘못 잡힘)
    """
    out = []
    for game, doc in entries:
        if not game.get("kind"):
            out.append(f"{_game_title(game)}: 경기 종류(청백전 / 다른 팀)를 태깅하지 않아 월간에서 빠졌습니다.")
        elif not tags.monthly_eligible(doc.get("clips") or [], game):
            out.append(f"{_game_title(game)}: 월간에 넣을 장면이 없어 빠졌습니다 — 장면마다 공격/수비를 정하지 않았거나 "
                       "모든 장면이 실점·잘못 잡힘입니다.")
    return out


def describe_not_picked(pool: list[Scene], picked: list[Scene]) -> list[str]:
    """대상 장면은 있었는데 점수 경쟁(경기당·경기일당 상한, 6분)에서 한 장면도 못 든 경기."""
    chosen = {(s.date, s.label) for s in picked}
    seen: dict[tuple[str, str], dict] = {}
    for s in pool:
        if (s.date, s.label) not in chosen:
            seen.setdefault((s.date, s.label), s.game)
    return [f"{_game_title(g)}: 다른 장면의 점수가 더 높아 이번 월간에는 들어가지 못했습니다." for g in seen.values()]


def shout_scene(picked: list[Scene]) -> Scene:
    """이 달의 함성 1위 — 고른 장면 중 함성(delta_db)이 가장 큰 것, 같으면 점수·이른 순."""
    return max(picked, key=lambda s: (s.delta, s.score))


def game_card_lines(date: str, label: str, kind: str | None, opponent: str) -> list[str]:
    """경기일 카드 글: `12월 6일 · 1경기` + (`vs OO FC` | `청백전`)."""
    lines = [f"{kor_date(date)} · {label}"]
    if kind in (naming.KIND_SCRIMMAGE, naming.SCRIMMAGE_LABEL):
        lines.append(naming.SCRIMMAGE_LABEL)
    elif (opponent or "").strip():
        lines.append(f"vs {opponent.strip()}")
    return lines


def month_description(month: str, chapters: list[tuple[float, str]]) -> str:
    """YouTube 설명 — 챕터(경기일마다 + 함성 1위). 첫 챕터는 0:00이어야 한다."""
    lines = [f"{TEAM_NAME} {kor_month(month)} 하이라이트", ""]
    lines += [f"{mmss(t)} {name}" for t, name in chapters]
    lines += ["", *yt_up.DESCRIPTION_FOOTER]
    return "\n".join(lines)


def build_monthly_report(event: dict) -> tuple[str, str, list[str]]:
    """월간 결과 이벤트 → (제목, 본문, 태그). 성공 본문은 BAND에 붙여넣을 글(탭하면 복사 — click 없음)."""
    month = event["month"]
    if event["kind"] == "monthly_fail":
        return (f"{kor_month(month)} 월간 하이라이트를 만들지 못했습니다",
                f"※ {event['reason']}\n검토 페이지의 월간 화면에서 '월간 다시 만들기'를 누를 수 있습니다.", ["warning"])
    lines = [f"{kor_month(month)} 하이라이트입니다.", ""]
    if event.get("yt_url"):
        lines.append(event["yt_url"])
    lines += ["", *[f"· {g}" for g in event.get("games", [])], "", "HLEditor 자동 생성"]
    notes = [f"※ {w}" for w in event.get("warnings", [])]
    if not event.get("yt_url"):
        notes.insert(0, f"※ YouTube 업로드 실패 — {event.get('yt_error') or '알 수 없음'}\n"
                        f"월간 영상은 Drive HLEditor/07_monthly에 올려 두었습니다: {event.get('drive_link')}")
    message = "\n".join(lines) + ("\n\n" + "\n".join(notes) if notes else "")
    ok = bool(event.get("yt_url"))
    title = f"{kor_month(month)} 월간 하이라이트 " + ("완료" if ok else "완료 · YouTube 실패")
    return title, message, ["soccer"] + ([] if ok else ["warning"])


class MonthlyError(RuntimeError):
    """월간을 만들 수 없는 이유를 사용자에게 보여 줄 문장으로 담는다."""


class MonthlyBuilder:
    """InboxRunner에 붙어 대기열의 monthly 요청을 처리한다. 저장소·알림 기록·시계는 러너 것을 쓴다."""

    def __init__(self, runner):
        self.r = runner
        self._tried: set[str] = set()      # 한 실행에서 같은 달을 두 번 시도·보고하지 않는다

    def handle(self, items: list[dict]) -> int:
        handled = 0
        for month in dict.fromkeys(i["month"] for i in items
                                   if i.get("type") == "monthly" and re.fullmatch(r"\d{4}-\d{2}", i.get("month") or "")):
            if month in self._tried:
                continue
            self._tried.add(month)
            try:
                handled += int(bool(self._build_month(month)))
            except Exception as e:
                if is_auth_error(e):
                    raise
                self.r.log(f"월간({month}) 처리 실패(상태는 그대로 두고 다음 실행에서 다시): {e!r}")
        return handled

    # 상태 ------------------------------------------------------------------
    def _transition(self, month: str, expect: tuple[str, ...], **fields) -> dict | None:
        name = monthly_file(month)
        doc = self.r.store.read_json(name)
        if not doc or doc.get("status") not in expect:
            return None
        doc.update(fields)
        self.r.store.write_json(name, doc)
        return doc

    def _fail(self, month: str, reason: str) -> None:
        self._transition(month, MONTHLY_STATES, status="failed", error=reason[:300])
        self.r._record("monthly_fail", month=month, reason=reason)

    def _build_month(self, month: str) -> bool:
        r = self.r
        doc = r.store.read_json(monthly_file(month))
        if not doc or doc.get("status") not in MONTHLY_STATES:
            return False                                    # 이미 처리됐거나 태깅을 기다리는 중
        attempts = int(doc.get("attempts") or 0)
        if doc["status"] == "running" and attempts >= MAX_ATTEMPTS:
            self._fail(month, f"월간 영상을 {MAX_ATTEMPTS}번 만들다 중간에 멈춰서 더 이상 자동으로 하지 않습니다. "
                              "Kaggle 로그를 확인하세요.")
            return True
        if self._transition(month, MONTHLY_STATES, status="running", attempts=attempts + 1, error=None) is None:
            return False
        try:
            self._do_build(month)
        except Exception as e:
            if is_auth_error(e):
                raise                                       # 상태는 running 그대로 — 인증을 고친 뒤 이어서
            r.log(f"월간({month}) 만들기 실패: {e!r}")
            self._fail(month, str(e) if isinstance(e, MonthlyError) else f"처리 중 오류가 났습니다: {e}")
        return True

    # 재료 ------------------------------------------------------------------
    def _load_games(self, month: str) -> list[tuple[dict, dict]]:
        """그달 경기의 (games, clips) — day_*.json이 경기 목록(label)을 안다. 상태 파일은 이름으로만 읽는다(M12)."""
        r = self.r
        year, mon = (int(x) for x in month.split("-"))
        entries = []
        for d in range(1, calendar.monthrange(year, mon)[1] + 1):
            date = f"{month}-{d:02d}"
            day = r.store.read_json(day_file(date))
            for g in (day or {}).get("games", []):
                label = g.get("label")
                game = r.store.read_json(games_file(date, label)) if label else None
                doc = r.store.read_json(clips_file(date, label)) if game else None
                if game and doc:
                    entries.append((game, doc))
        return entries

    def _do_build(self, month: str) -> None:
        r = self.r
        entries = self._load_games(month)
        available = {f.id: f for f in r.store.list("clips")}
        pool, missing = collect_pool(entries, set(available))
        excluded = describe_excluded(entries)
        warnings: list[str] = list(excluded)
        if missing["expired"]:
            warnings.append(f"보관 기간이 지나 정리된 클립 {missing['expired']}개는 뺐습니다.")
        if missing["lost"]:
            warnings.append(f"클립 파일 {missing['lost']}개를 Drive 06_clips에서 찾지 못해 뺐습니다.")
        if not pool:
            raise MonthlyError(
                f"{kor_month(month)}에 월간 영상에 넣을 장면이 없습니다 — 경기 종류(청백전/다른 팀)와 장면별 공격/수비를 "
                "태깅했는지, 클립이 정리되지 않았는지 확인하세요." + "".join(f"\n{w}" for w in warnings[:6]))
        picked = pick_scenes(pool, r.s.monthly_target_min * 60.0)
        warnings += describe_not_picked(pool, picked)
        shout = shout_scene(picked)
        r.log(f"월간 {month}: 대상 {len(pool)}개 중 {len(picked)}개 선택 "
              f"({sum(s.dur for s in picked) / 60:.1f}분), 함성 1위 {shout.date} {shout.label} #{shout.idx}")

        work = Path(r.workdir) / f"monthly_{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            out, chapters, duration, shout_src = self._assemble(month, picked, shout, available, work, warnings)
            name = f"{month}-월간.mp4"
            old = [f for f in r.store.list("monthly") if f.name == name]
            drive_link, file_id = r.store.upload_with_id(out, "monthly", name)    # Drive가 먼저 — 원본이다
            for f in old:
                if f.id != file_id:                                              # 같은 달을 다시 만들면 옛 파일은 휴지통
                    r.store.trash(f, "monthly")
            yt_url, yt_error = self._upload_youtube(month, out, chapters, shout, shout_src, work)
            games = list(dict.fromkeys(c[1] for c in chapters if c[1] != SHOUT_TITLE))
            done = self._transition(month, ("running",), status="done", error=None, yt_url=yt_url,
                                    drive_file_id=file_id, drive_link=drive_link, done_at=iso(r.now()),
                                    duration_sec=round(duration, 1),
                                    chapters=[[round(t, 1), name] for t, name in chapters],
                                    picked=[{"date": s.date, "label": s.label, "idx": s.idx} for s in picked])
            if done is None:
                # 만드는 동안 상태가 바뀌었다(예: 사용자가 다시 만들기를 눌러 build로 돌렸다). 바뀐 상태를 덮어쓰지 않고
                # 완료 알림도 보내지 않는다 — 곧 다시 만들어질 것이고, 그때 알림이 간다. 올라간 파일은 로그에 남긴다.
                r.log(f"월간({month}) 상태가 만드는 동안 바뀌어 완료를 기록·알리지 않았습니다 "
                      f"(올라간 파일: Drive {drive_link}, YouTube {yt_url})")
                return
            r._record("monthly", month=month, yt_url=yt_url, yt_error=yt_error, drive_link=drive_link,
                      games=games, warnings=warnings)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # 조립 ------------------------------------------------------------------
    def _assemble(self, month, picked, shout, available, work, warnings):
        """클립을 받아 카드와 함께 같은 규격으로 인코딩하고 이어 붙인다. (결과 파일, 챕터, 길이, 함성 클립 경로)."""
        r = self.r
        raw: dict[tuple, Path] = {}
        for s in picked:
            raw[s.order] = Path(r.store.download(available[s.clip["file_id"]], work / "raw" / s.clip["name"]))
        width, height, fps = probe_video(raw[picked[0].order])
        font_ok = self._prepare_font(work, warnings)

        segs: list[tuple[Path, str | None]] = []     # (인코딩된 파일, 챕터 이름 — 챕터가 시작하는 구간만)
        counter = iter(range(1, 10_000))
        encoded: dict[tuple, Path] = {}

        def card(lines: list[str], sec: float, chapter: str | None = None) -> None:
            dst = work / "seg" / f"{next(counter):03d}_card.mp4"
            dst.parent.mkdir(parents=True, exist_ok=True)
            self._encode_card(lines, sec, dst, width, height, fps, work, font_ok)
            segs.append((dst, chapter))

        def clip(s: Scene) -> None:
            if s.order not in encoded:
                dst = work / "seg" / f"{next(counter):03d}_clip.mp4"
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._encode_clip(raw[s.order], dst, width, height, fps)
                encoded[s.order] = dst
            segs.append((encoded[s.order], None))

        card([TEAM_NAME, f"{kor_month(month)} 하이라이트"], START_CARD_SEC)
        last = None
        for s in picked:
            if (s.date, s.label) != last:
                last = (s.date, s.label)
                lines = game_card_lines(s.date, s.label, s.game.get("kind"), s.game.get("opponent") or "")
                card(lines, GAME_CARD_SEC, " ".join(lines))
            clip(s)
        card([SHOUT_TITLE], SHOUT_CARD_SEC, SHOUT_TITLE)
        clip(shout)

        t, chapters = 0.0, []
        for path, chapter in segs:
            if chapter:
                chapters.append((0.0 if not chapters else t, chapter))      # 첫 챕터는 시작 카드부터 0:00
            t += sh.probe_duration(str(path))
        listfile = work / "monthly_concat.txt"
        listfile.write_text("".join(concat_line(p) for p, _ in segs), encoding="utf-8")
        out = work / f"{month}-월간.mp4"
        sh.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(out),
                "-loglevel", "error"])
        return out, chapters, t, raw[shout.order]

    def _prepare_font(self, work: Path, warnings: list[str]) -> bool:
        try:
            shutil.copyfile(sh.TITLE_FONT, work / "title_font.ttf")   # 콜론 없는 상대 경로(cwd=work)로 참조한다
            return True
        except Exception as e:
            self.r.log(f"카드 글꼴 준비 실패 — 카드에 글자 없이 진행: {e!r}")
            warnings.append("카드에 쓸 한글 글꼴을 찾지 못해 카드에 글자가 없습니다.")
            return False

    def _encode_clip(self, src: Path, dst: Path, w: int, h: int, fps: str) -> None:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
              f"setsar=1,fps={fps},format=yuv420p")
        sh.run(["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-af", f"aresample={AUDIO_RATE}",
                "-c:v", "libx264", "-preset", PRESET, "-crf", str(self.r.s.monthly_crf),
                "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2",
                str(dst), "-loglevel", "error"])

    def _encode_card(self, lines: list[str], sec: float, dst: Path, w: int, h: int, fps: str, work: Path,
                     font_ok: bool) -> None:
        """단색 배경 + 가운데 글자, 소리는 무음. 글은 줄마다 파일로 넘겨(따옴표·콜론 이스케이프를 피한다) 줄별로 가운데 정렬한다."""
        vf = "format=yuv420p"
        if font_ok:
            size = max(24, min(h // 14, int(w * 0.88 / max(len(x) for x in lines))))
            line_h = int(size * 1.6)
            parts = []
            for i, line in enumerate(lines):
                textfile = work / f"card_{dst.stem}_{i}.txt"
                textfile.write_text(line, encoding="utf-8")
                # expansion=none: textfile 안의 `%{...}`를 drawtext가 확장하지 않게 한다 — 상대 이름에 %가 있어도 카드가 깨지지 않는다
                parts.append(f"drawtext=fontfile=title_font.ttf:textfile={textfile.name}:expansion=none:fontcolor=white:"
                             f"fontsize={size}:x=(w-text_w)/2:y=(h-{len(lines) * line_h})/2+{i * line_h}")
            vf = ",".join(parts + ["format=yuv420p"])
        sh.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={CARD_BG}:s={w}x{h}:r={fps}:d={sec}",
                "-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo", "-t", str(sec), "-vf", vf,
                "-c:v", "libx264", "-preset", PRESET, "-crf", str(self.r.s.monthly_crf),
                "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE), "-ac", "2",
                str(dst), "-loglevel", "error"], cwd=str(work))

    def _upload_youtube(self, month, out, chapters, shout, shout_src, work):
        r = self.r
        title = f"{month}-월간 하이라이트"
        if r.s.dry_run:
            r.log(f"(dry-run) YouTube 제목: {title}")
            return f"{DRYRUN_PREFIX}{month}", None
        thumb = None
        try:
            cand = max(shout.clip.get("cands") or [{}], key=lambda c: float(c.get("delta_db") or 0))
            ts = max(0.0, float(cand.get("peak") or 0) - float(shout.clip.get("src_start") or 0))
            if yt_up.extract_thumbnail(str(shout_src), ts, str(work / "thumb.jpg")):
                thumb = str(work / "thumb.jpg")
        except Exception as e:
            r.log(f"월간 썸네일 추출 실패(업로드는 계속): {e!r}")
        try:
            url = yt_up.upload_video(str(out), title, month_description(month, chapters), thumbnail_path=thumb,
                                     privacy=r.s.youtube_privacy)
            return url, None
        except Exception as e:
            r.log(f"월간 YouTube 업로드 실패: {e!r}")
            return None, str(e)[:300]


def concat_line(path) -> str:
    """concat 목록 한 줄. 데먹서는 백슬래시를 이스케이프로 해석하므로 forward slash 경로를 쓰고, 작은따옴표는 '\\'' 로 쓴다."""
    safe = Path(path).as_posix().replace("'", "'\\''")
    return f"file '{safe}'\n"


def probe_video(path) -> tuple[int, int, str]:
    """첫 클립의 해상도·프레임레이트 — 카드와 나머지 클립을 이 규격에 맞춘다(yuv420p라 가로세로는 짝수)."""
    out = sh.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "stream=width,height,r_frame_rate", "-of", "json", str(path)])
    s = json.loads(out)["streams"][0]
    fps = s.get("r_frame_rate") or "30/1"
    if fps.startswith("0/") or fps.endswith("/0"):
        fps = "30/1"
    return int(s["width"]) // 2 * 2, int(s["height"]) // 2 * 2, fps
