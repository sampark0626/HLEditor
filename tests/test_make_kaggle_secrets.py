"""tools/make_kaggle_secrets.py — Drive 폴더 트리 생성과 비밀값 파일 구성 (브라우저 동의 부분 제외)."""

import importlib.util
from pathlib import Path

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
