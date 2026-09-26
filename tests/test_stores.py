"""kaggle_runner.stores — Drive 저장소(가짜 Drive로)와 로컬 저장소."""

import os
from datetime import UTC, datetime

import pytest
from fake_drive import FOLDER, FakeDrive, FakeSession

from kaggle_runner.stores import FOLDERS, DriveStore, LocalStore, RemoteFile


def make_store(drive=None, **session_kw):
    drive = drive or FakeDrive()
    root = drive.add("HLEditor", "root", mime=FOLDER)
    store = DriveStore(None, root, service=drive, session=FakeSession(drive, **session_kw),
                       sleep=lambda s: None)
    return drive, store


def test_creates_missing_folders_and_reuses_existing():
    drive = FakeDrive()
    root = drive.add("HLEditor", "root", mime=FOLDER)
    existing = drive.add("01_inbox", root, mime=FOLDER)
    store = DriveStore(None, root, service=drive, session=FakeSession(drive))
    assert store.folder_id("inbox") == existing
    for key, name in FOLDERS.items():
        assert drive.folder_named(name, root) == store.folder_id(key)
    DriveStore(None, root, service=drive, session=FakeSession(drive))   # 두 번째는 새로 만들지 않음
    assert sum(1 for f in drive.items.values() if f["mimeType"] == FOLDER) == 1 + len(FOLDERS)


def test_list_returns_only_videos_with_arrival_time_and_pages():
    drive = FakeDrive(page_size=2)
    drive, store = make_store(drive)
    inbox = store.folder_id("inbox")
    drive.add("a.mp4", inbox, content=b"aa", created="2026-09-27T05:12:33.123Z")
    drive.add("b.MOV", inbox, mime="application/octet-stream")          # 확장자로 판별
    drive.add("c.mp4", inbox, trashed=True)
    drive.add("memo.txt", inbox, mime="text/plain")
    drive.add("sub", inbox, mime=FOLDER)
    drive.add("d.mp4", inbox)
    files = store.list("inbox")
    assert [f.name for f in files] == ["a.mp4", "b.MOV", "d.mp4"]
    assert files[0].size == 2 and files[0].md5
    assert files[0].created_utc == datetime(2026, 9, 27, 5, 12, 33, 123000, tzinfo=UTC)


def test_move_only_from_expected_folder():
    drive, store = make_store()
    fid = drive.add("a.mp4", store.folder_id("inbox"))
    rf = RemoteFile(id=fid, name="a.mp4", size=0, created_utc=datetime.now(UTC))
    assert store.move(rf, "inbox", "processing") is True
    assert drive.items[fid]["parents"] == [store.folder_id("processing")]
    assert store.move(rf, "inbox", "processing") is False          # 이미 옮겨짐
    gone = RemoteFile(id="missing", name="x.mp4", size=0, created_utc=datetime.now(UTC))
    assert store.move(gone, "inbox", "processing") is False        # 404


def _remote(drive, fid):
    f = drive._public(drive.items[fid])
    return RemoteFile(id=fid, name=f["name"], size=int(f["size"]),
                      created_utc=datetime.now(UTC), md5=f["md5Checksum"])


def test_download_resumes_with_range_after_disconnect(tmp_path):
    content = os.urandom(3 * (8 << 20) + 123)
    drive, store = make_store(fail_after=[(8 << 20) + 10])       # 첫 요청은 8MB+10바이트 뒤 끊김
    fid = drive.add("big.mp4", store.folder_id("processing"), content=content)
    dest = store.download(_remote(drive, fid), tmp_path / "big.mp4")
    assert dest.read_bytes() == content
    assert store._session.requests == [None, f"bytes={(8 << 20) + 10}-"]


def test_download_restarts_when_server_ignores_range(tmp_path):
    content = os.urandom(20 << 20)
    drive, store = make_store(fail_after=[5 << 20], ignore_range=True)
    fid = drive.add("big.mp4", store.folder_id("processing"), content=content)
    assert store.download(_remote(drive, fid), tmp_path / "big.mp4").read_bytes() == content


def test_download_rejects_corrupt_file(tmp_path):
    drive, store = make_store()
    fid = drive.add("a.mp4", store.folder_id("processing"), content=b"real")
    rf = _remote(drive, fid)
    drive.items[fid]["content"] = b"evil"                          # 크기는 같고 내용만 다름
    with pytest.raises(OSError, match="md5"):
        store.download(rf, tmp_path / "a.mp4")


def test_upload_returns_link_and_stores_content(tmp_path):
    drive, store = make_store()
    local = tmp_path / "highlight.mp4"
    local.write_bytes(b"mp4data")
    link = store.upload(local, "output", "highlight.mp4")
    assert link.startswith("https://drive.google.com/file/d/")
    assert drive.children(store.folder_id("output")) == ["highlight.mp4"]


def test_state_json_create_update_read():
    drive, store = make_store()
    assert store.read_json("claims.json") is None
    store.write_json("claims.json", {"a": {"status": "processing"}})
    store.write_json("claims.json", {"a": {"status": "done"}, "한글": 1})
    assert store.read_json("claims.json") == {"a": {"status": "done"}, "한글": 1}
    names = [f["name"] for f in drive.items.values() if store.folder_id("state") in f["parents"]]
    assert names == ["claims.json"]                                 # 새로 만들지 않고 덮어씀


def test_trash_hides_file_from_listing():
    drive, store = make_store()
    fid = drive.add("old.mp4", store.folder_id("done"))
    store.trash(_remote(drive, fid), "done")
    assert store.list("done") == [] and drive.items[fid]["trashed"] is True


def test_local_store_roundtrip(tmp_path):
    store = LocalStore(tmp_path)
    (tmp_path / "01_inbox" / "a.mp4").write_bytes(b"video")
    (tmp_path / "01_inbox" / "memo.txt").write_text("x")
    [rf] = store.list("inbox")
    assert rf.name == "a.mp4" and rf.size == 5
    assert store.move(rf, "inbox", "processing") and not store.move(rf, "inbox", "processing")
    assert store.download(rf, tmp_path / "dl" / "a.mp4").read_bytes() == b"video"
    link = store.upload(tmp_path / "dl" / "a.mp4", "output", "out.mp4")
    assert os.path.exists(link)
    store.write_json("s.json", {"k": "값"})
    assert store.read_json("s.json") == {"k": "값"} and store.read_json("none.json") is None
    store.trash(rf, "processing")
    assert store.list("processing") == [] and (tmp_path / ".trash" / "a.mp4").exists()
