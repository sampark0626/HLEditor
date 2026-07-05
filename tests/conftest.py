import sys
from pathlib import Path

# 프로젝트 루트(app.py, jobs.py 등이 있는 위치)를 sys.path에 추가해
# 테스트 파일에서 `import app`, `import jobs` 등을 그대로 쓸 수 있게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
