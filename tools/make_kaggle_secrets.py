#!/usr/bin/env python3
"""Kaggle 인박스 러너용 비밀값 파일(hl_secrets.json)을 만든다 — PC에서 한 번 실행.

하는 일:
  1) 기존 client_secrets.json(데스크톱 OAuth 클라이언트)으로 브라우저 동의를 받아
     Drive + YouTube 권한의 refresh token을 발급한다.
  2) 내 드라이브에 HLEditor/ 폴더 트리(01_inbox … _state)가 없으면 만든다.
  3) kaggle_secrets/ 에 다음을 쓴다(.gitignore 대상 — 절대 커밋하지 말 것):
       hl_secrets.json              Kaggle 비공개 데이터셋 hl-secrets에 올릴 파일
       dataset-metadata.json        kaggle CLI로 올릴 때 쓰는 메타데이터
       apps_script_properties.txt   Apps Script 스크립트 속성에 넣을 값
  4) 다음에 할 일(Kaggle 업로드, Apps Script, ntfy 구독)을 출력한다.

토큰을 다시 발급할 때도 같은 명령을 쓴다. ntfy topic과 KICK_KEY는 이전 값을 그대로
재사용하므로 폰 구독과 Apps Script 설정은 바꿀 필요가 없다.

사용법:
  python tools/make_kaggle_secrets.py [--kaggle-user 아이디] [--root-name HLEditor]
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from kaggle_runner.inbox_runner import GOOGLE_SCOPES, SECRETS_FILENAME  # noqa: E402
from kaggle_runner.stores import FOLDER_MIME, FOLDERS  # noqa: E402

OUT_DIR = _ROOT / "kaggle_secrets"
DATASET_SLUG = "hl-secrets"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_folder(service, name: str, parent: str) -> str | None:
    q = (f"'{_q(parent)}' in parents and name = '{_q(name)}' "
         f"and mimeType = '{FOLDER_MIME}' and trashed = false")
    files = service.files().list(q=q, fields="files(id, name)", pageSize=10).execute().get("files", [])
    return files[0]["id"] if files else None


def ensure_folder_tree(service, root_name: str = "HLEditor") -> tuple[str, dict]:
    """내 드라이브 최상위에 root_name 폴더와 하위 폴더들을 만든다(이미 있으면 그대로 쓴다)."""
    created = []
    root_id = _find_folder(service, root_name, "root")
    if not root_id:
        root_id = service.files().create(
            body={"name": root_name, "mimeType": FOLDER_MIME, "parents": ["root"]},
            fields="id").execute()["id"]
        created.append(root_name)
    ids = {}
    for key, name in FOLDERS.items():
        fid = _find_folder(service, name, root_id)
        if not fid:
            fid = service.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [root_id]},
                fields="id").execute()["id"]
            created.append(f"{root_name}/{name}")
        ids[key] = fid
    for c in created:
        print(f"  폴더 만듦: {c}")
    return root_id, ids


def load_previous(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_properties(path: Path) -> dict:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def build_secrets(*, client_id: str, client_secret: str, refresh_token: str, root_id: str,
                  gemini_key: str, ntfy_topic: str, default_title: str, privacy: str) -> dict:
    return {
        "GEMINI_API_KEY": gemini_key,
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_REFRESH_TOKEN": refresh_token,
        "ROOT_FOLDER_ID": root_id,
        "NTFY_TOPIC": ntfy_topic,
        "DEFAULT_TITLE": default_title,
        "YOUTUBE_PRIVACY": privacy,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Kaggle 인박스 러너용 hl_secrets.json 만들기")
    ap.add_argument("--kaggle-user", default="", help="Kaggle 아이디(데이터셋 메타데이터·안내문에 사용)")
    ap.add_argument("--root-name", default="HLEditor", help="내 드라이브에 만들 최상위 폴더 이름")
    args = ap.parse_args(argv)

    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    import youtube_uploader as yt_up

    client_path = yt_up._get_secrets_path()
    if not client_path:
        print("client_secrets.json이 없습니다. README의 'YouTube 연동 설정'대로 먼저 받아 두세요.")
        return 1
    gemini_key = config.load_gemini_api_key()
    if not gemini_key:
        print("경고: .env에 GEMINI_API_KEY가 없습니다 — Kaggle에서 AI 판별 없이 처리됩니다.")

    print("브라우저에서 Google 로그인과 권한 동의를 진행해 주세요 (Drive + YouTube).")
    print("'확인되지 않은 앱' 화면이 나오면 [고급] → [안전하지 않은 페이지로 이동]을 누르면 됩니다.")
    flow = InstalledAppFlow.from_client_secrets_file(client_path, scopes=GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("refresh token을 받지 못했습니다. 다시 실행해 주세요.")
        return 1

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    root_id, _ = ensure_folder_tree(service, args.root_name)

    OUT_DIR.mkdir(exist_ok=True)
    secrets_path = OUT_DIR / SECRETS_FILENAME
    props_path = OUT_DIR / "apps_script_properties.txt"
    previous = load_previous(secrets_path)
    prev_props = read_properties(props_path)
    topic = previous.get("NTFY_TOPIC") or f"hl-{secrets.token_urlsafe(18)}"
    kick_key = prev_props.get("KICK_KEY") or secrets.token_urlsafe(24)

    data = build_secrets(
        client_id=creds.client_id, client_secret=creds.client_secret,
        refresh_token=creds.refresh_token, root_id=root_id, gemini_key=gemini_key or "",
        ntfy_topic=topic, default_title=config.get_default_title(),
        privacy=config.get_youtube_privacy())
    secrets_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    user = args.kaggle_user or "<Kaggle 아이디>"
    (OUT_DIR / "dataset-metadata.json").write_text(json.dumps({
        "title": DATASET_SLUG, "id": f"{user}/{DATASET_SLUG}", "licenses": [{"name": "other"}],
    }, indent=2), encoding="utf-8")
    props_path.write_text("\n".join([
        "# Apps Script → 프로젝트 설정 → 스크립트 속성에 아래 값을 넣으세요",
        f"KAGGLE_USERNAME={args.kaggle_user}",
        "KAGGLE_API_TOKEN=<Kaggle Settings → API에서 만든 토큰>",
        f"ROOT_FOLDER_ID={root_id}",
        f"NTFY_TOPIC={topic}",
        "KERNEL_SLUG=hleditor-inbox",
        f"SECRETS_DATASET={DATASET_SLUG}",
        "FAST_DAYS=7,1",
        f"KICK_KEY={kick_key}",
    ]) + "\n", encoding="utf-8")

    print(f"""
완료했습니다. 만든 파일: {OUT_DIR}
  - {SECRETS_FILENAME}  (비밀값 — 절대 공유·커밋 금지)
  - dataset-metadata.json
  - apps_script_properties.txt

다음에 할 일:
  1) Kaggle에 비공개 데이터셋으로 올리기
     웹: kaggle.com → Datasets → New Dataset → {SECRETS_FILENAME} 업로드 → 이름 {DATASET_SLUG}, 비공개(Private)
     (이미 있으면 그 데이터셋의 New Version으로 올리기)
  2) Apps Script 스크립트 속성: apps_script_properties.txt 내용을 그대로 넣기
  3) 아이폰 ntfy 앱 → + → 주제(topic) '{topic}' 구독
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
