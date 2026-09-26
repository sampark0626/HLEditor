"""인박스 저장소 — Google Drive(Kaggle 실행용)와 로컬 폴더(테스트·PC 실행용).

두 저장소는 같은 폴더 구조와 같은 메서드를 가진다(MOBILE_PLAN.md 3-1).
러너는 폴더 키로만 말하고, 실제 이름·ID 해석은 저장소가 맡는다.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FOLDERS = {
    "inbox": "01_inbox",
    "inbox_2d": "01_inbox_2d",
    "processing": "02_processing",
    "done": "03_done",
    "failed": "04_failed",
    "output": "05_output",
    "state": "_state",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"


@dataclass(frozen=True)
class RemoteFile:
    id: str
    name: str
    size: int
    created_utc: datetime        # 저장소에 도착한 시각 (aware UTC)
    md5: str | None = None


def is_video(name: str, mime: str | None = None) -> bool:
    return bool(mime and mime.startswith("video/")) or Path(name).suffix.lower() in VIDEO_EXTS


def parse_rfc3339(value: str) -> datetime:
    """Drive의 createdTime("2026-09-27T05:12:33.123Z") → aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── 로컬 폴더 저장소 ───────────────────────────────────────────────────────
class LocalStore:
    """Drive와 같은 폴더 구조를 로컬 디스크에 둔다. 파일 id는 파일명이다."""

    def __init__(self, root):
        self.root = Path(root)
        for name in FOLDERS.values():
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _dir(self, key: str) -> Path:
        return self.root / FOLDERS[key]

    def list(self, key: str) -> list[RemoteFile]:
        out = []
        for p in sorted(self._dir(key).iterdir()):
            if p.is_file() and is_video(p.name):
                st = p.stat()
                out.append(RemoteFile(id=p.name, name=p.name, size=st.st_size,
                                      created_utc=datetime.fromtimestamp(st.st_mtime, UTC)))
        return out

    def move(self, f: RemoteFile, src: str, dst: str) -> bool:
        path = self._dir(src) / f.id
        if not path.exists():
            return False
        shutil.move(str(path), str(self._dir(dst) / f.id))
        return True

    def download(self, f: RemoteFile, dest) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        for key in ("processing", "inbox", "inbox_2d", "done", "failed"):
            path = self._dir(key) / f.id
            if path.exists():
                shutil.copyfile(path, dest)
                return dest
        raise FileNotFoundError(f"로컬 저장소에 파일 없음: {f.id}")

    def upload(self, local_path, key: str, name: str) -> str:
        dest = self._dir(key) / name
        shutil.copyfile(local_path, dest)
        return str(dest)

    def trash(self, f: RemoteFile, key: str) -> None:
        trash_dir = self.root / ".trash"
        trash_dir.mkdir(exist_ok=True)
        path = self._dir(key) / f.id
        if path.exists():
            shutil.move(str(path), str(trash_dir / f.id))

    def read_json(self, name: str) -> dict | None:
        path = self._dir("state") / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json(self, name: str, data: dict) -> None:
        path = self._dir("state") / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Google Drive 저장소 ────────────────────────────────────────────────────
def _q(value: str) -> str:
    """Drive 검색어 문자열 리터럴 이스케이프."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveStore:
    """Google Drive API v3 저장소.

    루트 폴더 ID 하나만 받고 하위 폴더는 이름으로 찾는다(없으면 만든다).
    service: googleapiclient Drive v3 리소스, session: 인증된 requests 세션(대용량 다운로드용).
    둘 다 주입할 수 있어 가짜 객체로 테스트한다.
    """

    DOWNLOAD_RETRIES = 5
    CHUNK = 8 << 20

    def __init__(self, creds, root_id: str, *, service=None, session=None, sleep=time.sleep):
        if service is None:
            from googleapiclient.discovery import build
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            session = AuthorizedSession(creds)
        self._svc = service
        self._session = session
        self._sleep = sleep
        self.root_id = root_id
        self._ids: dict[str, str] = {}
        self._resolve_folders()

    # 폴더 ------------------------------------------------------------------
    def _resolve_folders(self) -> None:
        q = (f"'{_q(self.root_id)}' in parents and mimeType = '{FOLDER_MIME}' "
             "and trashed = false")
        res = self._svc.files().list(q=q, fields="files(id, name)", pageSize=100).execute(num_retries=3)
        by_name = {f["name"]: f["id"] for f in res.get("files", [])}
        for key, name in FOLDERS.items():
            if name not in by_name:
                created = self._svc.files().create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [self.root_id]},
                    fields="id").execute(num_retries=3)
                by_name[name] = created["id"]
            self._ids[key] = by_name[name]

    def folder_id(self, key: str) -> str:
        return self._ids[key]

    # 목록·이동 --------------------------------------------------------------
    def list(self, key: str) -> list[RemoteFile]:
        q = (f"'{_q(self._ids[key])}' in parents and trashed = false "
             f"and mimeType != '{FOLDER_MIME}'")
        out, token = [], None
        while True:
            res = self._svc.files().list(
                q=q, pageSize=200, pageToken=token, orderBy="createdTime",
                fields="nextPageToken, files(id, name, size, createdTime, md5Checksum, mimeType)",
            ).execute(num_retries=3)
            for f in res.get("files", []):
                if is_video(f["name"], f.get("mimeType")):
                    out.append(RemoteFile(id=f["id"], name=f["name"], size=int(f.get("size") or 0),
                                          created_utc=parse_rfc3339(f["createdTime"]),
                                          md5=f.get("md5Checksum")))
            token = res.get("nextPageToken")
            if not token:
                return out

    def move(self, f: RemoteFile, src: str, dst: str) -> bool:
        """src 폴더에 있을 때만 dst로 옮긴다. 이미 다른 곳으로 옮겨졌으면 False."""
        from googleapiclient.errors import HttpError
        src_id, dst_id = self._ids[src], self._ids[dst]
        try:
            meta = self._svc.files().get(fileId=f.id, fields="parents, trashed").execute(num_retries=3)
        except HttpError as e:
            if getattr(e.resp, "status", None) == 404:
                return False
            raise
        if meta.get("trashed") or src_id not in (meta.get("parents") or []):
            return False
        self._svc.files().update(fileId=f.id, addParents=dst_id, removeParents=src_id,
                                 fields="id, parents").execute(num_retries=3)
        return True

    def trash(self, f: RemoteFile, key: str) -> None:
        self._svc.files().update(fileId=f.id, body={"trashed": True}).execute(num_retries=3)

    # 내려받기·올리기 --------------------------------------------------------
    def download(self, f: RemoteFile, dest) -> Path:
        """스트리밍으로 받는다. 끊기면 Range로 이어받고, 크기·md5를 검증한다."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.unlink(missing_ok=True)
        url = f"{DRIVE_FILES_URL}/{f.id}?alt=media"
        offset = 0
        for attempt in range(1, self.DOWNLOAD_RETRIES + 1):
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self._session.get(url, headers=headers, stream=True, timeout=(15, 120)) as r:
                    if r.status_code not in (200, 206):
                        raise OSError(f"HTTP {r.status_code}")
                    if offset and r.status_code == 200:   # 서버가 Range를 무시 → 처음부터 다시
                        offset = 0
                    with open(dest, "ab" if offset else "wb") as fh:
                        for chunk in r.iter_content(chunk_size=self.CHUNK):
                            fh.write(chunk)
                            offset += len(chunk)
                break
            except OSError:
                if attempt == self.DOWNLOAD_RETRIES:
                    raise
                self._sleep(min(60, 2 ** attempt))
        if f.size and dest.stat().st_size != f.size:
            raise OSError(f"다운로드 크기 불일치: {dest.stat().st_size} != {f.size}")
        if f.md5 and md5_file(dest) != f.md5:
            raise OSError("다운로드 md5 불일치")
        return dest

    def upload(self, local_path, key: str, name: str) -> str:
        """이어올리기(resumable)로 올리고 Drive 보기 링크를 돌려준다."""
        from googleapiclient.http import MediaFileUpload
        mime = "video/mp4" if str(name).lower().endswith(".mp4") else "application/octet-stream"
        media = MediaFileUpload(str(local_path), mimetype=mime, resumable=True, chunksize=32 << 20)
        req = self._svc.files().create(body={"name": name, "parents": [self._ids[key]]},
                                       media_body=media, fields="id, webViewLink")
        resp = None
        while resp is None:
            _, resp = req.next_chunk(num_retries=5)
        return resp.get("webViewLink") or f"https://drive.google.com/file/d/{resp['id']}/view"

    # _state JSON -----------------------------------------------------------
    def _find_state(self, name: str) -> str | None:
        q = f"'{_q(self._ids['state'])}' in parents and name = '{_q(name)}' and trashed = false"
        res = self._svc.files().list(q=q, fields="files(id)", pageSize=1).execute(num_retries=3)
        files = res.get("files", [])
        return files[0]["id"] if files else None

    def read_json(self, name: str) -> dict | None:
        fid = self._find_state(name)
        if not fid:
            return None
        r = self._session.get(f"{DRIVE_FILES_URL}/{fid}?alt=media", timeout=30)
        r.raise_for_status()
        return r.json()

    def write_json(self, name: str, data: dict) -> None:
        from googleapiclient.http import MediaIoBaseUpload
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(body), mimetype="application/json", resumable=False)
        fid = self._find_state(name)
        if fid:
            self._svc.files().update(fileId=fid, media_body=media).execute(num_retries=3)
        else:
            self._svc.files().create(body={"name": name, "parents": [self._ids["state"]]},
                                     media_body=media, fields="id").execute(num_retries=3)
