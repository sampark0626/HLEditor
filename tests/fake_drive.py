"""DriveStore·make_kaggle_secrets 시험용 가짜 Drive v3 서비스와 인증 세션.

실제 Drive API처럼 검색어(q)를 해석해 파일을 거르므로, 우리 코드가 만드는 검색어가
맞는지도 함께 검증된다.
"""

import hashlib
import itertools
import json
import re
from types import SimpleNamespace

from googleapiclient.errors import HttpError

FOLDER = "application/vnd.google-apps.folder"


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class _ResumableReq:
    def __init__(self, fn):
        self._fn = fn

    def next_chunk(self, num_retries=0):
        return None, self._fn()


def _read_media(media):
    return media.getbytes(0, media.size())


class FakeDrive:
    def __init__(self, page_size=None):
        self.items = {}
        self._ids = (f"id{n}" for n in itertools.count(1))
        self.page_size = page_size

    # 시험 준비용 ------------------------------------------------------------
    def add(self, name, parent, *, mime="video/mp4", content=b"", created="2026-09-27T03:00:00.000Z",
            trashed=False):
        fid = next(self._ids)
        self.items[fid] = {"id": fid, "name": name, "parents": [parent], "mimeType": mime,
                           "content": content, "createdTime": created, "trashed": trashed}
        return fid

    def folder_named(self, name, parent):
        return next(f["id"] for f in self.items.values()
                    if f["name"] == name and parent in f["parents"] and f["mimeType"] == FOLDER)

    def children(self, parent):
        return sorted(f["name"] for f in self.items.values()
                      if parent in f["parents"] and not f["trashed"] and f["mimeType"] != FOLDER)

    # Drive v3 흉내 -----------------------------------------------------------
    def files(self):
        return self

    @staticmethod
    def _public(f):
        return {"id": f["id"], "name": f["name"], "mimeType": f["mimeType"],
                "size": str(len(f["content"])), "createdTime": f["createdTime"],
                "md5Checksum": hashlib.md5(f["content"]).hexdigest()}

    def list(self, q, fields=None, pageSize=100, pageToken=None, orderBy=None):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        items = [f for f in self.items.values() if parent in f["parents"]]
        m = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        if m:
            name = m.group(1).replace("\\'", "'").replace("\\\\", "\\")
            items = [f for f in items if f["name"] == name]
        if f"mimeType = '{FOLDER}'" in q:
            items = [f for f in items if f["mimeType"] == FOLDER]
        if f"mimeType != '{FOLDER}'" in q:
            items = [f for f in items if f["mimeType"] != FOLDER]
        if "trashed = false" in q:
            items = [f for f in items if not f["trashed"]]
        size = self.page_size or pageSize
        start = int(pageToken or 0)
        page, more = items[start:start + size], start + size < len(items)

        def run():
            res = {"files": [self._public(f) for f in page]}
            if more:
                res["nextPageToken"] = str(start + size)
            return res
        return _Req(run)

    def get(self, fileId, fields=None):
        def run():
            if fileId not in self.items:
                raise HttpError(SimpleNamespace(status=404, reason="Not Found"), b"not found")
            f = self.items[fileId]
            return {"parents": list(f["parents"]), "trashed": f["trashed"]}
        return _Req(run)

    def update(self, fileId, addParents=None, removeParents=None, body=None, media_body=None,
               fields=None):
        def run():
            f = self.items[fileId]
            if removeParents:
                f["parents"].remove(removeParents)
            if addParents:
                f["parents"].append(addParents)
            if body:
                f.update(body)
            if media_body is not None:
                f["content"] = _read_media(media_body)
            return {"id": fileId, "parents": f["parents"]}
        return _Req(run)

    def create(self, body, fields=None, media_body=None):
        def run():
            content = _read_media(media_body) if media_body is not None else b""
            fid = self.add(body["name"], body["parents"][0], mime=body.get("mimeType", "video/mp4"),
                           content=content)
            return {"id": fid, "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}
        if media_body is not None and media_body.resumable():
            return _ResumableReq(run)
        return _Req(run)


class _Resp:
    def __init__(self, content, status, fail_after=None):
        self._content = content
        self.status_code = status
        self._fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        sent = 0
        for i in range(0, len(self._content), chunk_size):
            chunk = self._content[i:i + chunk_size]
            if self._fail_after is not None and sent + len(chunk) > self._fail_after:
                yield chunk[: self._fail_after - sent]
                raise ConnectionError("연결 끊김(시험)")
            sent += len(chunk)
            yield chunk

    def json(self):
        return json.loads(self._content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


class FakeSession:
    """AuthorizedSession 흉내 — alt=media 다운로드와 Range 이어받기를 지원한다."""

    def __init__(self, drive, fail_after=None, ignore_range=False):
        self.drive = drive
        self.fail_after = list(fail_after or [])   # 요청마다 이 바이트 수 뒤에 끊는다(한 번씩 소비)
        self.ignore_range = ignore_range
        self.requests = []

    def get(self, url, headers=None, stream=False, timeout=None):
        fid = re.search(r"/files/([^?]+)\?alt=media", url).group(1)
        content = self.drive.items[fid]["content"]
        rng = (headers or {}).get("Range")
        self.requests.append(rng)
        start = int(re.match(r"bytes=(\d+)-", rng).group(1)) if rng and not self.ignore_range else 0
        fail = self.fail_after.pop(0) if self.fail_after else None
        return _Resp(content[start:], 206 if (rng and not self.ignore_range) else 200, fail)
