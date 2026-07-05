# HLEditor — 축구 하이라이트 자동 추출기

동호회·아마추어 축구 경기 영상에서 **음성 볼륨 급증 + Gemini 비전 판별**로 하이라이트 구간을 자동으로 찾아 하나의 영상으로 만들고, **YouTube 업로드 → BAND 게시**까지 한 화면에서 처리하는 도구입니다.

하루 최대 **8개 30분 영상을 배치로** 처리합니다.

---

## 동작 구조

```
[영상 추가]
   │
   ├─ 수동 모드 ──▶ 오디오 분석 → Gemini 비전 판별 → 리뷰 → 영상 생성 → YouTube 업로드 → BAND 게시
   │
   └─ 원스톱 ────▶ 오디오 분석 → Gemini 비전 판별 → 자동 승인 → 영상 생성 → YouTube 업로드 [→ BAND 게시]
                                                                          (버튼 한 번으로 전체 자동)
```

- **1차 (오디오)**: 골·환호 등 볼륨 급증 시점을 rolling baseline 대비 상대 상승폭으로 검출. 빠르고 무료.
- **2차 (비전)**: 후보 구간 프레임을 Gemini에 전송해 유형·신뢰도 판별. 정확하지만 유료.
- **탈중복**: 실제 잘라낼 구간이 2초 이상 겹치면 볼륨 변화가 더 큰 구간만 유지.
- **리뷰**: 신뢰도 슬라이더로 채택 기준을 조정하고 구간별 AI 설명을 확인.
- **생성·배포**: 선택 구간을 ffmpeg로 이어붙여 하이라이트 영상 생성 → YouTube 업로드(챕터·썸네일 자동) → BAND 게시.

---

## 파일 구성

| 파일 | 설명 |
|------|------|
| `app.py` | Flask 앱 조립·시작 (진입점). 라우트/잡 처리 로직은 아래 모듈로 분리됨 |
| `jobs.py` | 배치 잡 상태 저장소·백그라운드 워커·오디오/비전 처리 파이프라인 |
| `routes_jobs.py` | 큐 관리·리뷰·영상 생성 API (Flask Blueprint) |
| `routes_auth.py` | YouTube OAuth 인증·업로드 API (Flask Blueprint) |
| `routes_band.py` | BAND OAuth 인증·게시 API (Flask Blueprint) |
| `tray.py` | Windows 시스템 트레이 아이콘 |
| `config.py` | `.env` 파싱·ffmpeg 확인 등 공용 설정 헬퍼 |
| `soccer_highlights.py` | 오디오 분석 · Gemini 비전 판별 · 탈중복 · 영상 생성 파이프라인 (CLI로도 단독 실행 가능) |
| `youtube_uploader.py` | YouTube OAuth 2.0 · 영상 업로드 · 썸네일 추출 · 챕터 설명 생성 |
| `band_poster.py` | BAND OAuth 2.0 · 날짜별 게시글 작성 |
| `templates/index.html` | 웹 UI 뼈대 (큐 관리 / 리뷰 / 영상 생성 3탭) |
| `static/css/app.css` | 웹 UI 스타일 |
| `static/js/*.js` | 웹 UI 로직 (state·utils·queue·review·build·pipeline·notify·main) |
| `tests/` | pytest 유닛/통합 테스트 |
| `pyproject.toml` | ruff(lint) · pytest 설정 |
| `.env` | API 키·OAuth 설정 (git 제외, `.env.example` 참고) |
| `client_secrets.json` | Google OAuth 클라이언트 설정 (git 제외) |
| `PRIVACY.md` | BAND API 심사용 개인정보처리방침 |
| `PROJECT.md` | 설계 의도·핸드오프 문서 (Claude Code 작업 인수인계용) |

실행 시 생성되는 디렉터리:

| 디렉터리 | 내용 |
|---------|------|
| `output/` | 생성된 하이라이트 영상 (mp4) |
| `results/` | 잡별 비전 판별 결과 JSON (`results_{잡ID}.json`) — 24시간 이내 것은 서버 재시작 시 리뷰 큐로 자동 복원됨 |

---

## 설치

### 1) 의존성

```bash
pip install -r requirements.txt
```

개발(테스트·lint)까지 하려면:

```bash
pip install -r requirements-dev.txt
```

`ffmpeg` / `ffprobe` 가 PATH에 있어야 합니다.

- **Windows**: `winget install Gyan.FFmpeg` 또는 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)에서 `ffmpeg-release-essentials.zip` 다운로드 후 PATH 등록
- **macOS**: `brew install ffmpeg`
- **Ubuntu**: `sudo apt install ffmpeg`

> ffmpeg가 없으면 앱 시작 시 터미널에 설치 안내가 출력되고, UI 상단에 경고 배너가 표시됩니다.

### 2) 환경 설정

`.env.example`을 복사해 `.env`로 저장하고 값을 채웁니다.

```bash
cp .env.example .env
```

```
# 필수
GEMINI_API_KEY=your_gemini_api_key

# YouTube 업로드 (선택)
YOUTUBE_PRIVACY=public

# BAND 게시 (선택 — API 발급 후 입력)
BAND_CLIENT_ID=
BAND_CLIENT_SECRET=
BAND_TARGET_KEY=

# 앱 공통 설정 (선택)
# DEFAULT_TITLE=우리 팀 경기영상   # 워터마크·YouTube 제목 기본값 (기본: 한울타리 FC 경기영상)
# HL_PORT=5000                    # 웹 서버 포트 (바꾸면 YouTube/BAND 리디렉션 URI도 재등록 필요)
```

---

## 실행

```bash
python app.py
```

브라우저가 자동으로 `http://127.0.0.1:5000`을 엽니다.

---

## 웹 UI 사용법

### 큐 관리 탭

1. **파일 선택** 버튼 또는 경로를 직접 입력 (여러 줄 가능)
2. 영상별로 **민감도**(많이 잡기 / 보통 / 엄선)를 개별 설정하거나 일괄 변경
3. 처리 방식 선택:

| 버튼 | 동작 |
|------|------|
| **큐에 추가** | 추가 후 수동으로 리뷰·생성·업로드 진행 |
| **YouTube까지 원스톱** | 추가 → 검출 → AI 판별 → 자동 승인 → 영상 생성 → YouTube 업로드 (완전 자동) |
| **BAND까지 원스톱** | 위 전체 + BAND 게시 (완전 자동) |

4. 원스톱 진행 중에는 단계별 진행 바가 표시되며 **취소** 버튼으로 중단 가능
5. 처리 현황에서 진행률·예상 잔여 시간·비용 확인. 처리 중인 영상은 개별 **취소** 버튼으로도 중단 가능
6. 오류 발생 시 **재시도** 버튼으로 재처리
7. `GEMINI_API_KEY`가 없어 비전 판별을 생략한 영상에는 "⚠ AI 판별 생략됨" 배지가 표시됨(이 경우 후보 전체가 자동 채택 취급됨)
8. **서버 로그** 카드의 "보기/새로고침" 버튼으로 `app.log` 최근 내용을 바로 확인 가능(문제 발생 시 원인 파악용)

> 처리 완료 시 브라우저 데스크톱 알림을 받으려면 알림 권한을 허용하세요.

### 리뷰 탭

1. 처리 완료된 영상이 아코디언으로 표시됨
2. **전체 신뢰도 슬라이더** — 전체 적용 시 모든 영상의 채택 기준을 한 번에 조정
3. 영상별로 펼쳐 후보 목록 확인 (썸네일·AI 설명·신뢰도·유형 포함)
4. 후보 행의 **▶ 미리보기** 버튼으로 원본 영상의 해당 구간을 바로 재생해 실제 장면 확인 가능
5. 체크박스로 구간을 포함/제외 후 **저장 & 다음** → 다음 미검토 영상으로 자동 이동
6. **AI 판단으로 전체 저장 & 영상 생성** — 신뢰도 임계 기준으로 일괄 저장 후 생성 시작

> 채택 구간이 0개인 영상은 "하이라이트 없음" 경고가 표시됩니다. 임계를 낮추거나 직접 선택하세요.

### 영상 생성 탭

1. **인증 상태 카드** — YouTube / BAND 인증 여부 확인 및 인증 진행
2. 품질(용량 우선 / 균형 / 화질 우선 / 초고속 복사) 선택 후 **일괄 생성**
3. 생성 완료 영상별로 **업로드** 버튼 → YouTube에 업로드
   - 썸네일: 신뢰도 가장 높은 구간의 원본 프레임 자동 추출
   - 설명: **득점(goal) 구간만** 챕터 타임스탬프로 표기
   - 제목: `베이스 | 영상명 | 날짜` 형식으로 영상별 자동 차별화
4. **생성 후 자동 YouTube 업로드** 옵션 켜면 생성 완료 즉시 자동 업로드
   - 안전을 위해 이 옵션은 **세션 동안만 유지**되고 저장되지 않습니다 — 새로고침·재시작 후에는 항상 꺼진 상태로 시작하므로 매번 다시 켜야 합니다.
5. YouTube 업로드 완료 후 **BAND에 게시** → 같은 날짜 영상 링크를 한 게시글에 묶어 게시
6. 파이프라인 요약 바에서 생성·업로드·게시 진행 상황 한눈에 확인
7. 제목·파일명 입력 중에는 자동 새로고침으로 커서 위치가 흐트러지지 않습니다

---

## YouTube 연동 설정

1. [Google Cloud Console](https://console.cloud.google.com)에서 프로젝트 생성
2. **YouTube Data API v3** 활성화
3. **OAuth 동의 화면 → 대상 → 테스트 사용자**에 본인 계정 추가
4. **사용자 인증 정보 → OAuth 클라이언트 ID** (데스크톱 앱) 생성
5. JSON 다운로드 → `client_secrets.json`으로 이름 변경 후 프로젝트 폴더에 저장
6. 앱 실행 → **영상 생성 탭 → YouTube 인증** 버튼 → Google 로그인

인증 완료 시 `youtube_token.json`이 생성되며 이후 자동 갱신됩니다.

---

## BAND 연동 설정

1. [BAND 개발자 센터](https://developers.band.us)에서 앱 등록
2. Redirect URI: `http://localhost:5000/auth/band/callback`
3. Privacy Policy URL: `https://raw.githubusercontent.com/sampark0626/HLEditor/main/PRIVACY.md`
4. 발급된 Client ID / Secret을 `.env`에 입력
5. `BAND_TARGET_KEY`: 게시할 밴드의 key (BAND 앱 → 설정에서 확인)를 `.env`에 입력
6. 앱 실행 → **영상 생성 탭 → BAND 인증** 버튼 → BAND 로그인

`BAND_TARGET_KEY`가 설정되어 있으면 원스톱 버튼에서 밴드 선택 없이 자동 게시됩니다.

---

## 민감도 프리셋

| 설정 | 후보 수 | 권장 상황 |
|------|---------|-----------|
| 많이 잡기 | 많음 | 골·장면이 적은 수비적 경기 |
| 보통 (기본) | 적당 | 일반적인 동호회 경기 |
| 엄선 | 적음 | 환호가 크고 뚜렷한 경기 |

겹치는 구간 처리: 두 후보의 영상 구간(각 13초)이 **2초 이상 겹치면** 볼륨 변화가 더 큰 쪽만 남깁니다. `soccer_highlights.py`의 `OVERLAP_MAX_SEC`로 조정 가능합니다.

---

## 비용 참고

- 30분 영상 기준 후보 약 15~40개
- Gemini 2.5 Flash 기준 영상 1개당 약 $0.01~0.03
- 오디오 분석·영상 생성은 무료 (로컬 처리)

---

## 산출물 및 재시작 시 복원

- 생성된 하이라이트 영상은 `output/`, 잡별 판별 결과 JSON은 `results/`에 저장됩니다.
- 앱 시작 시 `results/`에 있는 **24시간 이내** 결과는 자동으로 리뷰 큐에 "준비됨" 상태로 복원됩니다(서버가 재시작돼도 방금 처리한 결과를 다시 볼 수 있음). 그보다 오래된 결과나 원본 영상 파일이 사라진 경우는 복원되지 않습니다.
- 앱 시작 시 이전 세션에서 남은 임시 작업폴더도 자동 정리됩니다.

---

## 개발자용 (테스트 · lint)

```bash
pip install -r requirements-dev.txt

# 테스트 실행
pytest

# lint 검사 (자동 수정: ruff check . --fix)
ruff check .
```

`tests/`에 순수 로직(오디오 스파이크 검출, 후보 선택, YouTube 설명 생성, BAND 게시 포맷 등) 유닛 테스트와 Flask API 스모크 테스트가 있습니다. 실제 ffmpeg/Gemini 호출 없이 동작하도록 목(mock) 처리돼 있어 빠르게 실행됩니다.

---

## 주의사항

- `client_secrets.json`, `youtube_token.json`, `band_token.json`, `.env`는 `.gitignore`에 등록되어 있어 git에 올라가지 않습니다.
- YouTube 업로드는 본인 채널에만 가능합니다. 썸네일 업로드는 채널 전화번호 인증이 필요할 수 있습니다.
- 원스톱 BAND 게시는 `.env`의 `BAND_TARGET_KEY`를 사용합니다. 설정되지 않은 경우 영상 생성 탭에서 수동 게시하세요.
- ffmpeg가 PATH에 없으면 영상 처리가 실패합니다. 오류 발생 시 앱 상단 배너의 설치 안내를 따르세요.
- `HL_PORT`로 포트를 바꾸면 YouTube/BAND 리디렉션 URI도 각 개발자 콘솔에서 함께 변경해야 합니다.

---

자세한 설계 의도·검증 기록은 [PROJECT.md](PROJECT.md) · [PRIVACY.md](PRIVACY.md)를 참고하세요.
