# 축구 하이라이트 자동 추출 프로젝트

동호회 축구 경기 영상에서 **음성 볼륨 급증 + Gemini 비전 판별**로 하이라이트 구간을 자동 추출해 하나의 영상으로 만드는 도구.

이 문서는 Claude Code가 프로젝트를 이어받아 작업할 수 있도록 설계 의도·검증 결과·주의사항을 정리한 핸드오프 문서다.

---

## 핵심 아이디어

영상을 사람이 직접 다 보지 않고 하이라이트를 찾기 위해, **오디오 볼륨**을 1차 신호로 쓴다. 골·슈팅·결정적 장면에서는 환호·외침으로 볼륨이 순간적으로 튄다. 다만 이것만으로는 "무슨 일이 일어났는지" 모르므로, 급증 구간의 프레임을 **Gemini 비전 모델**에 보내 하이라이트 여부를 2차 판별한다.

```
오디오 추출 → RMS 볼륨 + rolling baseline → 급증(spike) 후보 검출
            → 후보 구간 프레임 추출 → Gemini 비전 판별(유형/신뢰도)
            → 신뢰도 임계 이상만 채택 → ffmpeg로 클립 분할·병합
```

이 2단계 구조(싼 오디오 필터 → 비싼 비전 판별)는 비용·정확도 균형을 위한 의도적 설계다. 오디오가 "언제"를 좁혀주고, 비전이 "무엇"을 판단한다.

---

## 파일

| 파일 | 설명 |
|------|------|
| `soccer_highlights.py` | 메인 스크립트. 전체 파이프라인 단일 파일로 구현 |
| `PROJECT.md` | 이 문서 |

스크립트 상단의 **조절 파라미터 블록**(`WIN_SEC` ~ `CONF_MAYBE`)이 동작의 거의 전부를 좌우한다. 로직보다 이 값들을 먼저 본다.

---

## 실행 방법

```bash
pip install numpy scipy google-genai
# ffmpeg 필수 (mac: brew install ffmpeg / ubuntu: apt install ffmpeg)
export GEMINI_API_KEY="..."

# 1단계: 오디오 후보만 확인 (API 호출 없음, 무료, 빠름)
python3 soccer_highlights.py 경기.mov --no-vision --dry-run

# 2단계: 비전 판별 결과까지 확인 (영상 생성은 생략)
python3 soccer_highlights.py 경기.mov --dry-run

# 3단계: 실제 하이라이트 영상 생성
python3 soccer_highlights.py 경기.mov -o highlights.mp4
```

권장 흐름: 항상 `--dry-run`으로 후보·판별 결과를 먼저 보고, 파라미터를 맞춘 뒤 마지막에 영상을 생성한다.

### 주요 옵션
- `--no-vision` : 비전 판별 생략, 오디오 후보 전체 사용
- `--dry-run` : 후보/판별만 출력, 영상 미생성
- `--conf 0.5` : 자동 채택 신뢰도 임계 조정 (기본 0.70)
- `--save-json PATH` : 후보/비전 판별 결과를 JSON으로 저장 (재선택용)
- `--from-json PATH` : 저장된 JSON에서 읽어 **비전 재호출 없이** 재선택·재생성
- `--api-key` : 환경변수 대신 직접 키 전달

### 권장 워크플로 (30분 영상에서 특히 중요)
비전을 **한 번만** 돌리고, 임계 튜닝은 API 재호출 없이 한다.

```bash
# 1) 비전 한 번 돌리고 결과를 JSON으로 저장 (영상은 아직 안 만듦)
python3 soccer_highlights.py 경기.mov --save-json results.json --dry-run

# 2) 저장된 결과로 임계만 바꿔 재선택 — Gemini 재호출 0회, 무료/즉시
python3 soccer_highlights.py --from-json results.json --conf 0.55 --dry-run

# 3) 마음에 들면 그대로 영상 생성 (역시 비전 재호출 없음)
python3 soccer_highlights.py --from-json results.json --conf 0.55 -o highlights.mp4
```

`--from-json` 사용 시 영상 경로는 JSON에 기록돼 있어 생략 가능하다(원본 위치가 바뀌었으면 첫 인자로 새 경로를 넘기면 덮어쓴다).

---

## 파라미터 가이드

스크립트 상단에서 조정한다.

| 파라미터 | 의미 | 조정 방향 |
|----------|------|-----------|
| `WIN_SEC` (0.5) | RMS 계산 윈도우 | 짧을수록 민감 |
| `HOP_SEC` (0.25) | 윈도우 이동 간격 | — |
| `BASELINE_SEC` (20) | rolling baseline 추정 구간 | 환경 소음 변화 크면 줄임 |
| `SPIKE_PERCENTILE` (95) | 상위 몇 %를 spike로 | 후보 많으면 ↑ (97~98) |
| `SPIKE_MIN_DB` (8.0) | 최소 상승폭 dB 하한 | 후보 많으면 ↑ (10+) |
| `MERGE_GAP_SEC` (4.0) | 인접 spike 병합 간격 | — |
| `PRE_SEC` / `POST_SEC` (8 / 5) | peak 앞뒤 클립 길이 | 빌드업 포함 위해 앞을 길게 |
| `FRAME_INTERVAL` (0.5) | 비전용 프레임 추출 간격 | 골 순간 놓치면 ↓ |
| `MAX_FRAMES` (8) | 후보당 Gemini 전송 프레임 수 | 비용/정확도 trade-off |
| `VISION_MODEL` | gemini-2.5-flash | 정확도 필요시 gemini-2.5-pro |
| `CONF_AUTO` (0.70) | 자동 채택 임계 | 좋은 장면 누락시 ↓ |
| `CONF_MAYBE` (0.40) | '확인필요' 분류 하한 | — |

판별 결과는 `✅ 채택` / `⚠️ 확인필요` / `❌`로 표시된다. `확인필요`는 신뢰도 애매(0.40~0.70) 구간으로, 사람이 빠르게 검토하는 용도다.

---

## 검증 기록 (235초 테스트 영상)

XbotGo 고정 카메라로 촬영된 학교 운동장 동호회 경기 약 4분 샘플로 전체 파이프라인을 검증했다.

- **오디오 신호 살아있음**: baseline 약 -34dB, 하이라이트 구간에서 +10~17dB 명확히 급증. 조용한 동호회 경기인데도 1차 필터로 충분히 신뢰 가능.
- **검출**: 4분에 후보 17개 → 다소 많음. 30분 풀 경기엔 `SPIKE_PERCENTILE`/`SPIKE_MIN_DB` 상향 권장.
- **클립 분할·병합 정상**: 3개 구간 → 39초 영상 생성 확인.
- **비전 특성**: 고정 광각 풀샷이라 선수·공이 작게 보임. 골문 앞 밀집 상황(예: 196초 지점)은 단일/다중 프레임 판별이 잘 됨. 중앙 정렬 킥오프성 장면(예: 13초)은 오디오로 잡혀도 비전이 걸러줄 가능성 높음 → 2단계 구조의 효용이 여기서 드러남.

---

## ⚠️ 30분 영상 적용 시 주의사항 (중요)

검증은 4분 샘플로 했고, 실제 대상은 **약 30분**이다. 규모가 7~8배 커지면서 새로 고려할 점이 있다.

### 1. 비전 API 비용·시간
4분에 후보 17개였으니 30분이면 단순 비례로 **100개 이상 후보**가 나올 수 있다. 후보마다 Gemini 호출이 1번씩 일어나므로:
- 먼저 `--no-vision --dry-run`으로 후보 수를 확인하고
- 후보가 과하면 임계(`SPIKE_PERCENTILE` 97~98, `SPIKE_MIN_DB` 10+)를 올려 비전 호출 자체를 줄인다
- `gemini-2.5-flash`는 저렴하지만 호출 수가 많아지면 누적되므로, 후보를 먼저 솎는 게 비용 측면에서 핵심

### 2. 최종 재인코딩 시간
`build_output`은 채택 구간을 `libx264`로 재인코딩 후 concat한다. 채택 구간이 수십 개면 이 단계가 가장 느리다. 대응:
- `-preset veryfast` → `ultrafast`로 변경 (품질 약간 희생, 속도 ↑)
- 또는 segment 추출을 stream-copy(`-c copy`)로 바꾸는 방안 검토. 단 키프레임 경계 문제로 시작 지점이 어긋날 수 있어, 정확도가 중요하면 현행 재인코딩 유지

### 3. 병렬화 여지 (개선 후보)
현재 비전 판별은 후보를 순차 처리한다. 30분 영상에서 후보가 많으면 느리므로, Claude Code로 이어서 작업한다면 **비전 호출 병렬화**(예: `concurrent.futures`로 동시 N개 호출)가 가장 효과적인 개선 지점이다. Gemini API rate limit을 고려해 동시성 4~8 정도로.

### 4. 메모리
오디오는 16kHz mono로 추출하므로 30분이어도 약 60MB 수준, 문제 없음. 프레임은 후보 구간만 그때그때 추출·삭제하므로 누적되지 않음.

---

## 향후 개선 아이디어 (Claude Code 작업 후보)

우선순위 순:

1. ~~비전 호출 병렬화~~ ✅ 구현됨 — `ThreadPoolExecutor`로 후보 동시 처리 (`classify_all_parallel`). 기본 동시성 6, `--workers`로 1~8 조정. 완료 순서대로 실시간 출력하되 원본 순서 유지.
2. ~~판별 결과 JSON 저장~~ ✅ 구현됨 (`--save-json`)
3. ~~재선택 모드~~ ✅ 구현됨 (`--from-json` + `--conf`)
4. ~~로컬 웹 UI~~ ✅ 구현됨 (`app.py` + `templates/index.html`) — 4단계 클릭 워크플로, 진행 로그, 슬라이더 재선택
5. ~~503/429 재시도 + 예외 격리~~ ✅ 구현됨 — `classify_with_gemini` 지수 백오프, 후보 단위 격리(프레임추출/API 모두), ffmpeg spawn WinError 5 재시도(`run`)
6. ~~비전 판별 중단~~ ✅ 구현됨 — `should_cancel` 콜백 + `/api/cancel` + UI 중단 버튼
7. ~~토큰 사용량/비용 표시~~ ✅ 구현됨 — `usage_metadata` 합산, CLI/UI에 입출력 토큰·예상 USD 표시
8. ~~타이틀 워터마크~~ ✅ 구현됨 — 우상단 `drawtext`(`--title` / UI 입력), 폰트를 작업폴더 복사로 Windows 콜론 문제 회피
9. **stream-copy 분기** — 속도 우선 모드 옵션화 (미구현)
10. **STT 보조 신호** — 현장음 "골!" 외침 보조 점수 (우선순위 낮음, 미구현)
11. **BAND 업로드 자동화** — 다운로드→편집→업로드 한 사이클 에이전트화 (검토 단계, 아래 별도 섹션)

### 코드 구조 메모 (Claude Code용)
- 비전 판별 루프는 `main()` 일반 모드의 `[3/4]` 블록. 여기서 후보별 `classify_with_gemini` 호출을 `ThreadPoolExecutor`로 감싸면 병렬화된다. 각 후보 dict에 결과를 `update`하는 방식이라 순서 의존성이 없어 병렬화가 쉽다.
- 후보 선택 로직은 `select_segments()`로 분리돼 있어, 비전 결과만 채워지면 일반 모드/재선택 모드가 동일 함수를 쓴다.
- `save_results()` / `--from-json` 경로 덕분에, 병렬화 작업 중에도 비전을 한 번 저장해두면 반복 테스트 시 API를 다시 안 써도 된다.

---

## 환경 의존성
- Python 3.x, `numpy`, `scipy`, `google-genai`
- `ffmpeg` / `ffprobe` (PATH에 있어야 함)
- `GEMINI_API_KEY` 환경변수 (비전 사용 시)

## 도메인 메모
- 카메라: XbotGo 고정 광각 풀샷 (AI 카메라). 선수·공이 작게 보이는 점이 비전 판별 난이도에 영향
- 대상: 동호회/아마추어 경기. 해설자 없음, 관중 적음 → TV 중계보다 오디오 신호 약함 → 절대 임계보다 baseline 대비 상대 급증이 적합
