# HLEditor 개선 계획 (2026-07-03, Fable 5 진단)

Fable 5가 전체 코드를 진단해 도출한 개선 계획. 실행은 Sonnet 등 다른 모델이 이 문서를 기준으로 진행한다.
각 항목은 독립적으로 실행 가능하며, Phase 순서대로 진행을 권장한다.

## 실행 로그 (Sonnet, 2026-07-03)

**Phase 1 완료.** 1-1 ~ 1-6 전항목 수정 + `app.py`/`soccer_highlights.py`/`templates/index.html` 수정,
실제 서버 기동 후 큐 추가 → 판별 → 리뷰 → 빌드 전 과정 스모크 테스트로 검증함.

- 테스트 중 **문서에 없던 추가 버그**를 발견해 함께 수정: Windows cp949 콘솔에서 진행 로그의
  이모지(✅⚠️❌)를 `print()`할 때 `UnicodeEncodeError`가 발생 — 1-1 수정 전에는 이 예외가 워커를
  죽였고(1-1과 겹쳐 보였음), 1-1 수정 후에도 이 예외 자체는 남아 있어 **사실상 모든 비전 판별
  잡이 매번 실패**하고 있었다. `soccer_highlights.py` 모듈 로드 시 `sys.stdout`/`stderr`를 UTF-8로
  `reconfigure`하도록 고쳐 해결. 실제 11개 후보 판별을 재실행해 에러 없이 완료됨을 확인($0.0091).
- **테스트 중 발견한 운영상 위험(코드 버그 아님, 사용자 인지 필요)**: 브라우저 localStorage에 저장된
  "생성 완료 후 자동 YouTube 업로드" 체크박스 상태가 페이지를 새로고침해도 그대로 유지된다.
  스모크 테스트에서 실제로 이 값이 켜져 있어, 리뷰→빌드 테스트 중 실제 YouTube 채널로 업로드가
  자동 시도됐다(다행히 인증 토큰이 만료되어 있어 실패, 실제 업로드는 발생하지 않음 — 로그로 확인).
  이 옵션은 한번 켜두면 브라우저를 껐다 켜도 계속 켜진 채로 유지되어, 이후 테스트/재처리 시에도
  모르는 사이 실제 채널에 업로드될 수 있다는 뜻이므로 사용 시 주의가 필요하다. (Phase 3에 항목 추가 권장)
- 테스트로 생성된 산출물(highlights_highlights_1경기.mp4, results_90e1aa65.json, temp 작업폴더)은
  모두 정리 완료. `youtube_token.json` 재인증이 필요한 상태로 보임(RefreshError: invalid_grant) —
  이는 기존 토큰 만료로 이 세션의 변경과 무관.

**Phase 2 완료.** 2-1 ~ 2-7 전항목 실행. 매 항목마다 실제 서버 기동 후 큐 추가 → 판별 → 승인 →
빌드까지 스모크 테스트로 검증(항목별로 반복, 총 5회 이상 전체 파이프라인 실행).

- 2-6 로그 로테이션: `RotatingFileHandler(maxBytes=2MB, backupCount=3)`로 교체.
- 2-7 파일 정리: 미사용 중복 `client_secret.json` 삭제(코드는 `client_secrets.json`만 참조,
  grep으로 확인 후 삭제). `PROJECT.md`의 stale 내용(단일 파일 구조 설명, 존재하지 않는
  `/api/progress` 언급)을 실제 다중 파일 구조에 맞게 갱신.
- 2-3 `config.py` 신설: `app.py`/`youtube_uploader.py`/`band_poster.py`에 중복돼 있던 `.env` 파싱
  로직과 ffmpeg 설치 확인 로직을 통합. 다른 두 모듈은 `import config`로 전환.
- 2-4 하드코딩 제거: "한울타리 FC 경기영상"(app.py 4곳 + index.html 7곳) → `config.get_default_title()`
  / JS `DEFAULT_TITLE`(Jinja 주입, `.env`의 `DEFAULT_TITLE`로 재정의 가능). 포트 5000(5곳) →
  `config.get_port()`(`.env`의 `HL_PORT`로 재정의 가능, YouTube/BAND 리디렉션 URI 포함).
- 2-5 산출물 디렉터리 정리: `output/`(하이라이트 mp4), `results/`(판별 결과 json) 신설,
  기존 파일 각각 11개/14개 이동, `.gitignore` 갱신. 시작 시 이전 세션의 고아 `hl_*` 임시
  작업폴더를 정리하는 `_cleanup_stale_temp_dirs()` 추가(실제 실행 시 과거 세션 잔여물 14개
  정리 확인됨). CLI(`soccer_highlights.py --output`) 기본 동작은 변경하지 않음(웹 앱 전용 정리).
- 2-2 `app.py`(927→135줄) Blueprint 분리: `jobs.py`(잡 저장소·워커·처리 파이프라인),
  `routes_jobs.py`/`routes_auth.py`/`routes_band.py`(Flask Blueprint), `tray.py`(트레이 아이콘).
  Phase 1에서 추가한 취소/지연삭제 로직이 모듈 경계를 넘나드는 부분(빌드 중 취소 확인,
  YouTube 자동 업로드 트리거)이 정확히 이전됐는지가 핵심 리스크였음 — 처리 중 삭제 →
  판별 중 취소 → 지연 삭제 완료 전체 흐름을 재현해 검증함.
- 2-1 `templates/index.html`(1897→335줄) 분리: `static/css/app.css` +
  `static/js/{state,utils,notify,queue,pipeline,review,build,main}.js`. Jinja 상수는
  `window.HL_CONFIG` 객체 하나로 모으고, `state.js`가 이를 destructure해 이후 로드되는
  파일들이 기존과 동일한 전역 식별자(`CONF_AUTO` 등)로 참조하도록 함(classic `<script>`가
  전역 렉시컬 스코프를 공유하는 성질 이용 — 실제로 `typeof CONF_AUTO`가 다른 파일에서도
  `"number"`로 확인됨). 원본에 있던 사용되지 않는 `_origGo` 변수는 정리하며 제거.
  실제 브라우저에서 정적 파일 로드(200 OK) + 전체 기능 버튼 클릭까지 검증.
- 전 항목에 걸쳐 실제 YouTube 업로드가 트리거되지 않도록 매 테스트 전 `localStorage`의
  자동 업로드 플래그를 확인하며 진행(Phase 1에서 겪은 실수 재발 방지).

**Phase 3 완료.** 3-1 ~ 3-7 전항목 실행. 스크린샷으로 실제 렌더링까지 시각 검증함.

- 3-7 자동 업로드 영속화 위험 제거: `restoreState()`에서 `autoUpload`를 더 이상 localStorage에서
  복원하지 않음 — 매 세션 기본 꺼짐으로 시작. `onAutoUploadToggle`의 `LS.set` 호출도 제거.
- 3-1 리뷰 탭 영상 미리보기: `routes_jobs.py`에 `/api/jobs/<jid>/source`(원본 영상,
  `send_file(conditional=True)`로 Range 지원) 신설. `review.js`에 `<video>` 엘리먼트 + 후보별
  "▶ 미리보기" 버튼(`previewCand`) 추가 — 클릭 시 `loadedmetadata` 이벤트 후 `currentTime`을
  `peak - pre_sec`로 점프해 재생. 스크린샷으로 실제 경기 장면 재생 확인함.
- 3-2 후보 썸네일: 이미 비전 판별용으로 추출돼 workdir에 남아있는 프레임(`cand{idx}_*.jpg`)을
  재활용 — 새 추출 없이 `/api/jobs/<jid>/thumb/<idx>` 라우트가 중간 프레임을 서빙. 리뷰
  테이블에 썸네일 컬럼 추가. 스크린샷으로 실제 이미지 렌더링 확인함(첫 확인 시도에서 브라우저
  세션이 일시적으로 불안정해 이미지 로드가 멎었으나, 새 세션에서 재확인해 정상 동작 검증 —
  `fetch()` 직접 호출로 서버 응답(유효한 JPEG, 정확한 헤더)과 디스크 파일을 먼저 교차 검증한
  뒤 브라우저 쪽 문제임을 특정함).
- 3-3 빌드 탭 포커스 유지: `renderBuild()`가 입력값 수집은 항상 수행하되, 포커스가
  `[data-bd-title]`/`[data-bd-out]` 안에 있으면 destructive innerHTML 재렌더를 건너뜀.
- 3-4 API 키 없음 배지: 큐/리뷰 탭에 `!vision_used`인 잡에 "⚠ AI 판별 생략됨" 표시.
- 3-5 접근성: `esc()`에 작은따옴표 이스케이프 추가, 아코디언에 `role="button"`/`aria-expanded`/
  키보드(Enter·Space) 지원, 탭에 `role="tab"`/`aria-selected`, `:focus-visible` 아웃라인 CSS.
- 3-6 서버 로그 UI: `app.py`에 `/api/logs?tail=N` 라우트, 큐 탭에 "서버 로그" 카드 +
  보기/새로고침 버튼(`toggleServerLog`).
- 이 과정에서 얻은 교훈: 브라우저 미리보기 세션이 오래 지속되면(수십 회의 DOM 조작·폴링 누적)
  스크린샷/이미지 디코딩이 간헐적으로 멎을 수 있음 — 원인이 코드인지 세션 노후화인지 애매하면
  `fetch()`로 서버 응답을 직접 검증하고, 필요시 서버를 재시작해 새 세션에서 재확인하는 것이 빠름.

**Phase 4 완료.** 4-1 ~ 4-4 전항목 실행. `pytest`/`ruff` 설치 후 실제로 실행해 통과를 확인했고,
4-3/4-4는 실서버·실ffmpeg로 통합 검증까지 마쳤다.

- 4-1 테스트 도입: `tests/` 신설, 52개 테스트 전부 통과.
  - `detect_spikes`를 ffmpeg I/O부와 순수 신호처리부(`detect_spikes_from_signal`)로 분리해
    합성 사인파+스파이크 신호로 유닛 테스트 가능하게 만듦(테스트 가능성을 위한 최소 리팩터).
  - `select_segments`, `jobs.flag`, `youtube_uploader.generate_description`(챕터 타임스탬프
    누적 로직), `band_poster.format_post_content`/`group_by_date` 유닛 테스트.
  - `test_app_routes.py`: Flask `test_client`로 큐 추가 → 승인 → 빌드 스모크 테스트.
    실제 ffmpeg/Gemini 없이 검증하기 위해 "ready" 상태 잡을 `jobs.JOBS`에 직접 주입하고
    `soccer_highlights.build_output`을 몽키패치로 목 처리해 라우팅·상태 전이만 검증.
  - `test_build_output.py`: 4-4에서 병렬화한 클립 인코딩의 순서 보장·진행률·취소를 검증
    (아래 4-4 항목과 함께 작성).
- 4-2 ruff/pyproject.toml: `pyproject.toml`에 ruff 설정(E/F/W/I/UP 규칙, E501·E701은 이 코드베이스의
  기존 스타일과 상충해 ignore) + pytest `testpaths`. `ruff check --fix`로 23건 중 22건 자동 수정
  (미사용 import, 불필요한 f-string, import 정렬, `Optional[X]` → `X | None`), 나머지 4건(E701)은
  기존에 코드베이스 전반에서 의도적으로 쓰는 `try:/except:` 한 줄 압축 스타일이라 ignore 처리.
  `requirements.txt`를 설치된 버전 기준 범위(`>=x.y,<x+1`)로 고정, dev 전용
  `requirements-dev.txt`(pytest/ruff) 분리.
- 4-3 잡 상태 영속화: `jobs.restore_recent_results()` — 시작 시 `results/*.json`을 스캔해
  "ready" 잡으로 복원. **다만 그대로 구현하면 위험한 회귀가 있었음**: `results/`에 이미
  과거 세션에서 쌓인 히스토리 파일 14개가 있어(6월~7월 초), 모든 파일을 무조건 복원하면
  재시작할 때마다 사용자가 이미 처리·정리한 오래된 잡들이 리뷰 큐에 계속 되살아나는
  회귀가 생길 뻔했다 — 구현 전 `results/` 실제 내용을 먼저 점검해 발견하고, 24시간 이내
  파일만 복원하도록 제한. 함께, 잡 삭제 시(`finalize_pending_delete`/즉시 삭제 양쪽) 대응하는
  `results_{jid}.json`도 정리하도록 `delete_results_file()`을 추가해 향후 orphan 파일 누적을 방지.
  합성 결과 파일로 실서버 기동 검증: 24시간 이내 파일 1개만 복원되고 과거 14개는 복원되지
  않음을 확인.
- 4-4 빌드 클립 인코딩 병렬화: `build_output`의 클립별 재인코딩 루프를
  `ThreadPoolExecutor(BUILD_CLIP_WORKERS=3)`로 병렬화(ffmpeg는 별도 프로세스라 GIL 무관).
  concat 순서는 완료 순서가 아니라 `clip{i:03d}.mp4` 파일명으로 보장. 취소 확인은
  `classify_all_parallel`과 같은 패턴(진행 중인 것은 끝까지, 대기 중인 것만 취소).
  실제 ffmpeg로 11구간짜리 실제 하이라이트 영상을 빌드해 소요 시간(약 98초)과 결과물
  duration(143.2초 = 11×13초, 클립 순서 정확히 보존)을 확인.

---

## Phase 1 — 버그 수정 (즉시, 반나절)

### 1-1. [치명] 처리 중인 잡 삭제 시 워커 스레드 사망
- `app.py` `_worker()`: `_process(jid)` 실패 시 except 블록에서 `_upd(jid, ...)`를 호출하는데,
  그 사이 `/api/jobs/<jid>/delete`로 잡이 삭제됐으면 `JOBS[jid]`가 KeyError → except 블록 안에서
  예외가 다시 발생 → **워커 스레드가 죽고 이후 큐 전체가 멈춘다.**
- 또한 pending 잡을 삭제해도 `JOB_QUEUE`에 jid가 남아 워커가 없는 잡을 꺼내 처리 시도.
- 수정: `_upd`/`_set_prog`/`_clr_prog`가 없는 jid를 무시하도록 방어. `_worker`에서 큐에서 꺼낸 jid가
  JOBS에 없으면 skip. 처리 중(status가 detecting/classifying/building)인 잡은 삭제 버튼 비활성화
  또는 삭제 시 취소 플래그 처리.

### 1-2. [높음] 정의되지 않은 CSS 변수 19곳 — 스타일 미적용
- `templates/index.html`의 `:root`에는 `--bg --panel --line --txt --muted --accent --warn --rej --blue --red --purple`만 정의되어 있는데,
  원스톱 설정 패널·인증 상태 표시 등 19곳에서 `var(--border)`, `var(--hint)`, `var(--green)`, `var(--surface)`를 사용.
- 결과: "✔ YouTube 인증됨" 초록색 미표시, 설정 패널 테두리/배경 미적용 등 시각적 결함.
- 수정: `:root`에 alias 추가(`--border:var(--line); --hint:var(--muted); --green:var(--accent); --surface:#0d1117;`)가 가장 안전.

### 1-3. [중간] 원스톱 시 워터마크에 전체 YouTube 제목이 새겨짐
- `advancePipeline()` building 단계에서 `titles[j.id] = ytTitleFor(...)`(= "베이스 | 영상명 | 날짜 | 번호")를
  `/api/jobs/build-all`에 넘기고, 서버는 이 값을 **drawtext 워터마크**로 사용.
- 결과: 영상 우상단 워터마크에 날짜·번호까지 새겨짐. 워터마크는 베이스 제목만, YouTube 제목은 전체 형식이어야 함.
- 수정: build-all에는 base title, auto_upload용 yt 제목은 별도 파라미터(`yt_titles`)로 분리.

### 1-4. [중간] 새로고침 복원된 파이프라인이 영원히 멈출 수 있음
- localStorage에서 `_pipe` 복원 후 서버가 재시작돼 JOBS가 비었으면 `advancePipeline`에서
  `pJobs.length === 0 → return`만 반복 → 버튼이 영구 잠김.
- 수정: 복원 후 N회 폴링(예: 10회) 동안 해당 jobId가 하나도 없으면 자동 취소 + 로그 안내.

### 1-5. [낮음] 취소 기능 부재 (PROJECT.md와 불일치)
- PROJECT.md에는 `/api/cancel` + `should_cancel`이 구현됐다고 기록돼 있으나 현재 `app.py`에는 라우트가 없음.
  `classify_all_parallel`은 `should_cancel` 파라미터를 지원하는데 웹 앱이 안 씀.
  UI의 파이프라인 "취소"는 클라이언트 상태만 리셋하고 서버 작업은 계속 돈다.
- 수정: 잡별 `cancel_requested` 플래그 + `/api/jobs/<jid>/cancel` 라우트 추가,
  `_process`에서 `should_cancel=lambda: JOBS[jid].get("cancel_requested")` 전달.
  빌드 루프도 클립 사이마다 플래그 확인. UI 취소 버튼이 이 API를 호출하도록 연결.

### 1-6. [낮음] JOBS 락 사용 불일치
- `api_jobs_build_all`, `api_post_band`, `_upload_job_to_youtube` 등이 `_JLOCK` 없이 JOBS를 순회/수정.
  GIL 덕에 대부분 안전하지만 순회 중 삭제 시 RuntimeError 가능.
- 수정: 스냅샷 패턴 통일 — 락 안에서 필요한 값만 복사 후 락 밖에서 사용.

---

## Phase 2 — 코드 구조 개선 (1~2일)

### 2-1. index.html(1,851줄) 분리
- 현재 CSS+HTML+JS가 한 파일. `static/css/app.css`, `static/js/` 로 분리:
  - `state.js` (전역 상태·localStorage), `api.js` (fetch 헬퍼), `queue.js`, `review.js`,
    `build.js`, `pipeline.js` (원스톱), `notify.js`, `utils.js`
  - ES module 없이 단순 `<script src>` 순서 로드로 충분 (빌드 도구 불필요).
- Jinja 주입 상수는 `<script>window.HL_CONFIG = {{ ... | tojson }}</script>` 한 블록으로 모음.

### 2-2. app.py(927줄) 모듈 분리
- `jobs.py` — JOBS 저장소·워커·_process (락 캡슐화: JobStore 클래스로)
- `routes_jobs.py` / `routes_auth.py` / `routes_band.py` — Flask Blueprint
- `tray.py` — 트레이 아이콘 관련
- `config.py` — 아래 2-3 포함

### 2-3. .env 파싱 4중 중복 제거
- `app.load_api_key`, `app._get_yt_privacy`, `youtube_uploader._load_env`, `band_poster._load_env`가
  같은 파싱 로직을 반복. `config.py` 하나로 통합 (또는 `python-dotenv` 도입).
- ffmpeg 체크도 `_ffmpeg_ok()`/`_check_ffmpeg()` 중복 → 하나로.

### 2-4. 하드코딩 제거
- `"한울타리 FC 경기영상"` 기본 제목이 app.py 3곳 + index.html 6곳에 하드코딩 → `.env`의
  `DEFAULT_TITLE`로 이동, Jinja로 UI에 주입.
- 포트 5000이 app.run·redirect URI 2곳·안내 문구에 흩어짐 → `PORT` 상수/환경변수화.

### 2-5. 산출물 디렉터리 정리
- 프로젝트 루트에 `results_*.json` 14개, `highlights_*.mp4` 10개(약 3GB)가 쌓임.
- `output/` (하이라이트 영상), `results/` (JSON) 하위 디렉터리로 분리 저장.
  기본 출력 경로 변경 + .gitignore 갱신 + 기존 파일 이동 안내.
- 시작 시 오래된 `hl_*` 임시 폴더(temp) 청소 루틴 추가 (앱 강제종료 시 잔류물 제거).

### 2-6. 로그 로테이션
- `app.log`가 이미 1.6MB. `RotatingFileHandler(maxBytes=2MB, backupCount=3)`로 교체.

### 2-7. 파일 정리
- `client_secret.json`과 `client_secrets.json` 두 파일이 공존 (코드는 후자만 사용) → 전자 삭제.
- PROJECT.md의 stale 내용(단일 파일 구조, /api/cancel, /api/progress) 갱신.

---

## Phase 3 — UI/UX 개선 (1~2일)

### 3-1. [효과 큼] 리뷰 탭 구간 미리보기
- 현재 리뷰는 텍스트(시각·유형·신뢰도·설명)만 보고 판단해야 함 — 실제 장면 확인 불가.
- 원본 영상을 Range 지원으로 서빙(`send_file(..., conditional=True)` + `/api/jobs/<jid>/source`)하고,
  후보 행 클릭 시 `<video>`의 `currentTime = peak - pre_sec`로 점프 재생.
  체크박스 옆 "▶ 미리보기" 버튼 하나로 리뷰 품질이 크게 오름.

### 3-2. 후보 썸네일 표시
- 비전 판별용으로 이미 프레임을 추출하므로, 후보당 대표 프레임 1장(peak 시점)을
  `workdir`에 보존하고 `/api/jobs/<jid>/thumb/<idx>`로 서빙 → 리뷰 테이블에 작은 썸네일 컬럼.

### 3-3. 입력 중 재렌더로 포커스 상실 방지
- 영상 생성 탭의 제목/파일명 input이 2초 폴링 재렌더 시 포커스를 잃음.
- 수정: `document.activeElement`가 해당 테이블 안이면 재렌더 스킵, 또는 input을 렌더 해시에서 제외하고
  값만 보존(현재도 수집은 하지만 포커스는 복원 안 됨).

### 3-4. API 키 없음 상태 명확화
- 키가 없으면 잡이 조용히 vision 없이 "준비됨"이 됨(모든 후보가 채택 취급).
- 큐/리뷰에 "⚠ AI 판별 생략됨 (GEMINI_API_KEY 없음)" 배지 표시.

### 3-7. [Phase 1 스모크 테스트에서 발견] "자동 업로드" 체크박스 영속화 위험
- "생성 완료 후 자동으로 YouTube 업로드" 체크박스 상태가 localStorage에 저장되어 브라우저를
  새로고침/재시작해도 그대로 유지된다. 한 번 켜두면 이후 세션에서도 모르는 사이 실제 채널에
  업로드가 시도될 수 있다(Phase 1 스모크 테스트 중 실제로 재현됨 — 다행히 만료된 토큰 덕에
  실제 업로드는 안 됐지만, 토큰이 유효했다면 테스트 영상이 실채널에 올라갔을 것).
- 수정 방향: 페이지 로드 시 이 옵션은 기본 꺼짐으로 시작(영속화 제외)하거나, 켜져 있을 때
  세션마다 한 번은 확인 문구를 보여주는 방식 권장.

### 3-5. 접근성·시각 다듬기
- 상태를 색으로만 구분하는 부분에 텍스트/아이콘 병행 (이미 대부분 텍스트 있음 — 점검 수준).
- 버튼 최소 크기·포커스 아웃라인, 아코디언에 `aria-expanded`, 탭에 `role="tab"`.
- `esc()`에 작은따옴표 이스케이프 추가 (title 속성 안전성).

### 3-6. 서버 로그 UI 노출 (선택)
- 클라이언트 로그 패널은 새로고침 시 소실. `/api/logs?tail=100`으로 app.log 끝부분을 보여주면
  문제 발생 시 사용자가 원인 파악 가능.

---

## Phase 4 — 품질 기반 (1일)

### 4-1. 테스트 도입 (pytest)
- 순수 함수부터: `detect_spikes`(합성 사인파+스파이크 wav로), 탈중복 로직, `_flag`,
  `select_segments`, `generate_description`(챕터 타임스탬프), `format_post_content`, `group_by_date`.
- Flask 라우트는 test_client로 add→approve→(mock build) 흐름 스모크 테스트.

### 4-2. 도구 설정
- `pyproject.toml` + `ruff` (lint/format). requirements.txt 버전 고정(`pip freeze` 기반 범위 지정).

### 4-3. (선택) 잡 상태 영속화
- 서버 재시작 시 JOBS가 사라져 리뷰 중이던 결과 접근 불가. 이미 `results_{jid}.json`을 저장하므로
  시작 시 이를 스캔해 "ready" 상태로 복원하는 로드 루틴 추가.

### 4-4. (선택) 빌드 클립 인코딩 병렬화
- `build_output`의 클립별 재인코딩을 `ThreadPoolExecutor(2~3)`로 병렬화 (ffmpeg는 프로세스라 GIL 무관).
  30분 영상 수십 클립에서 체감 단축. concat 순서는 파일명으로 보장됨.

---

## 진단 시 확인된 양호한 점 (변경 불필요)
- 시크릿 관리: `.env`, `client_secrets.json`, 토큰 파일 모두 gitignore 처리·미커밋 확인됨.
- Flask는 127.0.0.1 바인딩 (외부 노출 없음).
- 비전 호출 병렬화·지수 백오프·후보 단위 예외 격리·Windows 특유 문제(WinError 5, cp949, drawtext 콜론) 대응이 잘 되어 있음.
- 렌더 해시 캐싱, localStorage 영속화 등 프론트 최적화 기본기 있음.

## 실행 순서 권장
1 → 2 → 3 → 4 순서. Phase 1은 항목별 커밋, Phase 2는 2-1과 2-2를 별도 브랜치/커밋으로.
각 Phase 완료 후 `python app.py`로 기동 + 큐 추가~리뷰~생성 스모크 확인.
