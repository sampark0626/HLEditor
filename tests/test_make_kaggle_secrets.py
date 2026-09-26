"""tools/make_kaggle_secrets.py — Drive 폴더 트리 생성, 비밀값 파일 구성, 브라우저 동의 받기."""

import importlib.util
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from fake_drive import FOLDER, FakeDrive

from kaggle_runner.inbox_runner import REQUIRED_SECRETS
from kaggle_runner.stores import FOLDERS

_spec = importlib.util.spec_from_file_location(
    "make_kaggle_secrets", Path(__file__).resolve().parent.parent / "tools" / "make_kaggle_secrets.py")
mks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mks)


def test_folder_tree_created_once_and_reused():
    drive = FakeDrive()
    root_id, ids = mks.ensure_folder_tree(drive, "HLEditor")
    assert drive.folder_named("HLEditor", "root") == root_id
    assert set(ids) == set(FOLDERS)
    for key, name in FOLDERS.items():
        assert drive.folder_named(name, root_id) == ids[key]
    count = len(drive.items)
    again_root, again_ids = mks.ensure_folder_tree(drive, "HLEditor")
    assert (again_root, again_ids) == (root_id, ids) and len(drive.items) == count


def test_existing_root_folder_is_reused():
    drive = FakeDrive()
    existing = drive.add("HLEditor", "root", mime=FOLDER)
    root_id, _ = mks.ensure_folder_tree(drive, "HLEditor")
    assert root_id == existing


def test_build_secrets_has_every_required_key():
    data = mks.build_secrets(client_id="cid", client_secret="cs", refresh_token="rt", root_id="r",
                             gemini_key="g", ntfy_topic="hl-x", default_title="팀", privacy="unlisted")
    assert all(data[k] for k in REQUIRED_SECRETS)
    assert data["DEFAULT_TITLE"] == "팀" and data["YOUTUBE_PRIVACY"] == "unlisted"


def test_previous_values_are_read_back(tmp_path):
    props = tmp_path / "apps_script_properties.txt"
    props.write_text("# 주석\nKICK_KEY=abc=def\nNTFY_TOPIC=hl-1\n", encoding="utf-8")
    assert mks.read_properties(props) == {"KICK_KEY": "abc=def", "NTFY_TOPIC": "hl-1"}
    assert mks.read_properties(tmp_path / "none.txt") == {}
    assert mks.load_previous(tmp_path / "none.json") == {}


# ─── 브라우저 동의 받기(authorize) — 빈 연결이 먼저 와도 실제 인증 결과를 기다린다 ──────
class FakeFlow:
    def __init__(self):
        self.redirect_uri = None
        self.fetched = None
        self.credentials = object()

    def authorization_url(self, **kw):
        assert kw == {"access_type": "offline", "prompt": "consent"}
        return "https://accounts.google.com/o/oauth2/auth?fake", "state"

    def fetch_token(self, authorization_response):
        self.fetched = authorization_response


def _fake_browser(monkeypatch, flow, *, preconnects=2, query="state=s&code=abc"):
    def open_(url, new=1):
        port = int(re.search(r"localhost:(\d+)", flow.redirect_uri).group(1))

        def browser():
            for _ in range(preconnects):                     # 크롬 미리 연결·보안 프로그램 점검 흉내
                socket.create_connection(("127.0.0.1", port)).close()
                time.sleep(0.1)
            urllib.request.urlopen(f"http://127.0.0.1:{port}/?{query}", timeout=10).read()

        threading.Thread(target=browser, daemon=True).start()
        return True

    monkeypatch.setattr(mks.webbrowser, "open", open_)


def test_authorize_waits_past_empty_connections(monkeypatch):
    flow = FakeFlow()
    _fake_browser(monkeypatch, flow)
    creds = mks.authorize(flow, timeout_sec=20)
    assert creds is flow.credentials
    assert flow.fetched.startswith("https://127.0.0.1:") and "code=abc" in flow.fetched


def test_authorize_reports_denied_consent(monkeypatch):
    flow = FakeFlow()
    _fake_browser(monkeypatch, flow, preconnects=0, query="error=access_denied&state=s")
    with pytest.raises(RuntimeError, match="access_denied"):
        mks.authorize(flow, timeout_sec=20)
    assert flow.fetched is None


def test_authorize_gives_up_after_timeout(monkeypatch):
    flow = FakeFlow()
    monkeypatch.setattr(mks.webbrowser, "open", lambda url, new=1: True)   # 아무도 안 들어옴
    with pytest.raises(TimeoutError):
        mks.authorize(flow, timeout_sec=1)
