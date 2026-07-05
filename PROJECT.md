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

이 프로젝트는 더 이상 단일 스크립트가 아니라 웹 UI 기반 배치 도구로 확장되었다. 2026-07 리팩터(Phase 1~4, [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) 참고)를 거치며 역할별 모듈로 세분화됐다.

| 파일 | 설명 |
|------|------|
| `soccer_highlights.py` | 오디오 분석 · Gemini 비전 판별 · 탈중복 · 영상 생성 파이프라인 (CLI로도 단독 실행 가능) |
| `app.py` | Flask 앱 조립·시작만 담당하는 진입점 (실제 라우트는 아래 모듈에 분리) |
| `jobs.py` | `JOBS` 상태 저장소·백그라운드 워커·`_process` 처리 파이프라인·`restore_recent_results` |
| `routes_jobs.py` | 큐 관리·리뷰·영상 생성 API (`/api/jobs/*`) — Flask Blueprint |
| `routes_auth.py` | YouTube OAuth·업로드 API (`/auth/youtube/*`, `/api/jobs/*/upload-youtube`) — Blueprint |
| `routes_band.py` | BAND OAuth·게시 API (`/auth/band/*`, `/api/jobs/post-band`) — Blueprint |
| `tray.py` | Windows 시스템 트레이 아이콘 |
| `config.py` | `.env` 파싱, ffmpeg 확인, `output/`·`results/` 경로, 기본 제목/포트 등 공용 설정 |
| `youtube_uploader.py` | YouTube OAuth 2.0 · 업로드 · 썸네일 · 챕터 설명 생성 |
| `band_poster.py` | BAND OAuth 2.0 · 날짜별 게시글 작성 |
| `templates/index.html` | 웹 UI 뼈대 (큐 관리 / 리뷰 / 영상 생성 3탭). CSS/JS는 정적 파일로 분리됨 |
| `static/css/app.css` | 웹 UI 스타일 |
| `static/js/{state,utils,notify,queue,pipeline,review,build,main}.js` | 웹 UI 로직. `main.js`가 가장 마지막에 로드되어 폴링·초기화를 담당 — 로드 순서가 곧 의존 순서다 |
| `tests/` | pytest 테스트 (유닛 + Flask `test_client` 스모크) |
| `pyproject.toml` | ruff(lint) 설정, pytest `testpaths` |
| `requirements.txt` / `requirements-dev.txt` | 런타임 / 개발(pytest·ruff) 의존성 |
| `README.md` | 설치·사용법 (사용자용) |
| `PROJECT.md` | 이 문서 (설계 의도·핸드오프용) |
| `IMPROVEMENT_PLAN.md` | 2026-07 리팩터 계획과 실행 로그 (무엇을 왜 바꿨는지의 상세 기록) |
| `PRIVACY.md` | BAND API 심사용 개인정보처리방침 |

`soccer_highlights.py` 상단의 **조절 파라미터 블록**(`WIN_SEC` ~ `CONF_MAYBE`)이 동작의 거의 전부를 좌우한다. 로직보다 이 값들을 먼저 본다.
웹 UI로 실행할 때는 `python app.py`가 진입점이며, 아래 CLI 사용법은 `soccer_highlights.py`를 직접 스크립트로 돌릴 때만 해당한다.

산출물은 `output/`(영상)·`results/`(판별 결과 JSON)에 저장된다. 자세한 경로 로직은 `config.py`의 `OUTPUT_DIR`/`RESULTS_DIR` 참고.

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

### 3. 병렬화 (완료)
~~현재 비전 판별은 후보를 순차 처리한다~~ → ✅ 비전 판별(`classify_all_parallel`)과 빌드 클립 재인코딩(`build_output`) 모두 `ThreadPoolExecutor`로 병렬화 완료(동시성은 각각 `VISION_WORKERS`=6, `BUILD_CLIP_WORKERS`=3). 아래 "향후 개선 아이디어" 1번 및 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) Phase 4-4 참고.

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
6. ~~비전 판별 중단~~ ✅ 구현됨 — `should_cancel` 콜백 + `/api/jobs/<jid>/cancel` + UI 취소 버튼(잡별 개별 취소, 빌드 단계도 동일 메커니즘 적용)
7. ~~토큰 사용량/비용 표시~~ ✅ 구현됨 — `usage_metadata` 합산, CLI/UI에 입출력 토큰·예상 USD 표시
8. ~~타이틀 워터마크~~ ✅ 구현됨 — 우상단 `drawtext`(`--title` / UI 입력), 폰트를 작업폴더 복사로 Windows 콜론 문제 회피
9. ~~자동 검출 기준 UI 노출~~ ✅ 구현됨 — `SENSITIVITY_PRESETS`(more/normal/strict) + UI 3단 버튼("많이 잡기/보통/엄선"). `detect_spikes(percentile,min_db)` 오버라이드, `/api/detect`에 `sensitivity` 전달. dB·percentile 용어 숨김.
10. ~~단계별 진행 표시(프로그레스 바)~~ ✅ 구현됨 — `on_progress` 콜백이 잡별 `progress` 필드를 갱신하고, `/api/jobs` 폴링(2초 간격)에 실려 옴. UI에서 비전판별("32/72") / 영상생성("클립 18/53") 진행 바 표시.
11. ~~stream-copy 분기~~ ✅ 구현됨 — `QUALITY_PRESETS`의 `copy` 모드(`-c copy`, 재인코딩 없음). 단 타이틀/다운스케일 미적용.
12. ~~인코딩 속도·용량 튜닝~~ ✅ 구현됨 — 기본 `fast`+`CRF 25`로 변경, `QUALITY_PRESETS`(size/balanced/quality/copy)를 UI/CLI에서 선택(`--quality`).

### 보류 — 향후 확장 아이디어 (현재 작업 대상 아님)
- **BAND 업로드 자동화 (전체 사이클 에이전트화)** — 경기 종료 후 게시까지 반자동. BAND는 영상 직접 업로드 API가 없어 **YouTube 경유**로 우회. 단계:
  - ① 경기 종료 → XbotGo 업로드 → BAND 댓글에 공유 링크(사용자, 기존 습관)
  - ② **BAND API `Get Posts`/`Get Comments`** 로 댓글의 `cloud.xbotgo.net/share` 링크 추출 (읽기 전용, 가능 확인됨)
  - ③ **Claude + Chrome 확장**으로 공유 페이지 열어 원본 다운로드 (XbotGo는 React SPA + MD5 서명 + 시간제한 토큰이라 API 직접호출 대신 브라우저 조작이 견고. **다운로드 레그 1회 실측 미완**)
  - ④ 로컬 하이라이트 추출 (✅ 완성)
  - ⑤ Claude + Chrome으로 **YouTube 업로드**(일부공개) + 비전 결과 기반 **자동 설명글**(타임스탬프 장면 목록, 챕터 형식) + 최고 신뢰 장면 **썸네일**(채널 인증 시)
  - ⑥ **BAND `Write Post`** 또는 Chrome으로 YouTube 링크 게시 + 푸시
  - 성격: 완전 무인이 아니라 **Claude 운전 + 사용자 감독**(로그인·다운로드/업로드/게시 건별 확인, CAPTCHA는 사용자). 트리거는 BAND 폴링 또는 수동.
  - 사전 준비: BAND·YouTube OAuth 앱 등록, Chrome 확장 연결, XbotGo/BAND 약관 확인.
- **STT 보조 신호** — 현장음에서 "골!"·"슛!" 등 외침을 Speech-to-Text로 인식해 볼륨·비전이 애매한 후보의 점수를 보강하는 3차 신호. 동호회 경기는 해설 없고 웅성거림 위주라 인식률이 낮아 효과 불확실. 추후 정확도를 더 끌어올려야 할 때 재검토.

### 코드 구조 메모 (Claude Code용)
- 비전 판별(`classify_all_parallel`)과 빌드 클립 인코딩(`build_output`)은 둘 다 `ThreadPoolExecutor`로 병렬화돼 있다. 두 곳 모두 완료 순서와 무관하게 원본 인덱스로 결과를 재조립하는 동일한 패턴을 쓴다 — 새로운 병렬 작업을 추가할 때 이 패턴을 참고.
- 후보 선택 로직은 `select_segments()`로 분리돼 있어, 비전 결과만 채워지면 일반 모드/재선택 모드가 동일 함수를 쓴다.
- `save_results()` / `--from-json` 경로 덕분에, 반복 테스트 시 비전 API를 다시 안 써도 된다. 웹 앱에서는 이 결과 JSON(`results/results_{jid}.json`)을 `jobs.restore_recent_results()`가 재시작 시 스캔해 24시간 이내 것만 복원한다.
- 잡 상태(`jobs.JOBS`)는 프로세스 메모리에만 있고 영속화되지 않는다. `jobs.py`의 모든 접근은 `JLOCK`으로 보호해야 한다 — 새 필드를 추가하거나 순회하는 코드를 쓸 때 반드시 `with jobs.JLOCK:` 안에서 하거나, 스냅샷을 뜬 뒤 락 밖에서 쓸 것 (Phase 1에서 락 없이 순회하다 생긴 레이스를 여러 건 고쳤음).
- 잡 취소는 `cancel_requested`/`pending_delete` 두 플래그로 처리한다. 처리 중인 잡을 삭제하면 즉시 제거하지 않고 취소 플래그만 세운 뒤, 워커/빌드 루프가 다음 체크포인트(`should_cancel` 콜백)에서 감지해 실제로 정리한다(`jobs.finalize_pending_delete`). 이 흐름을 건드릴 때는 반드시 "처리 중 삭제" 시나리오를 재현해 워커가 죽지 않는지 확인할 것 — Phase 1의 치명 버그가 바로 이 지점이었다.
- 정적 JS는 `static/js/*.js`가 classic `<script>` 태그로 로드 순서대로 로드된다(ES 모듈 아님). `state.js`가 가장 먼저 로드되어 전역 변수·`window.HL_CONFIG` 파생 상수를 선언하고, `main.js`가 가장 마지막에 로드되어 `poll()`과 초기화 호출을 담당한다. 새 파일을 추가할 때 이 순서를 깨지 않아야 한다(전역 렉시컬 스코프를 공유하므로 늦게 로드된 파일이 먼저 로드된 파일의 `let`/`const`를 그대로 참조 가능).

---

## 환경 의존성
- Python 3.x — 런타임 의존성은 `requirements.txt`(버전 범위 고정), 개발용은 `requirements-dev.txt`(pytest, ruff)
- `ffmpeg` / `ffprobe` (PATH에 있어야 함)
- `GEMINI_API_KEY` 환경변수 (비전 사용 시). 없으면 비전 판별 없이 오디오 후보 전체를 채택 취급하고 UI에 배지로 알림
- 웹 UI 실행 시: `flask`, `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`, `requests`, `pillow`, `pystray`(트레이 아이콘, 선택)

## 테스트·lint
```bash
pip install -r requirements-dev.txt
pytest              # tests/ 전체 실행
ruff check .        # lint (--fix로 안전한 항목 자동 수정)
```
`tests/`는 ffmpeg/Gemini 실호출 없이 동작하도록 설계됐다(오디오 검출은 합성 신호로, Flask 라우트는 `jobs.JOBS`에 상태를 직접 주입하고 `build_output`을 몽키패치로 목 처리). 새 기능을 추가하면 여기에 대응하는 테스트를 먼저 확인/추가하는 것을 권장.

## 도메인 메모
- 카메라: XbotGo 고정 광각 풀샷 (AI 카메라). 선수·공이 작게 보이는 점이 비전 판별 난이도에 영향
- 대상: 동호회/아마추어 경기. 해설자 없음, 관중 적음 → TV 중계보다 오디오 신호 약함 → 절대 임계보다 baseline 대비 상대 급증이 적합
