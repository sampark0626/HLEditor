#!/usr/bin/env python3
"""
HLEditor 인박스 러너 — 폰에서 Drive에 올린 경기 영상을 Kaggle에서 자동 처리한다.

흐름 (MOBILE_PLAN.md 3-2):
  01_inbox의 영상을 가져와(claim) 경기 단위로 묶고(30분 분할 파트 병합)
  → 오디오 후보 → [팬 분석 ∥ Gemini 판별] → 채택 → 빌드(워터마크) → YouTube 업로드
  → ntfy 알림(그날 BAND 글 전체, 탭하면 복사) → 원본은 03_done.
  2D 요청(01_inbox_2d)은 GPU 실행에서만 처리하고 결과는 Drive 05_output에 올린다.

실행:
  Kaggle — Apps Script가 kernel_bootstrap.py를 push하면 main([])이 불린다.
           비밀값은 비공개 데이터셋의 /kaggle/input/**/hl_secrets.json 에서 읽는다.
  PC 로컬 시험 — python -m kaggle_runner.inbox_runner --local-inbox DIR --dry-run [--no-vision]
           DIR 아래 01_inbox 등 폴더가 만들어진다. 영상을 01_inbox에 넣고 실행한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):  # python kaggle_runner/inbox_runner.py 로 직접 실행한 경우
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import band_poster  # noqa: E402
import config  # noqa: E402
import soccer_highlights as sh  # noqa: E402
import youtube_uploader as yt_up  # noqa: E402
from kaggle_runner import grouping  # noqa: E402
from kaggle_runner.analysis import analyze_candidates  # noqa: E402
from kaggle_runner.notify import Notifier  # noqa: E402
from kaggle_runner.stores import DriveStore, LocalStore, RemoteFile  # noqa: E402

KST = timezone(timedelta(hours=9), "KST")   # 한국은 서머타임이 없어 고정 오프셋으로 충분
SECRETS_FILENAME = "hl_secrets.json"
REQUIRED_SECRETS = ("GEMINI_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                    "GOOGLE_REFRESH_TOKEN", "ROOT_FOLDER_ID", "NTFY_TOPIC")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
NANUM_FONT_URL = ("https://raw.githubusercontent.com/google/fonts/main/"
                  "ofl/nanumgothic/NanumGothic-Regular.ttf")
RUNTIME_PACKAGES = ["google-genai>=2.6,<3", "google-api-python-client>=2.100", "google-auth>=2.20"]

CLAIMS_FILE = "claims.json"
MAX_ATTEMPTS = 3                  # 처리 도중 커널이 죽어 되돌려진 횟수가 이만큼이면 실패로 보낸다
POLL_SEC = 30                     # 다음 파트·새 업로드를 기다릴 때 확인 간격
MAX_RUN_SEC = 10 * 3600           # Kaggle 세션 한도(12시간) 전에 안전하게 멈춘다
CLAIM_KEEP_DAYS = 60              # 끝난 claims 기록을 이만큼 지나면 정리
QUALITY_MIN_HEIGHT = 1000         # 이보다 낮으면 폰 업로드 중 압축된 것으로 본다(F4)
QUALITY_MIN_VIDEO_BPS = 4_000_000


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def kst_date(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y-%m-%d")


# ─── 설정·비밀값 ─────────────────────────────────────────────────────────────
@dataclass
class Settings:
    gemini_key: str | None
    default_title: str
    youtube_privacy: str = "public"
    sensitivity: str = "normal"
    quality: str = "balanced"
    retention_days: int = 7
    wait_next_part_sec: float = grouping.WAIT_NEXT_PART_SEC
    settle_sec: float = grouping.SETTLE_SEC
    min_game_sec: float = 300.0
    dry_run: bool = False
    no_vision: bool = False
    max_segments: int | None = None   # 로컬 시험용 — 신호가 센 순으로 N개만 빌드
    once: bool = False                # 기다리지 않고 한 번만 돌고 끝낸다(로컬 시험용)


def find_secrets_file(root=Path("/kaggle/input")) -> Path | None:
    """마운트 깊이가 일정하지 않아 재귀 탐색한다(run_match.discover_match_videos 참고)."""
    root = Path(root)
    if not root.exists():
        return None
    return next(root.rglob(SECRETS_FILENAME), None)


def load_secrets(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_SECRETS if not data.get(k)]
    if missing:
        raise ValueError(f"{SECRETS_FILENAME}에 빠진 값: {', '.join(missing)}")
    return data


def mask(value: str | None) -> str:
    """로그에 비밀값을 남기지 않는다 — 앞 4글자만."""
    return f"{value[:4]}…" if value else "(없음)"


def settings_from(secrets: dict, args) -> Settings:
    def num(key, default, cast=float):
        try:
            return cast(secrets.get(key, default))
        except (TypeError, ValueError):
            return default

    s = Settings(
        gemini_key=secrets.get("GEMINI_API_KEY") or config.load_gemini_api_key(),
        default_title=secrets.get("DEFAULT_TITLE") or config.get_default_title(),
        youtube_privacy=secrets.get("YOUTUBE_PRIVACY") or config.get_youtube_privacy(),
        sensitivity=secrets.get("SENSITIVITY") or "normal",
        quality=secrets.get("QUALITY") or "balanced",
        retention_days=num("RETENTION_DAYS", 7, int),
        wait_next_part_sec=num("WAIT_NEXT_PART_MIN", grouping.WAIT_NEXT_PART_SEC / 60) * 60,
        settle_sec=num("SETTLE_SEC", grouping.SETTLE_SEC),
        min_game_sec=num("MIN_GAME_SEC", 300.0),
        dry_run=args.dry_run,
        no_vision=args.no_vision,
        max_segments=args.max_segments,
        once=args.once,
    )
    if args.settle_sec is not None:
        s.settle_sec = args.settle_sec
    if args.wait_part_min is not None:
        s.wait_next_part_sec = args.wait_part_min * 60
    if args.min_game_sec is not None:
        s.min_game_sec = args.min_game_sec
    if s.sensitivity not in sh.SENSITIVITY_PRESETS:
        s.sensitivity = "normal"
    if s.quality not in sh.QUALITY_PRESETS:
        s.quality = "balanced"
    return s


# ─── 영상 정보 ───────────────────────────────────────────────────────────────
@dataclass
class MediaInfo:
    duration: float
    width: int | None = None
    height: int | None = None
    video_bps: int | None = None
    start_utc: datetime | None = None


def parse_creation_time(value: str | None) -> datetime | None:
    """ffprobe의 creation_time 계열 태그 → aware UTC. 비어 있거나 기본값(1970/1904)이면 None.

    예: "2026-09-06T04:05:12.000000Z", "2026-09-06T13:05:12+0900"(애플 태그).
    """
    if not value:
        return None
    text = value.strip().replace(" ", "T", 1).replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if dt.year < 2000:
        return None
    return dt.astimezone(UTC)


def parse_probe(data: dict) -> MediaInfo:
    fmt = data.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    video = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), {})
    vtags = {k.lower(): v for k, v in (video.get("tags") or {}).items()}

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    start = (parse_creation_time(tags.get("creation_time"))
             or parse_creation_time(tags.get("com.apple.quicktime.creationdate"))
             or parse_creation_time(vtags.get("creation_time")))
    return MediaInfo(duration=float(fmt.get("duration") or 0.0),
                     width=as_int(video.get("width")), height=as_int(video.get("height")),
                     video_bps=as_int(video.get("bit_rate")), start_utc=start)


def probe_media(path) -> MediaInfo:
    out = sh.run(["ffprobe", "-v", "error", "-print_format", "json",
                  "-show_format", "-show_streams", str(path)])
    return parse_probe(json.loads(out))


def quality_warning(info: MediaInfo) -> str | None:
    hint = "Drive 앱의 '업로드 → 사진 및 동영상'으로 올렸는지 확인하세요."
    if info.height and info.height < QUALITY_MIN_HEIGHT:
        return f"원본 해상도가 {info.height}p라 폰에서 압축된 것 같습니다. {hint}"
    if info.video_bps and info.video_bps < QUALITY_MIN_VIDEO_BPS:
        return f"원본 화질이 {info.video_bps / 1e6:.1f}Mbps로 낮아 압축된 것 같습니다. {hint}"
    return None


# ─── 날짜별 게시 목록(_state/day_YYYY-MM-DD.json) ───────────────────────────
def day_file(date_str: str) -> str:
    return f"day_{date_str}.json"


def next_label(day: dict) -> str:
    """그날 처리한 경기 수 + 1 — "하이라이트 없음" 경기도 번호를 차지한다."""
    return f"{len(day.get('games', [])) + 1}경기"


def band_text(day: dict) -> str:
    links = [(g["label"], g["yt_url"]) for g in day.get("games", []) if g.get("yt_url")]
    return band_poster.format_post_content(links, match_date=day["date"])


# ─── 런타임 준비 (Kaggle) ────────────────────────────────────────────────────
def ensure_runtime_deps() -> None:
    """Kaggle 기본 이미지에 없거나 오래된 패키지만 설치한다(이미 맞으면 pip가 건너뛴다)."""
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *RUNTIME_PACKAGES], check=False)


def ensure_title_font(workdir: Path) -> str | None:
    """워터마크용 한글 폰트를 준비한다. 없으면 나눔고딕(OFL)을 받아 쓴다. 실패하면 워터마크만 생략."""
    if os.path.exists(sh.TITLE_FONT):
        return sh.TITLE_FONT
    dest = Path(workdir) / "NanumGothic-Regular.ttf"
    try:
        if not dest.exists():
            import requests
            r = requests.get(NANUM_FONT_URL, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
        sh.TITLE_FONT = str(dest)
        os.environ["HL_TITLE_FONT"] = str(dest)
        return str(dest)
    except Exception as e:
        log(f"워터마크 폰트 준비 실패 — 워터마크 없이 진행: {e!r}")
        return None


def google_credentials(secrets: dict):
    """refresh token으로 자격 증명을 만들고 바로 갱신해 본다(인증이 죽었으면 여기서 실패)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token=None,
        refresh_token=secrets["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=secrets["GOOGLE_CLIENT_ID"],
        client_secret=secrets["GOOGLE_CLIENT_SECRET"],
        scopes=GOOGLE_SCOPES,
    )
    creds.refresh(Request())
    return creds


def prepare_youtube_token(creds, workdir: Path) -> Path:
    """youtube_uploader가 읽는 형식(_save_token과 동일)으로 토큰 파일을 임시 경로에 만든다."""
    path = Path(workdir) / "youtube_token.json"
    expiry = getattr(creds, "expiry", None)
    path.write_text(json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or GOOGLE_SCOPES),
        "expiry": expiry.isoformat() if isinstance(expiry, datetime) else None,
    }), encoding="utf-8")
    yt_up.TOKEN_FILE = path
    return path


def gpu_available() -> bool:
    if not shutil.which("nvidia-smi"):
        return False
    try:
        return subprocess.run(["nvidia-smi"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def is_auth_error(exc: BaseException) -> bool:
    """Google 자격 증명이 죽은 경우(refresh token 만료·취소)인지."""
    try:
        from google.auth.exceptions import RefreshError
        if isinstance(exc, RefreshError):
            return True
    except ImportError:
        pass
    return "invalid_grant" in str(exc)


AUTH_HELP = ("PC에서 `python tools/make_kaggle_secrets.py`를 다시 실행한 뒤, "
             "Kaggle 데이터셋 hl-secrets에 새 hl_secrets.json을 새 버전으로 올려 주세요.")


# ─── 러너 ────────────────────────────────────────────────────────────────────
@dataclass
class LocalPart:
    remote: RemoteFile
    source: str
    path: Path
    info: MediaInfo
    meta: grouping.FileMeta


class InboxRunner:
    def __init__(self, store, notifier: Notifier, settings: Settings, *, workdir,
                 gpu: bool = False, now=utcnow, sleep=time.sleep, log=log):
        self.store = store
        self.notify = notifier
        self.s = settings
        self.workdir = Path(workdir)
        self.gpu = gpu
        self.now = now
        self.sleep = sleep
        self.log = log
        self.claims: dict = store.read_json(CLAIMS_FILE) or {}
        self.local: dict[str, LocalPart] = {}
        self.summary: dict = {"started_at": iso(now()), "gpu": gpu,
                              "games": [], "failed": [], "released": []}

    # 상태 파일 --------------------------------------------------------------
    def _save_claims(self) -> None:
        self.store.write_json(CLAIMS_FILE, self.claims)

    def _set_claim(self, rf: RemoteFile, **fields) -> None:
        entry = self.claims.setdefault(rf.id, {"name": rf.name})
        entry.update(fields, updated_at=iso(self.now()))
        self._save_claims()

    # 실행 루프 --------------------------------------------------------------
    def run(self) -> dict:
        self.recover_processing()
        self.cleanup()
        deadline = self.now() + timedelta(seconds=MAX_RUN_SEC)
        while True:
            claimed = self.claim_new()
            ready, waiting = grouping.group_parts(
                [p.meta for p in self.local.values()], self.now(),
                settle_sec=self.s.settle_sec, wait_next_part_sec=self.s.wait_next_part_sec)
            for group in ready:
                self.process_group(group)
            if ready:
                continue          # 처리하는 동안 새 영상이 왔을 수 있으니 바로 다시 확인한다
            if not waiting and not claimed:
                break             # 인박스가 비었고 기다리는 경기도 없다
            if self.s.once or self.now() >= deadline:
                self.release_waiting()
                break
            self.sleep(POLL_SEC)
        self.summary["finished_at"] = iso(self.now())
        return self.summary

    def recover_processing(self) -> None:
        """지난 실행이 죽고 02_processing에 남긴 파일을 원래 폴더로 되돌린다."""
        for rf in self.store.list("processing"):
            source = self.claims.get(rf.id, {}).get("source", "inbox")
            if self.store.move(rf, "processing", source):
                self.log(f"지난 실행에서 남은 파일을 되돌림: {rf.name} → {source}")
                self._set_claim(rf, source=source, status="recovered")

    def cleanup(self) -> None:
        """보관 기간이 지난 03_done 원본을 휴지통으로 보내고(영구 삭제 아님) 오래된 기록을 정리한다."""
        now = self.now()
        keep = timedelta(days=self.s.retention_days)
        for rf in self.store.list("done"):
            done_at = from_iso(self.claims.get(rf.id, {}).get("done_at")) or rf.created_utc
            if now - done_at >= keep:
                self.store.trash(rf, "done")
                self.claims.pop(rf.id, None)
                self.log(f"보관 기간 지남 → 휴지통: {rf.name}")
        stale = [fid for fid, e in self.claims.items()
                 if e.get("status") in ("done", "failed")
                 and (from_iso(e.get("updated_at")) or now) < now - timedelta(days=CLAIM_KEEP_DAYS)]
        for fid in stale:
            self.claims.pop(fid, None)
        self._save_claims()

    def claim_new(self) -> list[str]:
        keys = ["inbox"] + (["inbox_2d"] if self.gpu else [])
        claimed = []
        for key in keys:
            for rf in self.store.list(key):
                prev = self.claims.get(rf.id, {})
                # 처리 도중 죽어서 되돌려진 파일만 시도 횟수를 누적한다.
                # 사용자가 04_failed에서 다시 넣은 파일은 새로 1회부터 센다.
                crashed = prev.get("status") in ("processing", "recovered")
                attempts = int(prev.get("attempts", 0)) + 1 if crashed else 1
                if not self.store.move(rf, key, "processing"):
                    continue
                self._set_claim(rf, source=key, attempts=attempts, status="processing")
                claimed.append(rf.id)
                if attempts > MAX_ATTEMPTS:
                    self._fail([rf], f"{MAX_ATTEMPTS}번 처리하다 중간에 멈춰서 더 이상 "
                                     "자동으로 처리하지 않습니다. Kaggle 로그를 확인해 주세요.")
                    continue
                try:
                    path = self.store.download(rf, self.workdir / "in" / _safe_name(rf))
                    info = probe_media(path)
                except Exception as e:
                    if is_auth_error(e):
                        raise
                    self._fail([rf], f"영상을 받거나 읽지 못했습니다: {e}")
                    continue
                meta = grouping.FileMeta(id=rf.id, name=rf.name, duration=info.duration,
                                         start_utc=info.start_utc, arrived_utc=rf.created_utc,
                                         source=key)
                self.local[rf.id] = LocalPart(remote=rf, source=key, path=path, info=info, meta=meta)
                self.log(f"받음: {rf.name} ({info.duration / 60:.1f}분, {key})")
        return claimed

    def release_waiting(self) -> None:
        """기다리던 파트를 원래 폴더로 되돌린다 — 다음 실행이 이어서 처리한다."""
        for part in list(self.local.values()):
            if self.store.move(part.remote, "processing", part.source):
                self._set_claim(part.remote, status="released")
                self.summary["released"].append(part.remote.name)
            self._drop_local(part)

    # 경기 처리 --------------------------------------------------------------
    def process_group(self, group: grouping.Group) -> None:
        parts = [self.local[f.id] for f in group.files]
        names = ", ".join(p.remote.name for p in parts)
        game_dir = self.workdir / f"game_{uuid.uuid4().hex[:8]}"
        game_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._process_game(group, parts, game_dir)
        except Exception as e:
            if is_auth_error(e):
                raise
            self.log(f"처리 실패 ({names}): {e!r}")
            self._fail([p.remote for p in parts], f"처리 중 오류가 났습니다: {e}")
        finally:
            shutil.rmtree(game_dir, ignore_errors=True)
            for p in parts:
                self._drop_local(p)

    def _process_game(self, group: grouping.Group, parts: list[LocalPart], game_dir: Path) -> None:
        match_date = kst_date(group.start_utc or group.files[0].arrived_utc)
        warnings = list(dict.fromkeys(w for w in (quality_warning(p.info) for p in parts) if w))
        if group.timed_out:
            warnings.append("다음 파트가 오지 않아 도착한 부분만 처리했습니다.")
        if len(parts) > 1:
            warnings.append(f"나뉜 파일 {len(parts)}개를 한 경기로 합쳤습니다.")

        if group.duration < self.s.min_game_sec:
            self._fail([p.remote for p in parts],
                       f"영상이 {group.duration / 60:.1f}분으로 너무 짧아 처리하지 않았습니다 "
                       f"(최소 {self.s.min_game_sec / 60:.0f}분).")
            return

        if len(parts) == 1:
            video = parts[0].path
        else:
            video = Path(sh.concat_videos([p.path for p in parts], game_dir / "merged.mp4",
                                          workdir=game_dir))
        day = self.store.read_json(day_file(match_date)) or {"date": match_date, "games": []}
        label = next_label(day)
        self.log(f"[{label}] 처리 시작 — {', '.join(p.remote.name for p in parts)} "
                 f"({group.duration / 60:.1f}분, {match_date})")

        preset = sh.SENSITIVITY_PRESETS[self.s.sensitivity]
        cands = sh.detect_spikes(str(video), game_dir, percentile=preset["percentile"],
                                 min_db=preset["min_db"])
        gemini_key = None if self.s.no_vision else self.s.gemini_key
        info = analyze_candidates(video, game_dir, cands, gemini_key=gemini_key, log=self.log)
        selected, maybe = sh.select_segments(cands, sh.CONF_AUTO, info["vision_used"])
        if self.s.max_segments:
            strongest = sorted(selected, key=lambda c: c.get("delta_db", 0), reverse=True)
            selected = sorted(strongest[: self.s.max_segments], key=lambda c: c["peak"])
        self.log(f"[{label}] 후보 {len(cands)}개 → 채택 {len(selected)}개 (확인필요 {len(maybe)}개), "
                 f"팬 {info['pan']}, 비용 ${(info['usage'] or {}).get('cost_usd', 0)}")
        self._save_results(match_date, label, video, group, cands, info)

        record = {"label": label, "yt_url": None, "sources": [p.remote.name for p in parts],
                  "start_kst": group.start_utc.astimezone(KST).isoformat(timespec="minutes")
                  if group.start_utc else None,
                  "processed_at": iso(self.now())}
        if not selected:
            day["games"].append(record)
            self.store.write_json(day_file(match_date), day)
            self.notify.send(f"{label}: 하이라이트 없음",
                             f"채택된 장면이 없어 YouTube에 올리지 않았습니다 "
                             f"(후보 {len(cands)}개, 확인필요 {len(maybe)}개). "
                             "원본은 HLEditor/03_done에 있습니다.")
            self._done(parts)
            self.summary["games"].append({"label": label, "date": match_date, "yt_url": None})
            return

        out_mp4 = game_dir / f"highlight_{match_date}_{label}.mp4"
        quality = sh.QUALITY_PRESETS[self.s.quality]
        sh.build_output(str(video), selected, out_mp4, game_dir, title=self.s.default_title,
                        preset=quality.get("preset"), crf=quality.get("crf"),
                        copy_mode=bool(quality.get("copy")))
        selected_ids = {id(c) for c in selected}
        approved = [i for i, c in enumerate(cands) if id(c) in selected_ids]
        title = yt_up.make_title(self.s.default_title, label, match_date)
        yt_url, yt_error = self._publish(out_mp4, video, cands, approved, label, title)

        if yt_url:
            record["yt_url"] = yt_url
            day["games"].append(record)
            self.store.write_json(day_file(match_date), day)
            played = sum(1 for g in day["games"] if g.get("yt_url"))
            message = band_text(day)
            if warnings:
                message += "\n\n" + "\n".join(f"※ {w}" for w in warnings)
            self.notify.send(f"{label} 하이라이트 완료 (오늘 {played}개)", message, tags=["soccer"])
            if self.s.dry_run:   # 로컬 시험: 결과 영상을 05_output에 남겨 직접 확인할 수 있게
                self.store.upload(out_mp4, "output", out_mp4.name)
        else:
            link = self.store.upload(out_mp4, "output", out_mp4.name)
            record["backup_link"] = link
            day["games"].append(record)
            self.store.write_json(day_file(match_date), day)
            help_text = f"\n{AUTH_HELP}" if "인증" in (yt_error or "") else ""
            self.notify.send(f"{label} YouTube 업로드 실패",
                             f"하이라이트는 만들었지만 YouTube에 올리지 못했습니다: {yt_error}\n"
                             f"영상은 Drive HLEditor/05_output에 올려 두었습니다.{help_text}",
                             tags=["warning"], click=link)
        self._done(parts)
        self.summary["games"].append({"label": label, "date": match_date, "yt_url": yt_url,
                                      "error": yt_error})

        if group.wants_2d:
            self._run_2d(video, selected, out_mp4, label, match_date, game_dir)

    def _publish(self, out_mp4, video, cands, approved, label, title):
        if self.s.dry_run:
            return f"https://youtu.be/DRYRUN-{label}", None
        try:
            url = yt_up.publish_highlight(str(out_mp4), str(video), cands, approved, label, title,
                                          privacy=self.s.youtube_privacy,
                                          pre_sec=sh.PRE_SEC, post_sec=sh.POST_SEC)
            return url, None
        except Exception as e:
            self.log(f"[{label}] YouTube 업로드 실패: {e!r}")
            return None, str(e)[:300]

    def _save_results(self, match_date, label, video, group, cands, info) -> None:
        """판별 결과를 _state에 남긴다 — 나중에 PC에서 검토하거나 2D만 다시 돌릴 때 쓴다."""
        payload = {
            "date": match_date, "label": label, "sources": [f.name for f in group.files],
            "duration": group.duration, "vision_used": info["vision_used"],
            "usage": info["usage"], "pan": info["pan"],
            "params": {"PRE_SEC": sh.PRE_SEC, "POST_SEC": sh.POST_SEC,
                       "SENSITIVITY": self.s.sensitivity, "CONF_AUTO": sh.CONF_AUTO,
                       "VISION_MODEL": sh.VISION_MODEL if info["vision_used"] else None},
            "candidates": cands,
        }
        try:
            self.store.write_json(f"results_{match_date}_{label}.json",
                                  json.loads(json.dumps(payload, default=float)))
        except Exception as e:
            if is_auth_error(e):
                raise
            self.log(f"[{label}] 결과 JSON 저장 실패(처리는 계속): {e!r}")

    def _run_2d(self, video, selected, hl_out, label, match_date, game_dir) -> None:
        """2D(Dot Play) 변환 — GPU 실행에서만. 실패해도 하이라이트는 이미 전달됐으므로 알림만."""
        if not self.gpu:
            return
        try:
            from kaggle_runner import run_match as rm
            rm.install_packages(rm.CV_PACKAGES)
            # 검출 가중치(수백 MB)를 커널 Output(/kaggle/working) 밖에 받고, 같은 실행의 다음 경기에서 재사용한다
            rm.WORK_DIR = self.workdir
            from dotplay.config import PipelineConfig
            from dotplay.device import resolve_device
            from dotplay.pipeline import run_radar_segments

            device = resolve_device("auto")
            cfg = PipelineConfig(device=device, stride=rm.STRIDE)
            models = rm.resolve_models(device)
            merged, _ = sh.get_merged_timeline(selected, sh.PRE_SEC, sh.POST_SEC)
            segments = [(c["start"], c["end"]) for c in merged]
            radar = game_dir / "radar.mp4"
            run_radar_segments(str(video), segments, models, cfg, device, out_video=str(radar))
            final = game_dir / f"highlight_2d_{match_date}_{label}.mp4"
            rm.composite_pip(hl_out, radar, final, sh.run)
            link = self.store.upload(final, "output", final.name)
            self.notify.send(f"{label} 2D 변환 완료", "2D 버드뷰를 얹은 하이라이트를 Drive "
                             "HLEditor/05_output에 올렸습니다.", tags=["soccer"], click=link)
        except Exception as e:
            if is_auth_error(e):
                raise
            self.log(f"[{label}] 2D 변환 실패: {e!r}")
            self.notify.send(f"{label} 2D 변환 실패", f"하이라이트는 정상 처리됐고 2D 변환만 "
                             f"실패했습니다: {str(e)[:200]}", tags=["warning"])

    # 상태 전이 --------------------------------------------------------------
    def _done(self, parts: list[LocalPart]) -> None:
        for p in parts:
            self.store.move(p.remote, "processing", "done")
            self._set_claim(p.remote, status="done", done_at=iso(self.now()))

    def _fail(self, files: list[RemoteFile], reason: str) -> None:
        for rf in files:
            self.store.move(rf, "processing", "failed")
            self._set_claim(rf, status="failed", reason=reason[:300])
        names = ", ".join(rf.name for rf in files)
        self.summary["failed"].append({"files": names, "reason": reason})
        self.notify.send(f"처리 실패: {names}",
                         f"{reason}\n원본은 HLEditor/04_failed에 있습니다. "
                         "01_inbox로 옮기면 다시 처리합니다.", tags=["warning"])

    def _drop_local(self, part: LocalPart) -> None:
        self.local.pop(part.remote.id, None)
        try:
            part.path.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_name(rf: RemoteFile) -> str:
    return f"{rf.id[:16]}_{re.sub(r'[^0-9A-Za-z가-힣._-]', '_', rf.name)}"


# ─── 진입점 ──────────────────────────────────────────────────────────────────
def _notify_fatal(notifier: Notifier, exc: BaseException) -> None:
    """실행 전체가 멈추는 오류를 폰으로 알린다. 경기 단위 실패는 _fail이 따로 알린다."""
    if is_auth_error(exc):
        notifier.send("Google 인증 만료",
                      f"Drive·YouTube 인증이 만료되어 처리하지 못했습니다.\n{AUTH_HELP}",
                      tags=["warning"])
    else:
        notifier.send("HLEditor 처리기 오류",
                      f"처리기가 멈췄습니다: {str(exc)[:300]}\n"
                      "Kaggle 노트북 hleditor-inbox의 로그를 확인해 주세요. "
                      "올린 영상은 다음 실행 때 다시 처리합니다.", tags=["warning"])


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="HLEditor 인박스 러너 (폰 업로드 → 자동 하이라이트)")
    ap.add_argument("--local-inbox", help="Drive 대신 이 로컬 폴더를 인박스 저장소로 쓴다(시험용)")
    ap.add_argument("--secrets", help=f"{SECRETS_FILENAME} 경로(기본: /kaggle/input 아래 자동 탐색)")
    ap.add_argument("--dry-run", action="store_true", help="YouTube 업로드·ntfy 발송 대신 콘솔 출력")
    ap.add_argument("--no-vision", action="store_true", help="Gemini 판별 생략(오디오 후보 전체 채택)")
    ap.add_argument("--max-segments", type=int, help="신호가 센 순으로 N개 구간만 빌드(시험용)")
    ap.add_argument("--settle-sec", type=float, help="도착 후 대기 시간(초) 재정의")
    ap.add_argument("--wait-part-min", type=float, help="다음 파트 대기 시간(분) 재정의")
    ap.add_argument("--min-game-sec", type=float, help="처리할 최소 경기 길이(초) 재정의")
    ap.add_argument("--once", action="store_true", help="기다리지 않고 한 번만 확인하고 끝낸다")
    ap.add_argument("--workdir", help="임시 작업 폴더(기본: 시스템 임시 폴더/hl_inbox)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    workdir = Path(args.workdir or tempfile.gettempdir()) / "hl_inbox"
    workdir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(workdir).free / 1e9
    log(f"인박스 러너 시작 — 작업 폴더 {workdir} (여유 {free_gb:.0f}GB)")

    if args.local_inbox:
        secrets = load_secrets(args.secrets) if args.secrets else {}
        settings = settings_from(secrets, args)
        notifier = Notifier(secrets.get("NTFY_TOPIC", ""), dry_run=args.dry_run, log=log)
        store = LocalStore(args.local_inbox)
        gpu = False
        summary_dir = Path(args.local_inbox)
    else:
        ensure_runtime_deps()
        secrets_path = Path(args.secrets) if args.secrets else find_secrets_file()
        if not secrets_path:
            log(f"{SECRETS_FILENAME}를 찾지 못했습니다 — Kaggle 데이터셋 hl-secrets가 "
                "커널에 붙어 있는지 확인하세요.")
            return 2
        secrets = load_secrets(secrets_path)
        settings = settings_from(secrets, args)
        notifier = Notifier(secrets["NTFY_TOPIC"], dry_run=args.dry_run, log=log)
        log(f"비밀값 로드: Gemini {mask(secrets['GEMINI_API_KEY'])}, "
            f"루트 폴더 {mask(secrets['ROOT_FOLDER_ID'])}")
        try:
            creds = google_credentials(secrets)
            prepare_youtube_token(creds, workdir)
            store = DriveStore(creds, secrets["ROOT_FOLDER_ID"])
        except Exception as e:
            _notify_fatal(notifier, e)
            raise
        ensure_title_font(workdir)
        gpu = gpu_available()
        summary_dir = Path("/kaggle/working") if Path("/kaggle/working").exists() else workdir

    runner = InboxRunner(store, notifier, settings, workdir=workdir, gpu=gpu)
    try:
        summary = runner.run()
    except Exception as e:
        _notify_fatal(notifier, e)
        raise
    summary["notifications"] = len(notifier.sent)
    (summary_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"끝 — 경기 {len(summary['games'])}개, 실패 {len(summary['failed'])}건, "
        f"되돌림 {len(summary['released'])}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
