"""HLEditor 인박스 러너 — Kaggle 커널 진입 스크립트.

Apps Script(kaggle_runner/apps_script/Code.gs)가 이 파일 내용을 그대로 Kaggle에 push해
실행한다. 실제 로직은 저장소의 kaggle_runner/inbox_runner.py에 있어서, 이 파일은 짧게
유지한다. main 브랜치에 push하면 다음 실행부터 바로 반영되므로 pytest를 통과한 코드만 올린다.

수동으로 시험할 때는 Kaggle 노트북 셀에 이 파일 내용을 그대로 붙여넣고 실행해도 된다
(비공개 데이터셋 hl-secrets를 Input으로 붙이고 Internet을 켤 것).
"""

import subprocess
import sys

REPO_URL = "https://github.com/sampark0626/HLEditor.git"
REPO_DIR = "/tmp/HLEditor"   # /kaggle/working 밖 — 커널 Output에 저장소가 섞이지 않게

subprocess.run(["rm", "-rf", REPO_DIR], check=False)
subprocess.run(["git", "clone", "--depth", "1", REPO_URL, REPO_DIR], check=True)
sys.path.insert(0, REPO_DIR)

from kaggle_runner import inbox_runner  # noqa: E402

sys.exit(inbox_runner.main([]))
