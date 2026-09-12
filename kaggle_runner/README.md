# HLEditor Kaggle 러너 (v0)

로컬 서버 없이 Kaggle GPU 커널 하나로 "하이라이트 추출 → Dot Play 2D 변환 → 합성"까지
끝내는 초안 스크립트. [run_match.py](run_match.py) 참고.

## 첫 실행 — Kaggle CLI 없이, 가장 쉬운 경로

1. **Kaggle 계정** 준비 (없으면 kaggle.com 가입, 휴대폰 인증 필요할 수 있음)
2. **경기 영상 업로드**: Kaggle → Datasets → New Dataset → 원본 영상 파일 업로드
   (폰 브라우저로도 가능)
3. **새 Notebook 생성**: Kaggle → Code → New Notebook
4. 우측 패널에서:
   - **Accelerator**: GPU T4 x2 (또는 P100)
   - **Internet**: On (필수 — git clone, pip install, Gemini API 호출에 필요)
   - **Add-ons → Secrets**: `GEMINI_API_KEY` 등록 (하이라이트 판별용).
     로컬 가중치 export를 시도하려면 `ROBOFLOW_API_KEY`도 등록(선택,
     없으면 하이라이트 추출만 되고 2D 변환 단계에서 멈춤)
   - **Add-ons → Data**: 2번에서 만든 영상 Dataset을 Input으로 추가
5. [run_match.py](run_match.py) 내용을 노트북 셀에 그대로 붙여넣고, 상단
   `MATCH_VIDEO` 경로를 실제 파일 경로로 수정
   (보통 `/kaggle/input/<데이터셋-slug>/<파일명>.mp4` 형태 — Data 패널에서
   경로를 그대로 복사할 수 있음)
6. **Save & Run All** 클릭 — 브라우저를 닫아도 백그라운드에서 계속 실행됨
7. 완료 후 노트북의 **Output** 탭에서 `highlight_with_dotplay.mp4`,
   `coords.parquet`, `summary.json` 다운로드

## 알아둘 것

- **처음이라 미검증**: 특히 "로컬 가중치(.pt) export"가 무료 계정에서 되는지
  확인 전. 안 되면 스크립트가 자동으로 Roboflow REST로 폴백하는데, 그 경우
  이전에 겪은 402(크레딧 소진) 문제가 재발할 수 있음 — 로그를 보고 알려주면
  다음 버전에서 대응.
- **소요 시간**: 30분 경기 기준 대략 수십 분(하이라이트 구간만 변환) ~
  수 시간(`MODE="full"`, 원본 전체 변환). Kaggle 무료 한도(GPU 주 30시간)
  안에서 6편/주도 여유 있음.
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
