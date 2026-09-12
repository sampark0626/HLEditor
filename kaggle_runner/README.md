# HLEditor Kaggle 러너

로컬 서버 없이 Kaggle GPU 커널 하나로 "하이라이트 추출 → Dot Play 2D 변환 → 합성"까지
끝내는 스크립트. [run_match.py](run_match.py) 참고.

**여러 경기를 한 번에 처리한다.** `MATCH_VIDEOS`를 비워두면(기본값) Input에 붙인
Dataset 아래 있는 영상 파일을 전부 자동으로 찾아 순서대로 처리한다 — 매주 영상이
여러 개(예: 6개) 나와도 **코드 수정 없이** 전부 한 번의 Save & Run All로 끝난다.
파일별 산출물은 `highlight_<원본파일명>.mp4` 식으로 구분되어 서로 덮어쓰지 않는다.

⚠️ **지난주 영상을 다시 처리하지 않으려면**: 자동탐색은 Input에 붙어 있는 모든
Dataset의 영상을 대상으로 하므로, Dataset을 계속 누적해서 붙이면 지난주 것까지
매번 다시 돕니다. 매주 **새 이름의 Dataset**을 쓰고 지난주 Dataset은 Input에서
제거하는 걸 권장합니다. 혹시 안 지웠다면 `SKIP_STEMS`에 파일명(확장자 제외)을
적어 수동으로 제외할 수 있습니다 — 실행이 끝나면 로그에 이번에 처리한 파일명
목록이 그대로 찍히니 다음번에 복사해서 넣으면 됩니다.

## 첫 실행 — Kaggle CLI 없이, 가장 쉬운 경로

1. **Kaggle 계정** 준비 (없으면 kaggle.com 가입, 휴대폰 인증 필요할 수 있음) — 최초 1회
2. **경기 영상 업로드**: Kaggle → Datasets → New Dataset → 이번 주 영상 파일들을
   **한꺼번에** 업로드(여러 개 선택 가능, 폰 브라우저로도 가능)
3. **새 Notebook 생성**: Kaggle → Code → New Notebook — 최초 1회, 이후엔 이 노트북을
   계속 재사용
4. 우측 패널에서:
   - **Accelerator**: GPU T4 x2 (또는 P100)
   - **Internet**: On (필수 — git clone, pip install, Gemini API 호출에 필요)
   - **Add-ons → Secrets**: `GEMINI_API_KEY` 등록 (하이라이트 판별용) — 최초 1회
   - Input에 2번의 Dataset 붙이기 (파일 목록 위치는 Kaggle 버전에 따라 "Add-ons"가
     아니라 화면 우측 세로 아이콘 줄의 **Input** 패널일 수 있음 — `File → Add input`
     메뉴로도 열 수 있음)
5. [run_match.py](run_match.py) 내용을 노트북 셀에 그대로 붙여넣기 — **경로를
   따로 수정할 필요 없음**, `MATCH_VIDEOS`를 비워두면 Input에 붙은 영상을 전부
   자동으로 찾아 처리한다
6. **Save & Run All** 클릭 — 브라우저를 닫아도 백그라운드에서 계속 실행됨

**다음 주부터는 1·3·4(Secrets)는 건너뛰고 2(영상 업로드) → Input에 새 Dataset
버전 반영 확인 → 5(코드 그대로) → 6(실행)만 반복**하면 됩니다.

7. 완료 후 노트북의 **Output** 탭에서 영상별로 `highlight_<파일명>.mp4`,
   `highlight_with_dotplay_<파일명>.mp4`, `coords_<파일명>.parquet`,
   `summary_<파일명>.json`, 전체 요약 `batch_summary.json` 다운로드

## 알아둘 것

- **해결됨 — Roboflow 계정이 아예 필요 없습니다.** `roboflow/sports`(참고
  저장소) 공식 예제([setup.sh](https://github.com/roboflow/sports/blob/main/examples/soccer/setup.sh))가
  검출 가중치를 Google Drive 공개 링크에서 직접 받아 쓰는 걸 확인했습니다.
  `run_match.py`도 이제 이 방식을 기본 경로로 씁니다 — Roboflow API 키,
  크레딧, 유료 플랜 전부 무관합니다. Roboflow Secrets는 등록해 두면 좋지만
  (만약을 위한 최후 폴백) 필수는 아닙니다.
- 이 가중치 다운로드는 몇백 MB라 첫 실행 때 약간 시간이 걸립니다. 이후
  같은 세션 재실행에서는 캐시를 재사용합니다(세션이 끝나면 다시 받음 —
  매번 몇백MB 받는 게 아깝다면 나중에 Kaggle Dataset으로 한 번 캐싱해 두는
  것도 방법).
- **소요 시간**: 30분 경기 기준(실측) 하이라이트 추출만 약 15~20분. 2D 변환까지
  하면 더 걸림. 6편을 한 세션에서 순서대로 처리하면 다 합쳐 몇 시간 걸릴 수 있음
  — Kaggle 세션 최대 길이(12시간), 주간 GPU 한도(약 30시간) 안에서는 여유 있지만
  시간이 오래 걸리는 작업이라는 점은 감안할 것. 한 영상 처리가 실패해도 나머지는
  계속 진행된다(`batch_summary.json`에 영상별 성공/실패가 남음).
- **MODE**: 기본 `"highlights"`(하이라이트 구간만 2D 변환, 권장·저비용).
  특정 경기를 통째로 보고 싶으면 `"full"`로 바꿔 재실행(원본 영상만 있으면
  나중에 언제든 가능).
- 아직 없는 것: Kaggle API로 업로드~실행~결과회수를 자동화하는 것(지금은
  수동 클릭 필요), 캐릭터(등번호)별 구분. 이 초안이 한 번 돌아가는 걸
  확인한 뒤 이어서 붙일 예정.

## 처음 실행 후 알려주면 좋은 것

- 실패했다면: 어느 단계(`bootstrap`/`resolve_models`/하이라이트/2D변환/합성)에서
  어떤 에러 메시지가 났는지 그대로
- 성공했다면: `summary.json` 내용과 대략적인 총 소요 시간
