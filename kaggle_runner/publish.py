"""게시 단계 — 검토 페이지(Apps Script)가 대기열에 넣은 요청을 처리한다 (MONTHLY_PLAN.md 3-1 B).

분석 단계(inbox_runner)는 HL_REVIEW가 켜져 있으면 YouTube에 올리지 않고 클립만 06_clips에 남긴 채 경기를
`pending`으로 둔다. 사용자가 폰에서 태깅하고 "게시"를 누르거나, 다음 날 09:00까지 아무도 안 보면 Apps Script가
`publish`로 바꾸고 `requests.json`에 요청을 넣는다. 러너는 이 파일을 **읽기만** 하고(Apps Script만 쓴다),
처리 여부는 대상 파일(`games_*.json`)의 상태로 판단한다.

  publish: 그날 status=publish(또는 죽은 running)인 경기마다 "잘못 잡힘"을 뺀 클립 전부를 내려받아 이어 붙이고
           YouTube에 올린다. 상대 득점 장면도 그대로 들어간다(tags.weekly_clips). 끝나면 BAND 글 알림.
  retitle: 이미 올린 영상의 제목(과 설명 첫 줄)을 태깅 결과(상대팀)에 맞게 고친다 — 영상은 다시 올리지 않는다.

상태 전이(쓰는 쪽): pending→publish는 Apps Script, publish→running→published/auto_published/failed는 여기.
러너는 쓰기 직전에 다시 읽어 상태가 예상과 같을 때만 쓴다.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import soccer_highlights as sh
import youtube_uploader as yt_up
from kaggle_runner import naming, tags
from kaggle_runner.common import (
    REQUESTS_FILE,
    clips_file,
    day_file,
    from_iso,
    games_file,
    is_auth_error,
    iso,
)
from kaggle_runner.stores import RemoteFile

MAX_ATTEMPTS = 3                              # 게시를 이만큼 시도하다 죽으면 자동으로는 더 하지 않는다
PUBLISH_STATES = ("publish", "running")       # running은 지난 실행이 중간에 죽은 것
DONE_STATES = ("published", "auto_published")
DRYRUN_PREFIX = "https://youtu.be/DRYRUN-"


class PublishError(RuntimeError):
    """게시할 수 없는 이유를 사용자에게 보여 줄 문장으로 담는다."""


def _mmss(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def description_from_clips(name: str, clips: list[dict], pre_sec: float) -> str:
    """YouTube 설명 — 올린 클립의 `dur`를 누적해 하이라이트 안 위치를 구한다('슈팅 #n').

    generate_description(분석 때 후보 목록으로 계산)과 같은 형식이다. 클립 일부를 뺀 영상이라
    클립 기록(dur·src_start·cands)으로 다시 계산해야 시각이 맞는다.
    """
    shot_lines, offset = [], 0.0
    for clip in clips:
        for cand in clip.get("cands") or []:
            if (cand.get("type") or "").strip().lower() not in yt_up.SHOT_TYPES:
                continue
            peak = float(cand.get("peak") or 0)
            rel = max(0.0, max(0.0, peak - pre_sec) - float(clip.get("src_start") or 0))
            shot_lines.append(f"{_mmss(offset + rel)} 슈팅 #{len(shot_lines) + 1}  (원본 {_mmss(peak)})")
        offset += float(clip.get("dur") or 0)
    return yt_up.format_description(name, shot_lines)


def concat_clips(paths: list[Path], out: Path, workdir: Path) -> None:
    """클립을 순서대로 이어 붙인다(같은 설정으로 인코딩돼 복사로 이어지고, 안 되면 재인코딩 폴백)."""
    if len(paths) == 1:
        shutil.copyfile(paths[0], out)
    else:
        sh.concat_videos(paths, out, workdir=workdir)


class Publisher:
    """InboxRunner에 붙어 대기열을 처리한다. 저장소·알림 기록·시계는 러너 것을 그대로 쓴다."""

    def __init__(self, runner):
        self.r = runner
        # process_requests는 한 실행에서 여러 번 불린다(시작·기다리는 동안·끝). 제목 변경은 실패해도 같은 실행에서
        # 두 번 시도·보고하지 않는다(publish는 status가 failed로 바뀌어 저절로 한 번만 된다).
        self._retitle_tried: set[tuple[str, str]] = set()

    # 대기열 ----------------------------------------------------------------
    def process_requests(self) -> int:
        """requests.json의 publish·retitle 요청을 처리하고, 건드린 경기 수를 돌려준다."""
        r = self.r
        try:
            doc = r.store.read_json(REQUESTS_FILE)
        except Exception as e:
            if is_auth_error(e):
                raise
            r.log(f"요청 대기열을 읽지 못했습니다(다음에 다시 확인): {e!r}")
            return 0
        items = (doc or {}).get("items") or []
        handled = 0
        dates = list(dict.fromkeys(i["date"] for i in items if i.get("type") == "publish" and i.get("date")))
        for date in dates:
            handled += self._publish_day(date)
        pairs = list(dict.fromkeys((i["date"], i["label"]) for i in items
                                   if i.get("type") == "retitle" and i.get("date") and i.get("label")))
        for date, label in pairs:
            if (date, label) in self._retitle_tried:
                continue
            self._retitle_tried.add((date, label))
            handled += self._guard(f"제목 변경({date} {label})", lambda d=date, lb=label: self._retitle(d, lb))
        return handled

    def _guard(self, what: str, fn) -> int:
        """한 경기가 실패해도 다른 경기·인박스 처리는 계속한다. 인증 오류만 위로 올린다."""
        try:
            return int(bool(fn()))
        except Exception as e:
            if is_auth_error(e):
                raise
            self.r.log(f"{what} 실패(상태는 그대로 두고 다음 실행에서 다시): {e!r}")
            return 0

    def _publish_day(self, date: str) -> int:
        r = self.r
        try:
            day = r.store.read_json(day_file(date)) or {}
        except Exception as e:
            if is_auth_error(e):
                raise
            r.log(f"날짜 기록({date})을 읽지 못해 게시를 미룹니다: {e!r}")
            return 0
        handled = 0
        for label in [g["label"] for g in day.get("games", []) if g.get("label")]:
            handled += self._guard(f"게시({date} {label})", lambda lb=label: self._publish_game(date, lb))
        return handled

    # 상태 파일 --------------------------------------------------------------
    def _transition(self, date: str, label: str, expect: tuple[str, ...], **fields) -> dict | None:
        """games_*.json을 다시 읽어 상태가 expect 중 하나일 때만 fields를 써 넣는다. 아니면 None."""
        name = games_file(date, label)
        game = self.r.store.read_json(name)
        if not game or game.get("status") not in expect:
            return None
        game.update(fields)
        self.r.store.write_json(name, game)
        return game

    def _update_day(self, date: str, label: str, **fields) -> None:
        r = self.r
        day = r.store.read_json(day_file(date)) or {"date": date, "games": []}
        record = next((g for g in day["games"] if g.get("label") == label), None)
        if record is None:
            record = {"label": label}
            day["games"].append(record)
        record.update(fields)
        r.store.write_json(day_file(date), day)

    # 게시 ------------------------------------------------------------------
    def _publish_game(self, date: str, label: str) -> bool:
        r = self.r
        game = r.store.read_json(games_file(date, label))
        if not game or game.get("status") not in PUBLISH_STATES:
            return False                                   # 이미 처리됐거나 검토 중 — 건드리지 않는다
        attempts = int(game.get("attempts") or 0)
        if game["status"] == "running" and attempts >= MAX_ATTEMPTS:
            self._fail(date, label, f"게시를 {MAX_ATTEMPTS}번 시도하다 중간에 멈춰서 더 이상 자동으로 하지 않습니다. "
                                    "Kaggle 로그를 확인한 뒤 검토 페이지에서 다시 게시해 주세요.")
            return True
        game = self._transition(date, label, PUBLISH_STATES, status="running", attempts=attempts + 1, error=None)
        if game is None:
            return False
        try:
            self._do_publish(date, label, game)
        except Exception as e:
            if is_auth_error(e):
                raise                                      # 상태는 running 그대로 — 인증을 고친 뒤 다음 실행이 이어서
            r.log(f"[{label}] 게시 실패: {e!r}")
            reason = str(e) if isinstance(e, PublishError) else f"처리 중 오류가 났습니다: {e}"
            self._fail(date, label, reason)
        return True

    def _do_publish(self, date: str, label: str, game: dict) -> None:
        r = self.r
        clips_doc = r.store.read_json(clips_file(date, label))
        if not clips_doc or not clips_doc.get("clips"):
            raise PublishError("클립 기록(clips_*.json)이 없어 게시할 수 없습니다.")
        play = tags.weekly_clips(clips_doc["clips"], game)          # "잘못 잡힘"만 뺀 전체 — 팀·실점으로는 안 뺀다
        kind, opponent = game.get("kind"), game.get("opponent") or ""
        name = naming.game_name(date, label, kind, opponent)
        file_name = naming.game_name(date, label, kind, opponent, for_file=True)
        warnings: list[str] = []

        if not play:
            self._transition(date, label, ("running",), status=self._done_status(game), published_clips=[],
                             yt_url=None, yt_title=None, published_at=iso(r.now()), error=None)
            r._record("skip", date=date, label=label, files=0, warnings=[])
            return

        yt_url = game.get("yt_url")
        if yt_url and not yt_url.startswith(DRYRUN_PREFIX):
            r.log(f"[{label}] 이미 올라간 영상이 있어 다시 올리지 않고 마무리만 합니다: {yt_url}")
        else:
            yt_url = self._build_and_upload(date, label, play, name, file_name)
            if yt_url is None:
                return                                              # 실패로 처리됨(백업 + 알림)

        self._transition(date, label, ("running",), status=self._done_status(game), yt_url=yt_url, yt_title=name,
                         published_clips=[c["idx"] for c in play], published_at=iso(r.now()), error=None)
        self._update_day(date, label, yt_url=yt_url, name=name)
        self._rename_originals(date, label, clips_doc, file_name, warnings)
        r._record("game", date=date, label=label, files=0, warnings=warnings, yt_url=yt_url)

    @staticmethod
    def _done_status(game: dict) -> str:
        """태그 없이 자동 게시된 경기는 auto_published — 나중에 태깅하면 제목이 바뀐다."""
        return "auto_published" if game.get("tag_source") == "auto" else "published"

    def _build_and_upload(self, date: str, label: str, play: list[dict], name: str, file_name: str) -> str | None:
        """클립을 받아 이어 붙이고 올린다. 성공하면 URL, YouTube 실패면 05_output에 백업하고 None."""
        r = self.r
        work = Path(r.workdir) / f"pub_{uuid.uuid4().hex[:8]}"
        work.mkdir(parents=True, exist_ok=True)
        try:
            listing = {f.id: f for f in r.store.list("clips")}
            paths = []
            for clip in play:
                remote = listing.get(clip.get("file_id"))
                if remote is None:
                    raise PublishError(f"클립 파일이 Drive 06_clips에 없습니다: {clip.get('name')} "
                                       "(지워졌거나 만료됨)")
                paths.append(Path(r.store.download(remote, work / clip["name"])))
            out = work / f"{file_name}.mp4"
            concat_clips(paths, out, work)

            thumb = self._thumbnail(play, paths, work)
            description = description_from_clips(name, play, sh.PRE_SEC)
            yt_url, yt_error = self._upload(label, out, name, description, thumb)
            backup = naming.highlight_name(file_name)
            if not yt_url:
                link = r.store.upload(out, "output", backup)
                self._fail(date, label, f"YouTube 업로드 실패 — {yt_error}", backup_link=link, yt_error=yt_error)
                return None
            if r.s.dry_run:                                         # 로컬 시험: 결과 영상을 05_output에 남긴다
                r.store.upload(out, "output", backup)
            # 올린 직후 URL부터 남긴다 — 이후 단계에서 죽어도 다음 실행이 다시 올리지 않게
            self._transition(date, label, ("running",), yt_url=yt_url, yt_title=name)
            return yt_url
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _thumbnail(self, play: list[dict], paths: list[Path], work: Path) -> str | None:
        """점수가 가장 높은 장면(신뢰도, 같으면 함성 크기)의 peak 프레임. 클립 파일에서 뽑는다 — 원본이 지워졌을 수 있다."""
        best = None
        for i, clip in enumerate(play):
            for cand in clip.get("cands") or []:
                score = (float(cand.get("confidence") or 0), float(cand.get("delta_db") or 0))
                if best is None or score > best[0]:
                    best = (score, i, float(cand.get("peak") or 0))
        if best is None:
            return None
        _, i, peak = best
        out = work / "thumb.jpg"
        ts = max(0.0, peak - float(play[i].get("src_start") or 0))
        return str(out) if yt_up.extract_thumbnail(str(paths[i]), ts, str(out)) else None

    def _upload(self, label: str, out: Path, name: str, description: str, thumb: str | None):
        r = self.r
        if r.s.dry_run:
            r.log(f"[{label}] (dry-run) YouTube 제목: {name}")
            return f"{DRYRUN_PREFIX}{label}", None
        try:
            return yt_up.upload_video(str(out), name, description, thumbnail_path=thumb,
                                      privacy=r.s.youtube_privacy), None
        except Exception as e:
            r.log(f"[{label}] YouTube 업로드 실패: {e!r}")
            return None, str(e)[:300]

    def _fail(self, date: str, label: str, reason: str, *, backup_link: str | None = None,
              yt_error: str | None = None) -> None:
        r = self.r
        self._transition(date, label, PUBLISH_STATES, status="failed", error=reason[:300])
        if backup_link:
            r._record("yt_fail", date=date, label=label, files=0, warnings=[], error=yt_error or reason,
                      link=backup_link)
        else:
            r._record("pub_fail", date=date, label=label, reason=reason)

    # 원본 이름 ---------------------------------------------------------------
    def _rename_originals(self, date: str, label: str, clips_doc: dict, file_name: str,
                          warnings: list[str]) -> None:
        """03_done의 원본 이름을 최종 경기 이름으로 바꾼다(상대팀이 붙은 경우). 이미 지워졌으면 그냥 둔다."""
        r = self.r
        originals = clips_doc.get("originals") or []
        changed = False
        for k, entry in enumerate(originals, start=1):
            new_name = naming.original_name(file_name, k, len(originals), Path(entry["name"]).suffix)
            if new_name == entry["name"]:
                continue
            remote = RemoteFile(id=entry["file_id"], name=entry["name"], size=0, created_utc=r.now())
            try:
                renamed = r.store.rename(remote, "done", new_name)
            except FileNotFoundError:
                r.log(f"[{label}] 원본이 03_done에 없어(보관 기간이 지남) 이름을 바꾸지 않음: {entry['name']}")
                continue
            except Exception as e:
                if is_auth_error(e):
                    raise
                r.log(f"[{label}] 원본 이름 변경 실패({entry['name']} → {new_name}): {e!r}")
                warnings.append(f"원본 이름을 바꾸지 못했습니다({entry['name']}) — {str(e)[:120]}")
                continue
            if renamed.id != entry["file_id"] and entry["file_id"] in r.claims:      # 로컬 저장소는 id가 파일명
                r.claims[renamed.id] = r.claims.pop(entry["file_id"])
            if renamed.id in r.claims:
                r.claims[renamed.id]["name"] = renamed.name
                r._save_claims()
            entry["file_id"], entry["name"] = renamed.id, renamed.name
            changed = True
        if changed:
            r.store.write_json(clips_file(date, label), clips_doc)

    # 늦은 태깅 → 제목 변경 -----------------------------------------------------
    def _retitle(self, date: str, label: str) -> bool:
        r = self.r
        game = r.store.read_json(games_file(date, label))
        if not game or game.get("status") not in DONE_STATES:
            return False
        tagged_at = from_iso(game.get("tagged_at"))
        retitled_at = from_iso(game.get("retitled_at"))
        if tagged_at is None or (retitled_at is not None and retitled_at >= tagged_at):
            return False                                   # 태깅이 없거나 이미 반영됨
        attempts = int(game.get("retitle_attempts") or 0)
        if attempts >= MAX_ATTEMPTS:
            return False

        kind, opponent = game.get("kind"), game.get("opponent") or ""
        name = naming.game_name(date, label, kind, opponent)
        file_name = naming.game_name(date, label, kind, opponent, for_file=True)
        yt_url = game.get("yt_url")
        if yt_url and name != game.get("yt_title"):
            if yt_url.startswith(DRYRUN_PREFIX):
                r.log(f"[{label}] (dry-run) YouTube 제목 변경: {game.get('yt_title')} → {name}")
            else:
                try:
                    yt_up.update_video_snippet(yt_url.rstrip("/").rsplit("/", 1)[-1], name,
                                               yt_up.description_first_line(name))
                except Exception as e:
                    r.log(f"[{label}] YouTube 제목 변경 실패: {e!r}")
                    self._transition(date, label, DONE_STATES, retitle_attempts=attempts + 1,
                                     retitle_error=str(e)[:300])
                    r._record("retitle_fail", date=date, label=label, reason=str(e)[:200])
                    return True

        warnings: list[str] = []
        self._transition(date, label, DONE_STATES, yt_title=name if yt_url else game.get("yt_title"),
                         retitled_at=iso(r.now()), retitle_error=None)
        if yt_url:
            self._update_day(date, label, name=name)
        clips_doc = r.store.read_json(clips_file(date, label))
        if clips_doc:
            self._rename_originals(date, label, clips_doc, file_name, warnings)
        for w in warnings:
            r.log(f"[{label}] {w}")
        r.log(f"[{label}] 제목 반영: {name}")
        return True
