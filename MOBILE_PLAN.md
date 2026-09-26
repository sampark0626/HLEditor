# 아이폰 원스톱 자동화 계획 (2026-09-26, Opus 5.5 설계)

경기 직후 **아이폰에서 영상만 올리면, 알림이 올 때 BAND에 붙여넣기만 하면 되는** 흐름을 만든다.
실행은 Sonnet 등 다른 모델이 이 문서를 기준으로 진행한다. Phase 순서대로 진행한다.

**[사용자]** 로 표시된 단계는 사용자 계정·브라우저 동의·토큰 발급이 필요하다. 구현 모델이
대신할 수 없으니, 그 지점에서 멈추고 사용자에게 무엇을 해 달라고 할지 구체적으로 요청한다.

## 실행 로그

### 2026-09-26 — Opus 5.5가 코드 전체 구현 (사용자 계정 단계만 남음)

사용자가 "계획에 맞춰 끝까지 구현"을 요청해 Sonnet 대신 이 세션에서 바로 구현했다.
**코드·테스트·문서는 모두 끝났다. 남은 것은 [사용자] 단계(계정·브라우저·아이폰)뿐이다.**
정리한 절차는 [kaggle_runner/INBOX_SETUP.md](kaggle_runner/INBOX_SETUP.md)에 있다.

| Phase | 상태 |
|---|---|
| 0 | 0-2 완료(`tools/make_kaggle_secrets.py`). 0-1·0-3·0-4·0-5는 [사용자] 대기. INBOX_SETUP.md 1~3·6단계에 합쳐 둠 |
| 1 | 1-1~1-4 완료 |
| 2 | 2-1~2-6 완료. 로컬 E2E 통과(아래) |
| 3 | 3-1·3-2 완료(코드). 3-3·3-4 [사용자] 대기 |
| 4 | 4-1 완료(Node 가짜 환경 테스트 19개). 4-2·4-3 [사용자] 대기 |
| 5 | 5-1 코드 완료(실제 GPU 환경은 미검증, 유닛 테스트로 흐름만 확인), 5-2·5-3·5-4 완료 |

**로컬 E2E (실제 영상, Gemini·YouTube·ntfy만 dry-run)**
- 입력:
  - 9/6 경기 41.7분을 `-c copy`로 30:00과 11:39 두 파일로 잘랐다(`creation_time` 04:05Z / 04:35Z).
  - 다른 경기에서 6분을 잘라 넣었다(06:10Z).
- 결과:
  - 두 파트가 **1경기**로 병합되고, 6분짜리는 **2경기**로 따로 처리됐다.
  - 1080p 하이라이트가 2개(130초 / 126초) 만들어졌고, **우상단 워터마크**도 확인했다.
  - BAND 글이 누적되고(2경기 알림에 두 경기 링크 모두), 원본은 `03_done`으로, `_state`에는 claims·day·results가 남았다. 임시 파일은 정리됐다.
- 시간(이 PC, 판별 없음): 1경기는 병합·검출·팬 분석 2분 + 빌드(10구간) 54초, 2경기는 1분 12초. 전체 4분 20초.
- 팬 궤적 결과(이동폭 588.7px, 보정 44개)가 9/6 PC 앱 로그(589px, 43개)와 사실상 같다.
  → 분할→병합을 거쳐도 신호가 보존된다.

**계획과 달라진 점·추가로 발견한 것**
- **기존 버그 수정:** 이 PC에는 나눔고딕이 없어서 **PC 앱 하이라이트에도 워터마크가 조용히 빠지고 있었다**.
  9/6 영상 캡처로 확인했다. `TITLE_FONT`를 후보 목록으로 바꿔 맑은 고딕으로 대체한다(1-1 범위 안).
  이제 PC에서 만드는 영상에도 워터마크가 들어간다.
- **Kaggle 인증:** Apps Script는 `KGAT_` 토큰은 Bearer로, 예전 `kaggle.json` key는 아이디와 함께 Basic으로 보낸다.
  사용자가 어느 토큰을 받아도 동작하게 하려는 것이다.
- **알림 추가:** 인증 만료 말고도 실행 전체가 멈추는 오류는 `HLEditor 처리기 오류`로 알린다(`_notify_fatal`).
- **시도 횟수 규칙:** 커널이 처리 도중 죽은 경우만 시도 횟수를 누적한다. 사용자가 `04_failed`에서 `01_inbox`로 되돌린 파일은 1회부터 다시 센다.
- **로컬 시험용 옵션:** `--local-inbox`, `--dry-run`, `--no-vision`, `--max-segments`, `--settle-sec`, `--wait-part-min`, `--once`.
- **run_match 정리:** 2D 코드를 `run_dotplay()`로 분리했다. `ENABLE_DOTPLAY=False`면 dotplay import와 CV 패키지 설치를 하지 않는다.

**테스트**
- pytest 189개 통과(기존 108 + 신규 81). 신규 파일: `test_grouping`, `test_stores`(가짜 Drive), `test_inbox_runner`,
  `test_notify`, `test_make_kaggle_secrets`, `test_title_font`. `test_youtube_uploader`, `test_app_routes`에는 테스트를 추가했다.
- `node kaggle_runner/apps_script/code_test.js`: 19개 통과.
- ruff: 새로 만들거나 고친 파일은 오류 0. 저장소 전체의 기존 47건은 그대로 뒀다(이번 범위 밖).

**첫 실제 실행 전에 꼭 할 것**
1. **GitHub main에 push.** `kernel_bootstrap.py`가 main을 clone하므로, push하기 전엔 Kaggle에서 새 코드가 없다.
2. INBOX_SETUP.md 1~6단계.
3. 첫 시험에서 확인할 설계 가정:
   - Kaggle API 응답 형식(상태 문자열 등). Apps Script `showStatus`로 확인한다.
   - 폰 업로드 파일의 화질과 `creation_time` 유무. 커널 로그의 `받음:` 줄과 알림의 압축 경고로 확인한다.
   - XbotGo가 정확히 30:00에서 파일을 자르는지.

---

## 1. 목표와 범위

### 1-1. 완성 후 사용자 흐름

1. 경기가 끝나면 **Drive 앱 → `HLEditor/01_inbox` → + → 업로드 → 사진 및 동영상**에서 영상을 고른다.
   XbotGo 앱의 영상은 먼저 사진 앱에 저장해 둔다. 30분 단위로 나뉜 파일은 한 번에 같이 고른다.
   업로드가 끝날 때까지 Drive 앱을 켜 둔다(5G로 4~10분).
2. (자동) Apps Script가 **일·월요일엔 5분마다**, 그 외 요일엔 1시간마다 인박스를 확인한다. 영상이 있으면 Kaggle을 시작하고 "처리 시작" 알림을 보낸다.
   (선택) 아이폰 단축어 자동화를 켜 두면 Drive 앱에서 나오는 순간 바로 시작한다(3-3).
   처리는 병합 → 오디오 후보 → [팬 분석 ∥ Gemini 판별] → 채택 → 빌드 → YouTube 업로드(제목·챕터·썸네일) 순서로 진행한다.
3. ntfy 알림이 오면 알림을 탭하고, ntfy 앱에서 메시지를 탭해 **BAND 글 전체를 복사**한 뒤 BAND에 붙여넣는다.

2D(Dot Play)가 필요한 경기만 `01_inbox_2d`에 올린다. 그러면 하이라이트 알림이 먼저 오고, 2D 결과는 별도 알림으로 온다.
2D 완성도가 아직 낮아서 **기본은 끔**이다.

### 1-2. 서버·웹호스팅: 필요 없음

| 역할 | 구성요소 | 비용 |
|---|---|---|
| 업로드 받는 곳 | Google Drive `HLEditor/` 폴더 (사용자 구글 계정) | 무료 (공간 부족 시 Google One) |
| 자동 실행 스위치 | Google Apps Script 주기 확인(일·월 5분 / 그 외 1시간). 선택: 아이폰 단축어 자동화로 즉시 실행. 자세한 규칙은 3-3 | 무료 |
| 처리 | Kaggle 비공개 스크립트 커널 (평소 CPU, 2D 요청 시만 GPU T4) | 무료 (GPU는 주간 한도) |
| 알림 | ntfy (iOS 앱 + ntfy.sh 공개 서버) | 무료 |
| AI 판별 | Gemini API | 경기당 $0.10~0.14 (지금과 같음) |

### 1-3. 비목표 (v1에서 하지 않음)

- **폰에서 후보 검토:** 자동 판단(`CONF_AUTO` + 팬 보정)으로만 채택한다.
- **BAND 자동 게시:** `.env`에 BAND API 키가 없다. 알림 본문을 복사해 붙여넣는다.
- **XbotGo 클라우드 공유 링크에서 원본 받기:** 아직 검증하지 않았다(PROJECT.md 보류 항목).
- **이미 처리한 경기에 나중에 2D만 추가:** v2 후보다. v1은 그때 쓸 결과 JSON을 Drive에 남기는 데까지만 한다.
- **PC 웹 앱 기능 변경:** PC 앱은 그대로 둔다. 공용 모듈 리팩터는 PC 앱 동작이 바뀌지 않게 한다.

---

## 2. 설계 근거 (확인된 사실)

| # | 사실 | 근거 | 설계 반영 |
|---|---|---|---|
| F1 | XbotGo 원본은 1080p30 H.264 약 8.3Mbps라 **경기당 2.1~2.6GB** | `output/_merged/*.mp4` ffprobe (2026-09-26) | 폰 업로드 4~10분. 커널 디스크는 경기 단위로 비워 가며 쓴다 |
| F2 | PC(Core Ultra 5 226V)에서 경기당 팬 5~7분, Gemini 4~8분($0.10~0.14), 빌드 5~14분, YouTube 5~7분 | app.log 2026-09-06 | 팬과 Gemini를 **병렬**로 돌린다 |
| F3 | Kaggle 러너는 30분 경기의 하이라이트 추출에 15~20분 걸림 (팬 분석 제외) | `kaggle_runner/README.md` 실측 | 목표 처리시간은 경기당 20~25분 |
| F4 | iOS 사파리 파일 입력은 **사진 보관함 영상을 720p 수준으로 압축**함 | Apple Developer Forums thread 731042, addpipe 블로그 | 사파리 업로드는 쓰지 않고 Drive 앱으로 올린다 |
| F5 | Drive iOS 앱도 대용량 업로드 중에는 앱을 전면에 둬야 안정적 | 2026-09 검색 (사용자 커뮤니티) | 사용 안내에 명시한다 |
| F6 | Kaggle API는 `POST https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel` (**저장과 실행을 한 번에**), 인증은 `Authorization: Bearer <KGAT 토큰>`. 상태 조회는 `.../GetKernelSessionStatus` 에 `{userName, kernelSlug}`를 보내 `status`(QUEUED/RUNNING/COMPLETE/ERROR/…)를 받음 | Kaggle/kagglesdk 소스 (`kaggle_http_client.py`, `kaggle_env.py`, `kernels/services/kernels_api_service.py`) | Apps Script가 직접 호출한다. Phase 0에서 curl로 먼저 검증 |
| F7 | API로 push한 커널에서는 **Kaggle Secrets(`UserSecretsClient`)가 동작하지 않음** | Kaggle/kaggle-cli issue #582, Kaggle 포럼 가이드 | 비밀값은 **비공개 데이터셋**의 JSON 파일로 전달한다 |
| F8 | ntfy iOS 앱은 click URL이 **없는** 메시지를 탭하면 본문을 복사함 | binwiederhier/ntfy-ios PR #37 | 완료 알림은 click 없이 BAND 글 전체를 본문으로 보낸다 |
| F9 | BAND API 키가 설정되어 있지 않음 | `.env` | BAND 자동 게시는 제외 |
| F10 | 사용자는 8월에 approve-all(AI 일괄 승인)을 주로 씀 | app.log | 검토 없는 자동 흐름을 채택한다 |
| F11 | YouTube OAuth 앱이 '테스트' 상태면 refresh token이 7일 만에 만료됨 | PROJECT.md | 무인 실행이므로 **프로덕션 게시가 필수** |
| F12 | 워터마크 폰트가 `C:\Windows\Fonts\NanumGothic.ttf`로 하드코딩됨. 폰트가 없으면 워터마크만 빠지고 빌드는 성공함 | `soccer_highlights.py:100`, `build_output` | 폰트 경로를 설정할 수 있게 바꾼다 |
| F13 | YouTube·BAND OAuth 리디렉션 주소가 localhost로 고정됨 | `routes_auth.py:27`, `routes_band.py:25` | Kaggle은 refresh token만 쓰므로 리디렉션이 필요 없다. 토큰은 PC 헬퍼로 발급한다 |
| F14 | 지금 Kaggle 러너(`run_match.py`)는 팬 보정·분할 병합·YouTube 업로드가 없고, 2D를 항상 시도함. Output은 **실행이 다 끝나야** 받을 수 있음 | `run_match.process_match` | 새 러너(`inbox_runner`)가 업로드까지 직접 한다. `run_match`에도 2D 토글과 팬 보정을 추가한다 |
| F15 | Drive 자체 푸시 알림(`changes.watch`)을 받으려면 유효한 인증서의 공개 HTTPS 웹훅이 필요함. 채널은 최대 7일(`files.watch`는 1일)마다 갱신해야 하고, 알림 내용은 헤더에만 담김. Apps Script 웹 앱은 요청 헤더를 읽지 못함 | Google Drive API push 가이드, labnol (2026-09 확인) | **채택 안 함.** 별도 서버리스 엔드포인트가 필요해 "호스팅 없이"라는 조건이 깨진다. 기본은 주기 확인이다. 즉시성이 필요하면 **폰이 업로드 종료를 알리는** 선택 경로를 쓴다: F5 때문에 업로드 중엔 Drive 앱을 켜 둬야 하므로, Drive 앱을 나오는 시점이 곧 업로드가 끝난 시점이다. 이를 iOS 단축어 자동화로 잡는다 |

---

## 3. 아키텍처

```
[iPhone] Google Drive 앱 ─업로드→ Drive: HLEditor/01_inbox (또는 01_inbox_2d)
                                                 │ 파일 수 확인
[Google Apps Script tick()] ◀── 타이머 (일·월 5분 / 그 외 1시간)
                            ◀── (선택) 단축어 자동화: Drive 앱을 나올 때 실행 링크 doGet?key=… 호출
  inbox에 영상 있음 + 커널이 실행 중 아님
  → ntfy "처리 시작" + Kaggle SaveKernel(push=실행)
                                   │
[Kaggle 비공개 커널 hleditor-inbox]  (평소 CPU / 01_inbox_2d에 파일 있으면 GPU T4)
  kernel_bootstrap: git clone → kaggle_runner.inbox_runner.main()
  루프:
    claim(01_inbox → 02_processing) → 다운로드 → ffprobe → 경기 묶기(분할 파트 체인)
    → 병합 → 오디오 후보 → [팬 분석 ∥ Gemini 판별] → 팬 보정 → 채택 → 빌드(워터마크)
    → YouTube 업로드 → _state 갱신 → ntfy 알림(BAND 글) → 원본 03_done
    (실패: 원본 04_failed + 실패 알림)   (2D 요청: Dot Play → 05_output → 2D 알림)
  inbox가 비고 기다리는 파트도 없으면 종료
                                   │
[ntfy.sh] ──푸시→ [iPhone ntfy 앱] → 탭해서 복사 → BAND에 붙여넣기
```

### 3-1. Drive 폴더 구조 (루트 `HLEditor/`)

| 폴더 | 용도 | 쓰는 쪽 |
|---|---|---|
| `01_inbox` | 폰이 올리는 곳 (하이라이트만) | 사용자 |
| `01_inbox_2d` | 2D까지 원하는 경기 | 사용자 |
| `02_processing` | 커널이 가져간(claim) 파일. 중복 처리를 막음 | 커널 |
| `03_done` | 처리가 끝난 원본. 보관 기간이 지나면 휴지통으로 | 커널 |
| `04_failed` | 실패한 원본. 자동 삭제하지 않음. 사용자가 `01_inbox`로 옮기면 재시도 | 커널/사용자 |
| `05_output` | 2D 결과, YouTube 업로드 실패 시 하이라이트 백업 | 커널 |
| `_state` | `days/{YYYY-MM-DD}.json` (날짜별 게시 목록), `results/{game_id}.json` (판별 결과) | 커널 |

커널과 Apps Script는 **루트 폴더 ID 하나**만 알고, 하위 폴더는 이름으로 찾는다.

### 3-2. 커널 처리 규칙

- **claim:** `files.update(addParents=02_processing, removeParents=01_inbox)`로 옮긴다. 이미 옮겨진 파일이면 건너뛴다.
  `appProperties.hl_attempts`를 1 올리고, 3 이상이면 바로 `04_failed`로 보내고 알림을 보낸다(무한 재시도 방지).
- **시작 시 복구:** 커널은 한 번에 하나만 돈다(3-3). 그래서 시작 시점에 `02_processing`에 남은 파일은 지난 실행이
  죽고 남긴 것이다. `01_inbox`(또는 원래 폴더)로 되돌린 뒤 다시 claim한다(attempts는 누적).
- **다운로드:** Drive API `alt=media`를 requests로 스트리밍하고 `md5Checksum`으로 검증한다. 받는 곳은
  **`/kaggle/working` 밖의 임시 디렉터리**다(working은 커널 출력으로 저장되고 20GB 제한이 있음). 경기 하나를 처리하면 바로 지운다.
- **메타데이터:** ffprobe로 duration, width/height, video bit_rate, `creation_time`
  (없으면 `com.apple.quicktime.creationdate`)을 읽는다.
- **화질 경고:** height < 1000 이거나 video bitrate < 4Mbps면 알림에 "원본이 압축된 것 같음" 줄을 붙인다(F4 대비).
- **최소 길이:** 경기(묶은 뒤 합계)가 `MIN_GAME_SEC`(기본 300초)보다 짧으면 처리하지 않는다.
  원본은 `04_failed`로 보내고 알림한다. 실수로 올린 영상이 공개 게시되는 것을 막기 위함이다.
- **경기 묶기(`grouping.py`, 순수 함수):**
  - 정렬 순서: `creation_time` → 없으면 Drive `createdTime` → 파일명 자연 정렬.
  - 연결: 앞 파일 길이가 `SPLIT_MIN_SEC`(기본 1770초 = 29.5분) 이상이고, 다음 파일의 시작이
    (앞 시작 + 앞 길이) ± 90초이면 같은 경기로 본다. `creation_time`이 없으면 정렬 순서만으로 연결한다.
  - 완결: 마지막 파트가 `SPLIT_MIN_SEC`보다 짧으면 완결이다. 아니면 다음 파트를 **대기**한다.
  - 대기 타임아웃: `WAIT_NEXT_PART_MIN`(기본 40분)이 지나면 있는 파트만으로 처리하고, 알림에 "다음 파트가 오지 않아 앞부분만 처리함"을 적는다.
  - 정착 대기: 가장 최근에 도착한 파일이 `SETTLE_SEC`(기본 180초) 이내에 들어왔으면, 조용해질 때까지 기다렸다가 묶는다.
    동시에 올린 파트가 순서가 뒤바뀌어 도착하는 경우에 대비한 것이다.
  - 한 실행 안에서는 완결된 경기를 녹화 시작 시각 순서로 처리한다.
- **날짜와 경기 이름:**
  - `match_date`는 녹화 시작 시각(UTC)을 **KST(Asia/Seoul)**로 바꾼 날짜다. 녹화 시각이 없으면 Drive `createdTime`을 KST로 바꿔 쓴다.
    Kaggle 서버 시계는 UTC라서 `date.today()`를 쓰면 안 된다.
  - 경기 이름은 `"{n}경기"`이고, n = 그날 `_state/days` 기록 수 + 1이다.
- **처리:** 기존 함수를 그대로 재사용한다.
  - 순서: `sh.concat_videos` → `sh.detect_spikes`(sensitivity `normal`) → [`pan_signal.compute_pan_series`를 스레드에서 ∥ `sh.classify_all_parallel`]
    → 둘 다 끝난 뒤 `pan_signal.annotate_candidates` → `sh.select_segments(cands, sh.CONF_AUTO, vision_used)`
    → `sh.build_output(title=DEFAULT_TITLE, 품질 balanced)`.
  - 팬과 Gemini는 작업 디렉터리를 나눠 쓴다(`work/pan`, `work/vision`).
  - 팬 분석이 실패해도 경고만 남기고 계속한다(`jobs._process`와 같은 원칙).
  - 채택된 구간이 0개면 YouTube에 올리지 않는다. "하이라이트 없음"을 알리고 원본은 `03_done`으로 보낸다(실패가 아님).
- **YouTube:** `youtube_uploader.publish_highlight(...)`를 쓴다(1-3). 썸네일, 득점 챕터 설명,
  `make_title(DEFAULT_TITLE, "{n}경기", match_date)`까지 포함한다. 공개 범위는 비밀값의 `YOUTUBE_PRIVACY`를 따른다.
  업로드에 실패하면 하이라이트 mp4를 `05_output`에 올리고 Drive 링크를 담아 실패 알림을 보낸다. 원본은 `03_done`으로 보내 다시 빌드할 수 있게 둔다.
- **상태와 알림 글:**
  - `_state/days/{date}.json`에 `{label, yt_url, start_kst, source_names}`를 추가한다.
  - `band_poster.format_post_content(그날 전체 목록, match_date)`로 **그날 전체 BAND 글**을 만든다.
  - 완료 알림 제목은 `"{n}경기 하이라이트 완료 (오늘 {m}개)"`, 본문은 BAND 글 전체다. **click은 넣지 않는다**(탭하면 복사되게, F8). 경고가 있으면 본문 끝에 덧붙인다.
  - 시작 알림은 커널이 아니라 **Apps Script가 push할 때** 보낸다(3-3). 업로드 직후 바로 "받았다"는 확인을 주기 위함이다.
  - `sh.save_results()` 형식의 판별 결과를 `_state/results/`에 올린다(v2의 "나중에 2D"와 PC 검토용).
- **정리:** 성공한 원본은 `03_done`으로 옮긴다. 커널이 시작할 때 `03_done`에서 `RETENTION_DAYS`(기본 7일)가 지난 파일을
  **휴지통**(`trashed=true`)으로 보낸다. 영구 삭제는 하지 않는다(휴지통은 Drive가 30일 뒤에 비운다).
- **종료:** `01_inbox`(GPU 실행이면 `01_inbox_2d`도)가 비어 있고 기다리는 그룹도 없으면
  `/kaggle/working/run_summary.json`에 요약을 쓰고 종료한다. 실행이 10시간을 넘으면 안전하게 종료한다(세션 한도 대비).
- **2D:** GPU 실행일 때만 `01_inbox_2d`를 가져간다(CPU 실행은 건드리지 않음).
  하이라이트 알림까지 끝낸 뒤, `run_match.py`의 2D 코드(`resolve_models`, `run_radar_segments`, `composite_pip`)를
  **복제하지 말고 import해서 재사용**한다. 결과는 `05_output`에 올리고 `"2D 완료"` 알림을 보낸다(이 알림은 click에 Drive 링크).
  2D가 실패하면 알림만 보낸다(하이라이트는 이미 전달됨).
- **인증 실패:** `invalid_grant` 등으로 인증이 만료되면 알림을 보낸다: "Google 인증 만료 — PC에서
  `python tools/make_kaggle_secrets.py` 재실행 후 hl-secrets 데이터셋 새 버전 업로드". 원본은 `01_inbox`로 되돌린다.

### 3-3. Apps Script 트리거 규칙 (`kaggle_runner/apps_script/Code.gs`)

`tick()`은 5분 간격 시간 트리거로 부른다(기본 경로). 실제로 확인하는 빈도는 요일에 따라 다르다.

- **빈도:** 한국 시간(`Utilities.formatDate(now, 'Asia/Seoul', 'u')`, 1=월 … 7=일) 기준으로 요일이 `FAST_DAYS`(기본 `7,1` = 일·월)면 매번 진행한다.
  그 외 요일에는 마지막으로 확인한 지(`LAST_SLOW_CHECK_AT`) 55분이 지났을 때만 진행하고, 아니면 Drive 호출 없이 즉시 종료한다.
- **일·월로 정한 이유:** 경기는 일요일에 있고, 업로드와 처리가 월요일로 넘어가기도 한다.
  사용자가 2026-09-26에 지정했다. 로그에도 7/20(월) 아침에 처리한 기록이 있다.
- **나머지 요일도 끄지 않는 이유:** 화~토에 올린 영상이 다음 일요일까지 방치되지 않게 하기 위함이다.
- **비용:** 5분 간격이면 하루 288회 실행되고, 파일이 없으면 1초도 안 걸린다. Apps Script 무료 한도(트리거 실행 하루 90분)의 5% 이하다.
  Kaggle은 영상이 있을 때만 실행된다.
- **(선택) 즉시 경로:** 아이폰 단축어 자동화가 Google Drive 앱을 나올 때 실행 링크(`doGet?key=…`)를 불러 `tick({force: true})`를 실행한다.
  업로드 중엔 Drive 앱을 켜 둬야 하므로(F5), 앱을 나오는 시점이 곧 업로드가 끝난 시점이다(F15).
  일·월에 5분마다 확인하면 차이가 평균 2~3분뿐이라 **기본으로는 설정하지 않는다.**

1. `01_inbox`와 `01_inbox_2d`의 영상 수를 센다. 0이면 종료한다(대부분 여기서 끝).
2. `GetKernelSessionStatus`가 실행 중(QUEUED/RUNNING/NEW_SCRIPT 등, 문자열 **포함** 비교)이면 종료한다.
   실행 중인 커널이 루프에서 새 파일을 알아서 가져간다.
3. 마지막 push 후 10분이 안 지났으면 종료한다(상태가 늦게 반영되는 경우 대비).
4. 연속 ERROR가 3회면 push를 멈추고 ntfy 경고를 한 번 보낸다. 사람이 `resetBreaker()`를 실행해야 풀린다.
5. `SaveKernel`을 호출한다.
   - `slug=<user>/<KERNEL_SLUG>`, `newTitle=KERNEL_SLUG`, `language=python`, `kernelType=script`
   - `isPrivate=true`, `enableInternet=true`, `datasetDataSources=[<user>/<SECRETS_DATASET>]`
   - `01_inbox_2d`에 파일이 있으면 `machineShape="NvidiaTeslaT4"`, 없으면 이 필드를 생략한다(CPU).
   - 응답의 `error`와 `invalid*` 필드도 확인한다(HTTP 200이어도 거절일 수 있음).
   - push에 성공하면 ntfy로 `"영상 {k}개 확인 — 처리 시작 (경기당 약 25분)"`을 보낸다.
- `text`는 raw GitHub의 `kaggle_runner/kernel_bootstrap.py`를 받아서 넣는다. 받기에 실패하면 코드에 내장한 사본을 쓴다.
- Script Properties:
  - 설정값: `KAGGLE_USERNAME`, `KAGGLE_API_TOKEN`, `KERNEL_SLUG`(hleditor-inbox), `SECRETS_DATASET`(hl-secrets), `ROOT_FOLDER_ID`, `NTFY_TOPIC`, `FAST_DAYS`(기본 `7,1`), `KICK_KEY`(무작위 32자)
  - 상태값: `LAST_PUSH_AT`, `ERROR_STREAK`, `LAST_SLOW_CHECK_AT`
- 함수: `tick()`, `doGet()`, `installTrigger()`, `resetBreaker()`, `testPush()`(수동 테스트용).
- **실행 링크(`doGet`):** 코드는 항상 넣는다. 웹 앱 배포는 사용자가 원할 때만 한다(단축어 자동화나 수동 '지금 확인' 버튼용).
  배포할 때는 실행 계정을 나, 접근을 **모든 사용자**로 둔다.
  - 접근을 '나만'으로 두면 안 되는 이유: 단축어의 'URL 콘텐츠 가져오기'는 구글 로그인 쿠키를 쓰지 못한다.
  - 대신 `?key=` 값이 `KICK_KEY`와 다르면 아무것도 하지 않고 거부 한 줄만 돌려준다.
    링크가 새도 할 수 있는 일은 '인박스 확인 → 필요하면 실행'뿐이고, `KICK_KEY`를 바꾸면 무효가 된다.
  - 요일 제한만 건너뛰고 나머지 안전장치(실행 중 확인·쿨다운·서킷브레이커)는 그대로 거친다.
    "영상 2개 발견 — Kaggle 실행 요청함" / "새 영상 없음" 같은 한 줄을 돌려준다.
  - 같은 링크를 폰 홈 화면에 두면 수동 '지금 확인' 버튼으로도 쓸 수 있다.

### 3-4. 비밀값: Kaggle 비공개 데이터셋 `hl-secrets`의 `hl_secrets.json`

```json
{
  "GEMINI_API_KEY": "...",
  "GOOGLE_CLIENT_ID": "...",
  "GOOGLE_CLIENT_SECRET": "...",
  "GOOGLE_REFRESH_TOKEN": "...",
  "ROOT_FOLDER_ID": "...",
  "NTFY_TOPIC": "hl-<무작위 24자 이상>",
  "DEFAULT_TITLE": "한울타리 FC 경기영상",
  "YOUTUBE_PRIVACY": "public"
}
```

- refresh token의 권한 범위는 `drive`, `youtube.upload`, `youtube`다. Drive 앱이 올린 파일을 읽어야 해서 `drive.file`로는 부족하다.
- 선택 키(없으면 기본값): `SENSITIVITY=normal`, `QUALITY=balanced`, `RETENTION_DAYS=7`,
  `WAIT_NEXT_PART_MIN=40`, `SETTLE_SEC=180`, `MIN_GAME_SEC=300`.
- 커널은 `/kaggle/input` 아래를 **재귀 탐색**해 `hl_secrets.json`을 찾는다. 마운트 깊이가 일정하지 않기 때문이다(`run_match.discover_match_videos` 주석 참고).
- 값은 로그나 오류 메시지에 절대 출력하지 않는다(마스킹).
- YouTube 모듈에는 이 값으로 `youtube_uploader._save_token`과 같은 형식의 토큰 파일을 임시 경로에 만들어 넘긴다(1-4).

---

## 4. 단계별 작업

### Phase 0 — 계정 준비와 검증 (구현 전, 사용자와 함께)

아래 항목은 설계 가정을 확인하는 단계다. 결과를 실행 로그에 적고, 가정이 틀리면 설계를 먼저 고친다.

- **0-1 [사용자]** Google Cloud Console(YouTube 업로드에 쓰는 기존 프로젝트)에서 **Google Drive API를 사용 설정**한다.
  OAuth 동의 화면이 **'프로덕션'**인지 확인하고 아니면 게시한다(F11).
- **0-2 [구현]** `tools/make_kaggle_secrets.py`를 작성한다.
  - 기존 `client_secrets.json`(데스크톱 클라이언트)으로 `InstalledAppFlow.run_local_server`를 띄워 3개 권한 범위에 동의를 받는다.
  - Drive에 `HLEditor/` 폴더 트리(3-1)가 없으면 만든다.
  - ntfy topic을 무작위로 생성한다.
  - `.env`에서 GEMINI 키, 기본 제목, 공개 범위를 읽어 `kaggle_secrets/hl_secrets.json`을 만든다.
  - 다음 단계 안내(Kaggle 데이터셋 업로드, Apps Script에 넣을 값, ntfy 구독 주소)를 출력한다.
  - `kaggle_secrets/`를 `.gitignore`에 추가한다.
- **0-3 [사용자]** 헬퍼 실행과 계정 설정을 한다.
  - 헬퍼를 실행하고 브라우저에서 동의한다("확인되지 않은 앱" 경고가 나오면 고급 → 계속).
  - Kaggle에서 **비공개** 데이터셋 `hl-secrets`를 만들어 `hl_secrets.json`을 올린다.
  - Kaggle Settings → API에서 토큰을 발급한다.
  - 아이폰에 ntfy 앱을 설치하고 topic을 구독한다.
- **0-4 [사용자+구현]** 아이폰에서 Drive로 올리는 과정을 실측한다.
  - 실제 경기 영상 1개를 Drive 앱 → `01_inbox` → + → 업로드 → 사진 및 동영상으로 올린다(1-1의 기본 경로).
  - 기록할 것: 소요 시간, 파일 크기가 원본과 같은지, 파일 이름 형식, ffprobe 결과(1080p·약 8Mbps인지, `creation_time`이 있는지).
  - 압축돼 있으면 사진 앱에서 '파일에 저장'한 뒤 Drive 앱에서 파일로 올리는 경로와 비교한다.
    어느 경로든 **Drive 앱 안에서 업로드가 진행**되는 쪽을 권장한다. F5 때문이기도 하고, 나중에 단축어 자동화를 켜면 "Drive 앱을 나올 때"가 업로드 종료와 맞아떨어지기 때문이다.
  - `creation_time`이 없으면 묶기 규칙을 순서 기반으로 확정한다.
- **0-5 [구현]** Kaggle과 ntfy를 검증한다.
  - PC에서 curl로 `SaveKernel`을 호출한다: 작은 테스트 스크립트, `isPrivate`, `enableInternet`, `datasetDataSources=[hl-secrets]`.
  - 확인할 것: (a) 응답에 error가 없고 커널이 생성되는지, (b) `GetKernelSessionStatus`의 `status` 문자열 형식,
    (c) 스크립트에서 `find /kaggle/input`으로 본 비밀값 마운트 경로, (d) 0-4 파일의 Drive→Kaggle 다운로드 속도(기대 2GB에 1~3분),
    (e) `df -h`로 본 임시 디스크 여유, (f) ntfy 발송 → 폰 수신 → 탭하면 복사되는지.
  - **대안(실패 시):**
    - Apps Script가 GitHub API `repository_dispatch`를 부르고, GitHub Actions가 공식 `kaggle kernels push`를 실행하게 바꾼다.
    - 최후 수단은 Kaggle 웹에서 Save & Run All을 수동으로 한 번 누르는 것이다.

**완료 기준:** 0-4와 0-5가 모두 통과해야 한다. 달라진 가정은 이 문서 2장·3장에 반영했다.

### Phase 1 — 공용 모듈 보강 (PC 앱 동작 불변)

- **1-1** `soccer_highlights.TITLE_FONT`를 환경변수 `HL_TITLE_FONT`로 바꿀 수 있게 한다.
  기본값은 Windows면 기존 경로, 그 외에는 `/usr/share/fonts/truetype/nanum/NanumGothic.ttf`다.
- **1-2** `youtube_uploader.make_title(base, video_name, match_date, index=None)`를 추가한다.
  `static/js/build.js`의 `ytTitleFor`를 이식한 것으로, 해시 같은 이름은 빼고, base에 이름이 들어 있으면 생략하고, 100자로 자른다. 테스트를 붙인다.
- **1-3** `youtube_uploader.publish_highlight(output_path, source_video, cands, approved, video_name, title, privacy, pre_sec, post_sec, on_progress=None) -> yt_url`를 추가한다.
  - `routes_auth.upload_job_to_youtube`에서 썸네일(최고 신뢰 채택 후보의 peak)·설명·업로드 부분을 Flask 없이 떼어낸 것이다.
  - `routes_auth`는 이 함수를 부르도록 바꾸되, 로그와 잡 상태 갱신은 그대로 둔다.
  - 기존 `tests/test_youtube_uploader.py`가 통과해야 하고, 업로드를 목킹한 새 테스트를 추가한다.
- **1-4** `youtube_uploader`의 토큰 파일 경로를 환경변수 `HL_YOUTUBE_TOKEN_FILE`로 바꿀 수 있게 한다(기본은 기존 경로).

**완료 기준:** `pytest` 전부 통과, `ruff check .` 통과. PC 서버를 띄워 기존 화면과 잡 복원이 정상인지 확인한다(실제 YouTube 업로드는 하지 않는다).

### Phase 2 — 인박스 러너 코어 (로컬에서 끝까지)

- **2-1** `kaggle_runner/__init__.py`를 추가해 패키지로 만든다. `run_match.py`는 여전히 노트북에 붙여넣어 단독 실행할 수 있어야 한다.
  CV 의존성 목록은 상수로 빼서 `inbox_runner`와 같이 쓴다.
- **2-2** `kaggle_runner/grouping.py`를 만든다. 순수 함수 `group_parts(files, now, settle_sec, split_min_sec, tolerance_sec, wait_timeout_sec)`는
  `(complete_groups, pending_groups)`를 돌려준다. 입력은 `FileMeta(id, name, duration, start_utc|None, arrived_utc, source_folder)` 목록이다.
  테스트할 경우:
  - 단일 경기, 2파트, 3파트
  - 도착 순서가 뒤바뀐 경우, `creation_time`이 없는 경우
  - 30:00에 딱 끝난 단일 경기(대기하다 타임아웃)
  - 서로 다른 두 경기가 섞인 경우, 정착 대기 중인 경우
- **2-3** `kaggle_runner/stores.py`를 만든다.
  - `Store` 인터페이스: `list_inbox`, `claim`, `download`, `move_done`, `move_failed`, `upload_output`, `read_json`/`write_json`(`_state`), `trash_older_than`, `recover_processing`
  - `LocalStore`: 폴더 기반. 테스트와 PC 로컬 실행에 쓴다.
  - `DriveStore`: Drive API v3. requests로 스트리밍 다운로드하고, md5를 검증하고, 일시 오류는 재시도한다.
- **2-4** `kaggle_runner/notify.py`를 만든다.
  - ntfy JSON으로 발행한다: `POST https://ntfy.sh/`에 `{topic, title, message, tags, click?}`.
  - 4096바이트를 넘으면 잘라낸다.
  - 발송에 실패해도 파이프라인은 계속한다(로그만). payload 빌더는 순수 함수로 두고 테스트한다.
- **2-5** `kaggle_runner/inbox_runner.py`를 만든다.
  - `main()` 순서: 의존성 확인 → 비밀값 로드 → store 준비 → 시작 복구·보관 정리 → 루프 → 요약 후 종료.
  - `process_game()`은 3-2 규칙을 그대로 따른다.
  - CLI 옵션: `--local-inbox DIR`(LocalStore), `--dry-run`(YouTube·ntfy 대신 콘솔 출력), `--no-vision`.
- **2-6** 날짜·번호·글 누적 로직을 넣고 테스트한다: KST 날짜 산출, 경기 번호, `_state/days` 누적, BAND 글 생성, 화질 경고 판정, 비밀값 마스킹.

**완료 기준:**
- `pytest`와 `ruff`가 통과한다.
- **로컬 E2E**를 한다.
  - `output/_merged/ea2f5577.mp4`(실제 41분 경기)를 ffmpeg `-c copy`로 30분과 11분 두 파트로 자른다.
  - 두 파트를 LocalStore inbox에 넣고 `--dry-run`으로 실행한다.
  - 확인할 것: 두 파트가 한 경기로 병합되는지, 하이라이트 mp4가 생성되는지(워터마크 포함), BAND 글이 출력되는지.
- Gemini를 실제로 호출하면 약 $0.14가 든다. 먼저 사용자 승인을 받거나 `--no-vision`으로 흐름만 확인한다.

### Phase 3 — Kaggle 연결 (수동 트리거로 검증)

- **3-1** `kaggle_runner/kernel_bootstrap.py`를 만든다(짧게 유지).
  - `/tmp/HLEditor`에 `git clone --depth 1`하고 `sys.path`에 추가한 뒤 `inbox_runner.main()`을 부른다.
  - 의존성 설치는 `inbox_runner`의 첫 단계에서 한다: google-genai, google-api-python-client, google-auth. CV 스택은 GPU(2D) 실행일 때만 설치한다.
  - 주의: 커널은 매번 **main 최신 코드**를 받는다. pytest를 통과하지 않은 코드를 push하지 않는다.
- **3-2** 커널 환경을 준비한다.
  - 나눔고딕 TTF를 google/fonts 저장소(OFL)에서 받아 `HL_TITLE_FONT`로 지정한다.
  - 임시 디렉터리는 `/kaggle/working` 밖으로 정한다.
  - ffmpeg가 있는지 확인한다.
- **3-3 [사용자]** 짧은 테스트 클립으로 검증한다.
  - 원본에서 잘라낸 5~6분 클립을 폰으로 `01_inbox`에 올린다. 테스트 동안은 `MIN_GAME_SEC=60`으로 둔다.
  - 0-5의 curl로 push한다(또는 Kaggle 웹에 bootstrap을 붙여넣고 Save & Run All).
  - 알림이 오는지 확인한다. 테스트 동안 `YOUTUBE_PRIVACY`는 `unlisted`로 둔다.
- **3-4** 실제 경기 1개로 E2E를 돌리고 단계별 소요 시간을 기록한다: 업로드, 대기열, 설치, 다운로드, 팬∥판별, 빌드, 업로드.

**완료 기준:**
- 폰으로 완료 알림이 오고, 탭하면 BAND 글이 복사된다.
- YouTube 영상에 제목·챕터·썸네일·워터마크가 들어가 있다.
- 원본은 `03_done`으로 옮겨지고 `_state`가 갱신된다.
- 커널 Output(`/kaggle/working`)에 큰 파일이 없다.

### Phase 4 — 자동 트리거 (Apps Script + 아이폰 단축어 자동화)

- **4-1** `kaggle_runner/apps_script/Code.gs`를 작성한다(3-3 규칙, `doGet` 실행 링크 포함). `UrlFetchApp`, `DriveApp`, `PropertiesService`만 쓴다.
- **4-2 [사용자]** Apps Script를 설정한다.
  - script.google.com에서 새 프로젝트를 만들고 Code.gs를 붙여넣는다.
  - Script Properties를 입력하고 `installTrigger()`를 한 번 실행해 권한을 승인한다.
  - (선택) 웹 앱으로 배포하고(접근: 모든 사용자) 실행 링크를 받는다.
  - (선택) 아이폰 단축어 앱 → 자동화 → 새 자동화 → **앱** → Google Drive, **'닫힘'** 선택 → **즉시 실행** → 동작 'URL 콘텐츠 가져오기'에 실행 링크를 넣는다.
    '실행 시 알림'은 끈다.
- **4-3** 아이폰으로 E2E를 확인한다.
  - (a) 일·월요일에 업로드만 하면 5분 안에 "처리 시작" 알림이 오는지, 이후 완료 알림까지 아무것도 누르지 않아도 되는지 확인한다.
  - (b) 처리 도중 다른 경기를 올리면 같은 실행이 이어서 처리하는지 확인한다.
  - (c) 일부러 실패를 만든다(짧은 영상 또는 깨진 파일) → `04_failed`로 가고 실패 알림이 오는지 확인한다.
  - (d) 서킷브레이커가 동작하는지 확인한다(`KERNEL_SLUG`를 일부러 틀리게 해서 연속 ERROR를 만든다).
  - (e) 요일 제한을 확인한다. `FAST_DAYS`에서 오늘을 빼면 타이머가 1시간에 한 번만 확인하는지 본다.
  - (f) `doGet`을 테스트 배포해서 확인한다. 맞는 `key`면 요일 제한 없이 바로 실행되고, 틀린 `key`면 아무 일도 일어나지 않아야 한다.
    사용자가 배포를 원하지 않으면 테스트 후 배포를 보관 처리한다.
  - (g) (단축어 자동화를 설정한 경우) Drive 앱을 나올 때 몇 초 안에 시작되는지 확인한다.

**완료 기준:** (a)~(f) 통과. (g)는 자동화를 설정한 경우에만 확인한다. 경기 종료부터 알림까지의 시간을 실행 로그에 기록한다.

### Phase 5 — 2D 선택 실행과 마무리

- **5-1** `01_inbox_2d` → GPU 실행 → 2D 결과를 Drive에 올리고 알림한다(3-2의 2D 규칙).
- **5-2** `run_match.py`에 `ENABLE_DOTPLAY = False` 기본 토글과 팬 보정을 추가한다. 수동 배치로 돌려도 PC와 같은 결과가 나오게 한다.
- **5-3** 보관 정리가 동작하는지 확인한다: `03_done`의 7일 지난 파일이 휴지통으로 가는지.
- **5-4** 문서를 정리한다.
  - `kaggle_runner/INBOX_SETUP.md`: 6장을 바탕으로 한 사용자 1회 설정 가이드.
  - `README.md`: "아이폰 자동 처리" 섹션 추가.
  - `PROJECT.md`: 아키텍처, 결정, 실측치 추가.

---

## 5. 위험과 대안

| 위험 | 가능성 | 대응 |
|---|---|---|
| Kaggle API 형식이나 인증이 바뀌어 Apps Script push가 실패 | 중 | Phase 0-5에서 curl로 먼저 검증한다. 대안은 GitHub Actions + 공식 CLI, 최후엔 Kaggle 웹 수동 실행 |
| 아이폰에서 원본이 아닌 압축본이 올라감 | 낮음~중 | 0-4에서 확인한다. 커널이 화질 경고를 보낸다 |
| 분할 파트가 늦게 도착해 따로 처리됨 | 낮음 | 정착 대기 + 대기 타임아웃 + 알림에 병합 결과 표시. "나뉜 파일은 한 번에 같이 선택" 안내 |
| Drive 저장공간 부족 (무료 15GB는 한 경기일 약 15GB도 빠듯) | 중 | 7일 보관 후 휴지통. 필요하면 Google One 100GB |
| refresh token 만료(테스트 모드 7일) 또는 폐기 | 중 | 프로덕션 게시. 인증 실패 시 알림으로 재발급 절차를 안내 |
| Kaggle 대기열 지연이나 세션 한도 | 낮음 | 시작 알림으로 상황을 알린다. 10시간이면 안전 종료 |
| 비밀값(Drive 전체 권한 토큰) 유출 | 낮음 | 비공개 데이터셋과 비공개 커널, 로그 마스킹. Google 계정 보안 설정에서 즉시 권한 취소 가능 |
| 검토 없이 공개 게시됨 | 낮음 | `MIN_GAME_SEC` 가드. `YOUTUBE_PRIVACY`를 unlisted로 할지 사용자가 선택(7장) |
| ntfy 공개 서버 의존 | 낮음 | 추측할 수 없는 topic. 대안은 Telegram 봇(`notify.py`에 교체 지점을 둔다) |
| 일·월 외의 요일(공휴일 등)에 경기가 있음 | 낮음 | 1시간 안에는 처리된다. 급하면 `FAST_DAYS`에 그날을 잠시 추가하거나, 실행 링크를 배포해 수동으로 부른다 |
| (배포한 경우) 실행 링크 유출 | 낮음 | `key` 검사. 할 수 있는 일은 인박스 확인뿐이다. `KICK_KEY`를 바꾸면 무효가 된다 |

---

## 6. 사용자 1회 설정 체크리스트 (INBOX_SETUP.md 초안)

1. **Google Cloud Console:** Drive API를 사용 설정하고 OAuth 동의 화면을 '프로덕션'으로 둔다.
2. **PC:** `python tools/make_kaggle_secrets.py`를 실행하고 브라우저에서 동의한다. `hl_secrets.json`이 생기고 Drive 폴더가 자동으로 만들어진다.
3. **Kaggle:** 비공개 데이터셋 `hl-secrets`에 `hl_secrets.json`을 올리고, Settings → API에서 토큰을 발급한다.
4. **Apps Script:** 새 프로젝트에 Code.gs를 붙여넣고, Script Properties를 입력한 뒤 `installTrigger()`를 실행한다.
   (선택) 웹 앱으로 배포해(접근: 모든 사용자) 실행 링크를 받는다.
5. **아이폰:**
   - (선택) 단축어 앱 → 자동화 → 앱 'Google Drive'가 **닫힐 때** → **즉시 실행** → 'URL 콘텐츠 가져오기'(실행 링크)를 만든다.
     '실행 시 알림'은 끈다.
   - ntfy 앱을 설치하고 topic을 구독한다.
   - Drive 앱 설정에서 모바일 데이터 전송을 허용한다.
   - `01_inbox` 폴더에 별표를 해 둔다.
   - 저전력 모드와 저데이터 모드는 업로드 중에 끈다.
6. **테스트:** 짧은 클립을 올려서 알림이 오는지 확인한다.

---

## 7. 열린 질문 (사용자 확인 필요)

1. **자동 게시 공개 범위:** 지금 PC 설정(`YOUTUBE_PRIVACY`)을 그대로 쓸지, 검토 없이 올라가니 일부공개(unlisted)로 할지.
2. **Google 저장공간 여유:** Google One을 쓰는지. 보관 기간(`RETENTION_DAYS`)을 정하는 데 필요하다.
3. **경기 이름:** `"{n}경기"` 자동 번호가 괜찮은지. 지금은 파일명을 직접 바꿔서 쓰고 있다.
