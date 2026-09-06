# 축구 하이라이트 자동 추출 프로젝트

동호회 축구 경기 영상에서 **음성 볼륨 급증 + Gemini 비전 판별**로 하이라이트 구간을 자동 추출해 하나의 영상으로 만드는 도구.

이 문서는 Claude Code가 프로젝트를 이어받아 작업할 수 있도록 설계 의도·검증 결과·주의사항을 정리한 핸드오프 문서다.

---

## 핵심 아이디어

영상을 사람이 직접 다 보지 않고 하이라이트를 찾기 위해, **오디오 볼륨**을 1차 신호로 쓴다. 골·슈팅·결정적 장면에서는 환호·외침으로 볼륨이 순간적으로 튄다. 다만 이것만으로는 "무슨 일이 일어났는지" 모르므로, 급증 구간의 프레임을 **Gemini 비전 모델**에 보내 하이라이트 여부를 2차 판별한다.

```
오디오 추출 → RMS 볼륨 + rolling baseline → 급증(spike) 후보 검출
            → 카메라 팬 궤적 분석(로컬) → 후보별 팬 상태 주석·신뢰도 보정치
            → 후보 구간 프레임 추출 → Gemini 비전 판별(유형/신뢰도)
            → 보정 신뢰도(confidence + pan_bonus) 임계 이상만 채택 → ffmpeg로 클립 분할·병합
```

이 2단계 구조(싼 오디오 필터 → 비싼 비전 판별)는 비용·정확도 균형을 위한 의도적 설계다. 오디오가 "언제"를 좁혀주고, 비전이 "무엇"을 판단한다.

여기에 2026-07 **3차 신호(팬 궤적, `pan_signal.py`)**가 추가됐다. XbotGo는 액션을 따라 좌우 패닝하므로 카메라 움직임 자체가 경기 정보다: "골대 진영에서 정지"(골문 앞 상황)·"빠른 스위프"(역습)에 해당하는 후보의 신뢰도를 +0.10 보정한다. **승격 전용**(감점 없음)으로 설계했다 — 실측에서 진짜 세트피스와 종료 함성의 팬 상태가 구분되지 않는 사례가 있었고, 기각은 프레임을 직접 보는 Gemini의 몫이다. 로컬 계산이라 무료이며, 팬 이동폭·추정 신뢰도가 낮으면(고정 카메라, 짧은 클립) 자동으로 비활성화된다.

---

## 파일

이 프로젝트는 더 이상 단일 스크립트가 아니라 웹 UI 기반 배치 도구로 확장되었다. 2026-07 리팩터(Phase 1~4, [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) 참고)를 거치며 역할별 모듈로 세분화됐다.

| 파일 | 설명 |
|------|------|
| `soccer_highlights.py` | 오디오 분석 · Gemini 비전 판별 · 탈중복 · 영상 생성 파이프라인 (CLI로도 단독 실행 가능) |
| `pan_signal.py` | 카메라 팬 궤적 3차 신호 — 저해상 프레임 phase correlation → 팬 속도/위치 → 후보 상태 판정(`pan`)·신뢰도 보정(`pan_bonus`). 조절 파라미터는 파일 상단 `PAN_*` 블록 |
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
| `dotplay/`, `jobs_dotplay.py`, `routes_dotplay.py`, `static/js/dotplay.js` | Dot Play(FM 스타일 2D 버드뷰 변환) 기능 일체 — 자세한 내용은 아래 [Dot Play](#dot-play-fm-스타일-2d-버드뷰-변환--2026-0719-20-통합) 섹션 |

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

### 5. 파트 병합 — XbotGo 30분 자동 분할 대응 (2026-09-06)
XbotGo는 촬영이 30분을 넘으면 파일을 자동으로 잘라 저장한다. 한 경기가 `1부.mp4`·`2부.mp4`
로 나뉘면 예전엔 잡이 2개 생겨 하이라이트·YouTube 업로드도 2개가 됐다.

**해결**: 큐 관리 탭에서 파트 파일들을 체크해 **[선택한 파일 한 경기로 합치기]** 를 누르면
하나의 잡으로 묶인다. 처리 시작 시 워커가 먼저 `ffmpeg -f concat -c copy`(파라미터가 다르면
재인코딩 폴백)로 파트를 이어붙인 뒤, 그 다음 단계(검출·비전·빌드·업로드)는 기존과 동일하게
**단일 파일**로 진행한다 → 하이라이트/링크 1개.

- 프런트: `staged` + `mergeGroups`(`state.js`) → `stagedToJobItems()`가 `{videos:[p1,p2,…]}`
  항목으로 변환해 `/api/jobs/add`에 보낸다. 단일 파일은 기존대로 `{video:"…"}`.
- 백엔드: `jobs.new_job(source_videos=[…])` → `needs_merge=True`, `video`는 `output/_merged/{jid}.mp4`.
  `_process` 첫 단계에서 병합(`status="merging"`) 후 `needs_merge=False`. 재시도/복원은 병합
  완료 여부를 이 플래그로 판단해 다시 안 합친다. 잡 삭제 시 `output/_merged/{jid}.mp4`도 함께 정리.
- `soccer_highlights.concat_videos(parts, out_path, workdir=None)` — concat 헬퍼.
- 표시 이름(`video_name`)은 첫 파트 이름 그대로 두고, 합본임은 큐 UI가 `n_parts` 배지로 보여준다.
- **파트 순서 자동 정렬**(2026-09-06): 사용자가 파일 선택창에서 아무 순서로 골라도
  `jobs.order_parts()`가 **파일 수정시각(mtime) 오름차순**으로 재정렬한다. XbotGo는
  30분을 넘기면 파트를 순차 저장하므로 mtime이 곧 촬영 순서이고, 자투리(짧은) 파트는
  항상 뒤로 간다. mtime이 같으면 파일명 자연 정렬로 보조. 프런트는 미리보기로 파일명
  순 표시(`_orderedMembers`), 최종 순서는 서버가 mtime으로 확정한다. `_process`는 각
  파트 길이를 재 앞 파트가 뒤 파트보다 짧으면(순서 의심) 경고 로그를 남긴다.
- **한계**: 병합본 용량 ≈ 파트 합(디스크 필요). XbotGo 파일 롤오버 시 경계에서 1~2초가
  유실될 수 있어 정확히 30:00에 걸친 장면은 미세하게 잘릴 수 있다(원본에 없는 프레임).

---

## 원스톱 파이프라인 계약 (2026-08-01 수정)

원스톱(`static/js/pipeline.js`)은 브라우저가 2초 폴링으로 서버 상태를 보며 단계를
넘기는 구조다. 각 단계는 **진입 액션(`_enterStep`)** 과 **완료 판정(`advancePipeline`)** 으로
분리돼 있다.

```
processing → building → uploading → (posting | band_copy) → done
```

### 지켜야 할 규칙

1. **"이미 처리됨"은 오류가 아니다.** 파이프라인이 호출하는 서버 API
   (`approve-all` / `build-all` / `upload-all-youtube` / `post-band`)는 대상이 0건이어도
   **200**을 반환해야 한다. 프론트의 `post()`는 non-2xx에 예외를 던지고, 그 예외가
   그대로 단계 중단으로 이어진다.
   - 실제 사고: `build-all`의 `auto_upload`가 빌드 직후 업로드를 시작해 놓은 상태에서
     파이프라인이 안전망으로 `upload-all-youtube`를 한 번 더 호출하면 대상이 0건이 되어
     400이 났고, 원스톱이 업로드 단계에서 매번 죽었다. (업로드 자체는 백그라운드에서
     성공하지만 BAND 글 준비·완료 알림이 실행되지 않음)
2. **진입 액션은 멱등해야 한다.** 재개(`resumePipeline`)는 같은 단계의 진입 액션을 다시
   부르는 것뿐이다. 그래서 서버는 이미 끝난 작업을 건너뛴다 —
   `build-all`은 `skip_built`, `upload-all-youtube`는 `yt_status in (uploading, done)` 제외.
3. **파이프라인 잡만 건드린다.** 위 API는 모두 `job_ids`를 받는다. 없으면 전체 대상이
   되어 큐에 남아 있던 예전 잡까지 승인·빌드·업로드된다(의도치 않은 채널 업로드).
4. **실패해도 상태를 지우지 않는다.** 실패는 `_pipeFail()`로 `status="failed"`를 남기고
   진행 바에 [이어서 진행] / [실패 건너뛰고 다음 단계]를 띄운다. 처음부터 다시 하지 않는다.
   빌드 단계 실패는 `/api/jobs/<id>/retry {"stage":"build"}`로 **분석 결과를 유지한 채**
   인코딩만 재시도한다(몇 분짜리 Gemini 재판별을 다시 하지 않기 위함).

### YouTube 토큰 취급 (중요)

`youtube_uploader.py`는 **네트워크 오류로 토큰을 지우지 않는다.** `_is_hard_auth_error()`가
`invalid_grant` 계열과 401만 "진짜 인증 실패"로 보고, DNS 실패·타임아웃·구글 5xx·403(할당량)은
토큰을 보존한다. 예전에는 `is_authenticated()`가 모든 예외를 인증 실패로 처리해 `revoke()`를
불렀고, 이 함수는 페이지 로드마다 호출되므로 순간적인 네트워크 장애 한 번에 refresh token이
지워져 계속 재인증해야 했다.

토큰 파일에는 **`expiry`를 반드시 저장한다.** 저장하지 않으면 `creds.expired`가 항상 False가
되어 만료된 액세스 토큰으로 업로드를 시작하고, 매 요청이 401 → 재갱신 경로를 타게 된다
(로그의 `Refreshing credentials due to a 401 response`가 그 증상이다).

> OAuth 동의화면이 **"테스트" 상태면 refresh token이 7일 후 강제 만료**된다. 주기적으로
> 재인증이 필요하다면 Google Cloud Console에서 앱을 "프로덕션"으로 게시할 것.

---

## Dot Play (FM 스타일 2D 버드뷰 변환) — 2026-07-19/20 통합

Xbotgo(iPhone 16 Pro) 촬영 영상에서 선수·공 위치를 검출해 Football Manager
스타일 2D 버드뷰 dot-play 영상으로 변환하는 기능. 별도 PoC 저장소
(`C:\Users\SKTelecom\skt\fm-dotplay`)에서 먼저 검증한 코어를 이 프로젝트로
이식했다 — 같은 XbotGo 카메라 도메인이고, job 큐/워커 패턴을 그대로 재사용할
수 있어서다. 원래 fm-dotplay를 독립 프로젝트로 시작했다가 도중에 "HLEditor
안에 구현했어야 했다"는 판단으로 이쪽으로 옮겼다.

### 파이프라인
```
영상 → ① 검출(YOLO, roboflow 호스팅) → ② 추적(ByteTrack)
     → ③ 팀분류(SigLIP+KMeans) → ④ 피치 캘리브레이션(키포인트→호모그래피, 팬 대응 옵티컬플로우 전파)
     → ⑤ 좌표 스무딩(속도 이상치 제거+보간+Savitzky-Golay) → ⑥ dot-play 렌더
```

### 파일
| 파일 | 설명 |
|---|---|
| `dotplay/` | 코어 서브패키지 — `detect`/`track`/`teams`/`homography`/`smoothing`/`render`/`pitch`/`pipeline`/`device`/`roboflow_client`/`selftest`/`config` |
| `jobs_dotplay.py` | **하이라이트 파이프라인(`jobs.py`)과 완전 분리된** 별도 상태저장소·큐·워커 |
| `routes_dotplay.py` | API Blueprint (`/api/dotplay/*`) |
| `static/js/dotplay.js` | Dot Play 탭 UI — 자체 폴링 루프, 하이라이트 쪽 `allJobs`/`poll()`과 무관 |
| `dotplay_output/` | 산출물(`{jid}.mp4`, `{jid}.parquet`) — gitignore됨 |

### 아키텍처 결정 — 왜 워커를 분리했나
`jobs.py`의 백그라운드 워커는 큐 하나를 **순차 처리**하는 단일 스레드다.
dot-play는 CPU 전용 추론이라 5분 영상 기준 수십 분이 걸릴 수 있는데, 같은
큐에 얹으면 그동안 하이라이트 추출 잡이 전부 대기하게 된다. 그래서
`jobs_dotplay.py`가 완전히 독립된 `DOTPLAY_QUEUE`/`DOTPLAY_JOBS`/
`hl-dotplay-worker` 스레드를 쓴다. `app.py`가 두 워커를 모두 기동한다
(`jobs.start_worker()` + `jobs_dotplay.start_worker()`).

### 무거운 의존성 처리 — 지연 import
`dotplay/*`의 torch·ultralytics·transformers 등은 **잡이 실제로 실행될 때만**
import된다(`jobs_dotplay._process()` 내부). `app.py`/`routes_dotplay.py`는
가벼운 모듈만 최상위에서 import하므로, 이 무거운 패키지들이 없어도 앱 자체는
정상 기동한다 — 실제로 검증: CV 스택 설치 전 상태에서 서버를 띄워
`/api/dotplay/status`, `/api/dotplay/jobs`가 정상 응답하는 것을 확인했다.

### 이 환경(Python 3.14) 특유의 문제 두 가지 — 재발 방지
1. **roboflow의 `inference` 패키지가 Python 3.14 휠을 지원하지 않음**(모든
   버전이 3.11~3.13 상한). → `dotplay/roboflow_client.py`가 `requests`로
   REST API(`https://detect.roboflow.com/{model_id}`)를 직접 호출하도록
   대체. `supervision`의 `Detections.from_inference()` /
   `KeyPoints.from_inference()`는 원래 이 API가 반환하는 순수 JSON dict를
   그대로 받아들이도록 설계돼 있어 `inference` 패키지 유무와 무관하게
   동일하게 동작한다 — 기능 손실 없음.
2. **pandas 3.0의 Copy-on-Write** 때문에 `.to_numpy()`가 읽기전용 배열을
   반환할 수 있다. `dotplay/smoothing.py`의 `_smooth_track()`에서
   `reindex()` 이후 `.to_numpy(dtype=float)`에 반드시 `.copy()`를 붙여야
   in-place 대입(`x[outliers] = np.nan`)이 에러 없이 동작한다. 새로 numpy
   배열을 pandas에서 뽑아 mutate하는 코드를 추가할 때 이 패턴을 기본으로
   가정할 것.

### 설정
- `.env`의 `ROBOFLOW_API_KEY` 필요 (무료, app.roboflow.com 발급) — 없으면
  Dot Play 탭에 경고 배너가 뜨고, 잡 실행 시 명확한 오류 메시지로 실패한다
  (크래시 아님).
- `DOTPLAY_PLAYER_MODEL_ID` / `DOTPLAY_FIELD_MODEL_ID` — 기본값은 roboflow
  공개 Universe 모델(`football-players-detection-3zvbc/11`,
  `football-field-detection-f07vi/14`). 아마추어 구장 라인 인식이 나쁘면
  이 모델들을 자체 라벨링으로 파인튜닝해 교체하는 것을 고려.

### 검증 상태
- ✅ UI 탭 렌더링, API 엔드포인트, 서버 재시작 시 기존 하이라이트 잡 무손실
  복원 — 실제 브라우저로 확인 완료.
- ✅ self-test(`dotplay.selftest.run_selftest()`, 모델·API 키 불필요) —
  합성 좌표로 스무딩→렌더 파이프라인이 HLEditor의 Python 3.14 환경에서
  end-to-end 정상 동작하는 것을 확인(피치 라인·팀 컬러 점·잔상 정상 렌더링).
- ✅ **실제 클립 변환 검증 완료 (2026-07-20)** — 0719_6경기 원본에서 자른
  60초 클립(1080p30, stride=2, 900프레임)으로 end-to-end 성공. 결과:
  - **소요 시간 83분** (분석 68분 + 팀분류 13분 + 스무딩·렌더링 ~2분).
    병목은 CPU가 아니라 **Roboflow API 왕복(프레임당 2회 순차 호출 ≈ 4초)**.
    5분 영상이면 stride=2 기준 약 7시간 — 실사용엔 속도 개선 필수(아래).
  - **1차 시도는 25분 지점에서 ReadTimeout 1회로 전체 실패** →
    `roboflow_client.py`에 지수 백오프 재시도(타임아웃/연결오류/429/5xx,
    최대 5회) 추가 후 재실행에서 완주. 이 재시도는 필수다.
  - **품질(60초 실측)**: 좌표의 77%만 피치 안(+5% 여유), 프레임의 26%에서
    피치 밖 점 5개 이상(호모그래피 불안정 구간), 극단 이상치(피치 2배 초과,
    km 단위 폭주)도 165행 존재. 피치 안 기준 프레임당 중앙값 9명만 검출
    (실제 가시 인원 16~22명) — 먼 사이드 소형 선수 미검출 다수.
    트랙 파편화 심함(397트랙, 중앙값 15프레임=1초). 팀 분류는 동작하나
    분포 불균형(5215:3201). 골키퍼 클래스는 한 번도 안 잡힘(전부 player/
    referee).
  - 개선 후보(우선순위순): ① 피치 밖 좌표 클램프/드롭(스무딩 전) —
    호모그래피 폭주 프레임 무력화, ② 선수/피치 검출 2회 호출 병렬화(시간
    절반), ③ 피치 키포인트 검출을 매 프레임 대신 N프레임마다(옵티컬플로우
    전파가 이미 있음), ④ 아마추어 구장용 모델 파인튜닝(검출률 근본 개선).

### 하이라이트 PiP 합성 (2026-07-20 추가)
완료된 하이라이트 잡을 골라, 하이라이트 하단 중앙에 dot-play 버드뷰를 작게
얹은 합성 영상을 만드는 기능 (`mode="pip"` 잡).

- **편집본을 직접 분석하지 않는다** — 장면 전환마다 추적·옵티컬플로우가
  깨지므로, `routes_dotplay.api_dotplay_add_pip()`가 하이라이트 빌드와 동일한
  `sh.get_merged_timeline()`으로 원본 영상의 구간 타임라인을 계산해 잡에
  스냅샷으로 저장하고, `dotplay/pipeline.py::run_radar_segments()`가 원본의
  그 구간들만 분석한다(구간마다 추적기·호모그래피 리셋, track_id는 구간별
  1,000,000 단위 네임스페이스로 분리).
- **팀 색 일관성**: 팀 분류(SigLIP+KMeans)는 전체 구간의 크롭으로 1회만
  학습한다. 구간별로 따로 학습하면 구간마다 팀 A/B가 뒤바뀔 수 있다.
- **싱크**: 좌표 frame을 편집본 타임라인으로 재배치하고, `render_video()`의
  `frame_range`로 전체 길이를 강제 렌더링해 편집본과 길이를 맞춘다. 합성은
  ffmpeg overlay(`jobs_dotplay._composite_pip()`, 폭 28%·하단 중앙·불투명도
  0.9, `eof_action=repeat`). '최고 속도(무재인코딩)'로 빌드된 하이라이트는
  컷이 키프레임에 스냅돼 어긋날 수 있어, 추가 시점에 ffprobe로 실제 길이와
  1초 이상 차이나면 잡에 경고(note)를 붙인다.
- 산출물: `{jid}.mp4`(합성본), `{jid}_radar.mp4`(레이더 단독), `{jid}.parquet`.
- ❌ 실제 영상으로는 미검증(ROBOFLOW_API_KEY 없음 — 위와 동일 블로커).
  합성 단계(`_composite_pip`)는 합성 영상으로 단독 검증 완료, 오류 경로는
  실제 하이라이트 잡으로 E2E 확인 완료.

### 참고
전체 설계 배경·PoC 로드맵은 별도 저장소
`C:\Users\SKTelecom\skt\fm-dotplay\PLAN.md`에 남아 있다(이 저장소로 이식되기
전 원본 설계 문서). 코드는 이제 이 프로젝트가 정본이며, fm-dotplay는 참고용
으로만 남겨둔다.

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
13. ~~카메라 팬 궤적 3차 신호~~ ✅ 구현됨 (2026-07-11) — `pan_signal.py`. 팬 상태가 하이라이트를 지지하면(골문 앞 정지/빠른 전개) 신뢰도 +0.10 승격 전용 보정. CLI `--no-pan`으로 끔, 웹은 항상 시도(실패 격리). 리뷰 UI 신뢰도 셀에 ▲배지+툴팁. 검증: 5분 40초 실경기 영상에서 골문 앞/역습 장면 6개에 보정, 경기 종료 함성(최대 볼륨)은 미보정 — 프레임 육안 대조 완료.

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
- **임계 비교는 반드시 `sh.effective_conf(c)`(= confidence + pan_bonus)로 한다** — `select_segments`, `jobs.flag()`, 웹 `review.js recomputeFlags()` 세 곳이 같은 규칙을 쓴다. 새 선택/필터 코드를 추가할 때 raw `confidence`를 직접 비교하면 팬 보정이 누락된다. 팬 필드(`pan`, `pan_bonus`)는 후보 dict에 실려 `save_results()`/`--from-json`/재시작 복원을 그대로 통과한다.
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
- **Dot Play 기능**: `ROBOFLOW_API_KEY` 환경변수 (실제 변환 실행 시). 없으면
  Dot Play 탭에 경고 배너, 잡은 명확한 오류로 실패(다른 기능엔 영향 없음).
  torch·ultralytics·transformers 등 무거운 CV 스택이 `requirements.txt`에
  포함돼 있음(설치 시간 김) — 자세한 내용과 이 PC 특유의 우회 사항(위
  Python 3.14 관련 주석 두 가지)은 위 [Dot Play](#dot-play-fm-스타일-2d-버드뷰-변환--2026-0719-20-통합) 섹션 참고

## 테스트·lint
```bash
pip install -r requirements-dev.txt
pytest              # tests/ 전체 실행
ruff check .        # lint (--fix로 안전한 항목 자동 수정)
```
`tests/`는 ffmpeg/Gemini 실호출 없이 동작하도록 설계됐다(오디오 검출은 합성 신호로, Flask 라우트는 `jobs.JOBS`에 상태를 직접 주입하고 `build_output`을 몽키패치로 목 처리). 새 기능을 추가하면 여기에 대응하는 테스트를 먼저 확인/추가하는 것을 권장.

## 도메인 메모
- 카메라: XbotGo AI 카메라 — 공/액션을 따라 **좌우로 계속 패닝**한다(골대가 화면에 있다 없다 함). 화면 좌표 기반 신호(고정 ROI)는 설계 불가. 선수·공이 작게 보이는 점이 비전 판별 난이도에 영향
- 대상: 동호회/아마추어 경기. 해설자 없음, 관중 적음 → TV 중계보다 오디오 신호 약함 → 절대 임계보다 baseline 대비 상대 급증이 적합
