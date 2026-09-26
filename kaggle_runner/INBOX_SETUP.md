# 아이폰 자동 처리 설정 가이드

경기가 끝나고 **아이폰에서 영상만 올리면**, Kaggle이 자동으로 하이라이트를 만들어 YouTube에 올리고
**알림으로 BAND 글을 보내 주는** 흐름을 설정합니다. 웹호스팅이나 서버는 필요 없습니다.

```
아이폰 Drive 앱 ─업로드→ Drive HLEditor/01_inbox
      ▲                         │ (Apps Script가 일·월 5분, 그 외 1시간마다 확인)
      │                         ▼
  ntfy 알림 ◀── Kaggle 커널(hleditor-inbox): 병합 → 오디오 → 팬∥Gemini → 빌드 → YouTube
  (탭하면 BAND 글 복사)
```

설계 배경은 저장소 루트의 [MOBILE_PLAN.md](../MOBILE_PLAN.md)에 있습니다.
처음 한 번 설정하는 데 **40분 정도** 걸립니다.

## 준비물

- HLEditor가 설치된 PC (YouTube 업로드에 쓰던 `client_secrets.json`이 있는 상태)
- Google 계정 (YouTube 채널이 있는 계정)
- Kaggle 계정 — **휴대폰 인증 완료**(인터넷을 쓰는 커널 실행에 필요, 지금 Kaggle 러너를 쓰고 있다면 이미 됨)
- 아이폰

---

## 1. Google Cloud Console (5분)

1. [console.cloud.google.com](https://console.cloud.google.com)에서 YouTube 업로드에 쓰는 **기존 프로젝트**를 고릅니다.
2. **API 및 서비스 → 라이브러리**에서 `Google Drive API`를 찾아 **사용**을 누릅니다.
3. **OAuth 동의 화면(대상)**에서 게시 상태를 확인합니다. **'테스트'면 [앱 게시]를 눌러 '프로덕션'으로** 바꿉니다.
   테스트 상태로 두면 7일마다 인증이 만료되어 자동 처리가 멈춥니다.
   개인용이라 Google 검수를 받을 필요는 없습니다. 로그인할 때 "확인되지 않은 앱" 화면만 한 번 거치면 됩니다.

## 2. PC에서 비밀값 만들기 (5분)

HLEditor 폴더에서 실행합니다.

```bash
py -3 tools/make_kaggle_secrets.py --kaggle-user <Kaggle 아이디>
```

- 브라우저가 열리면 YouTube 채널 계정으로 로그인하고 권한(Drive, YouTube)에 동의합니다.
  "확인되지 않은 앱"이 나오면 **고급 → (안전하지 않음)으로 이동**을 누릅니다.
- 내 드라이브에 `HLEditor/` 폴더와 하위 폴더(01_inbox … _state)가 자동으로 생깁니다.
- `kaggle_secrets/` 폴더에 파일 3개가 생깁니다. **절대 공유하거나 커밋하지 마세요**(.gitignore 등록됨).
  - `hl_secrets.json`: Kaggle에 올릴 비밀값
  - `apps_script_properties.txt`: Apps Script에 넣을 값
  - `dataset-metadata.json`: kaggle CLI로 올릴 때만 씀

## 3. Kaggle (10분)

1. **비공개 데이터셋 만들기**
   - kaggle.com → Datasets → **New Dataset** → `hl_secrets.json` 업로드
   - 제목은 `hl-secrets`로 하고, **Private(비공개)**인지 꼭 확인한 뒤 Create를 누릅니다.
   - 주소가 `kaggle.com/datasets/<아이디>/hl-secrets` 인지 확인합니다.
2. **API 토큰 만들기**
   - kaggle.com → 오른쪽 위 프로필 → **Settings → API**에서 새 토큰을 만듭니다.
   - `KGAT_`로 시작하는 새 토큰을 권장합니다. 예전 방식(`kaggle.json` 안의 `key` 값)도 동작합니다.
3. 커널(노트북)은 따로 만들 필요 없습니다. 첫 자동 실행 때 `hleditor-inbox`라는 이름으로 생깁니다.

> 비밀값을 Kaggle Secrets가 아니라 비공개 데이터셋에 두는 이유: API로 실행한 커널에서는
> Kaggle Secrets를 읽을 수 없습니다(Kaggle/kaggle-cli issue #582).

## 4. Apps Script (10분)

1. [script.google.com](https://script.google.com) → **새 프로젝트**를 만들고 이름을 `HLEditor 트리거`로 바꿉니다.
2. 편집기의 `Code.gs` 내용을 모두 지우고, 저장소의 `kaggle_runner/apps_script/Code.gs` 내용을 붙여넣은 뒤 저장합니다.
3. 왼쪽 **⚙ 프로젝트 설정 → 스크립트 속성**에 `apps_script_properties.txt`의 값을 한 줄씩 넣습니다.
   `KAGGLE_API_TOKEN`에는 3단계에서 만든 토큰을 넣습니다.
4. 편집기 위쪽 함수 목록에서 **`installTrigger`**를 고르고 **실행**을 누릅니다. 권한 승인 창이 뜨면 허용합니다.
   "확인되지 않은 앱"이 나오면 **고급 → (안전하지 않음)으로 이동**을 누릅니다(내가 만든 스크립트라 정상).
   이제 5분마다 확인이 돌아갑니다(일·월요일 외에는 1시간에 한 번만 실제로 확인합니다).
5. 함수 목록에서 **`showStatus`**를 실행합니다. 실행 로그에 `Kaggle 커널 상태: IDLE`이 나오면 Kaggle 연결은 정상입니다.
   처음엔 커널이 없어서 IDLE이 맞습니다. 오류가 나면 `KAGGLE_USERNAME`과 `KAGGLE_API_TOKEN`을 확인합니다.
6. 함수 목록에서 **`testPush`**를 실행합니다. 영상 없이 Kaggle 커널을 한 번 돌려 보는 시험입니다.
   2~5분 뒤 kaggle.com → Code → **hleditor-inbox** → 최신 버전의 로그 끝에 `끝 — 경기 0개`가 보이면
   GitHub 코드 받기, 비밀값 데이터셋, Google 인증, Drive 폴더까지 모두 정상입니다.

## 5. 아이폰 (5분)

1. **ntfy** 앱(App Store)을 설치하고 **+ → 주제(topic)**에 `apps_script_properties.txt`의 `NTFY_TOPIC` 값을 넣어 구독한 뒤 알림을 허용합니다.
2. **Google Drive** 앱 → 설정에서 **Wi-Fi에서만 파일 전송을 끄고** 모바일 데이터로도 올릴 수 있게 합니다.
3. Drive 앱에서 `HLEditor/01_inbox` 폴더에 **별표**를 해 두면 찾기 쉽습니다.

## 6. 첫 시험 (20~30분)

1. 5분 이상인 짧은 경기 영상을 Drive 앱으로 `01_inbox`에 올립니다(아래 '평소 사용법' 참고).
2. 일·월요일이면 5분 안에 **"영상 1개 확인 — 처리 시작"** 알림이 옵니다.
   다른 요일이면 Apps Script에서 `tick`을 직접 실행하면 됩니다.
3. 20~30분 뒤 **"1경기 하이라이트 완료"** 알림이 오면 확인합니다.
   - ntfy 앱에서 메시지를 탭하면 BAND 글 전체가 복사되는지
   - YouTube 영상에 제목(`기본제목 | 1경기 | 날짜`)·득점 챕터·썸네일·우상단 워터마크가 들어갔는지
   - Drive의 원본이 `03_done`으로 옮겨졌는지
   - 완료 알림에 **"압축된 것 같습니다"** 경고가 없는지. 있으면 폰에서 화질이 낮아진 채 올라간 것이니, 올리는 방법을 바꿔야 합니다.
4. 문제가 있으면 Kaggle → Code → `hleditor-inbox` → 최신 버전의 **Log**를 확인합니다.

**첫 시험에서 같이 확인하면 좋은 것 (설계 가정)**
- 로그의 `받음:` 줄에 나오는 영상 길이
- 30분이 넘는 경기를 올렸다면, 두 파일이 한 경기로 합쳐졌는지(`나뉜 파일 2개를 한 경기로 합쳤습니다`)
- YouTube 공개 범위가 원하는 대로인지(기본은 PC의 `.env` 설정. 바꾸려면 `hl_secrets.json`의 `YOUTUBE_PRIVACY`를 고치고 데이터셋 새 버전 업로드)

---

## 평소 사용법

1. 경기가 끝나면 **Drive 앱 → `HLEditor/01_inbox` → + → 업로드 → 사진 및 동영상**에서 영상을 고릅니다.
   - XbotGo 앱의 영상은 먼저 사진 앱에 저장해 둡니다.
   - **30분 단위로 나뉜 파일은 한 번에 같이 고릅니다.** 자동으로 한 경기로 합쳐집니다.
   - 사파리로 올리지 마세요. 사진 보관함의 영상을 720p로 압축해서 올립니다.
2. 업로드가 끝날 때까지 **Drive 앱을 켜 둡니다**(5G로 경기당 4~10분). 다른 앱으로 가면 업로드가 멈출 수 있습니다.
3. "처리 시작" 알림이 오고, 경기당 20~30분 뒤 **완료 알림**이 옵니다.
4. 알림을 탭하고 ntfy 앱에서 **메시지를 탭하면 복사**됩니다. BAND에 붙여넣으면 끝입니다.
   완료 알림에는 그날 올린 경기가 모두 들어 있어서, 마지막 알림 하나만 붙여도 됩니다.

**2D(Dot Play)가 필요한 경기**는 `01_inbox` 대신 `01_inbox_2d`에 올립니다. 하이라이트 알림이 먼저 오고,
2D 결과는 Drive `05_output`에 올라간 뒤 별도 알림으로 옵니다. GPU로 실행되어 Kaggle 주간 GPU 한도를 씁니다.

## 폴더

| 폴더 | 뜻 |
|---|---|
| `01_inbox` | 여기에 올리면 자동 처리 |
| `01_inbox_2d` | 2D 변환까지 원하는 경기 |
| `02_processing` | 처리 중(건드리지 마세요) |
| `03_done` | 처리가 끝난 원본. 7일 뒤 휴지통으로 옮겨지고, 휴지통은 Drive가 30일 뒤에 비웁니다 |
| `04_failed` | 실패한 원본. **`01_inbox`로 옮기면 다시 처리**합니다 |
| `05_output` | 2D 결과, YouTube 업로드에 실패했을 때의 하이라이트 백업 |
| `_state` | 처리 기록(날짜별 게시 목록, 판별 결과). 지우지 마세요 |

## 문제 해결

| 증상 | 할 일 |
|---|---|
| 알림이 전혀 안 옴 | Apps Script → **실행(Executions)** 기록 확인. `showStatus` 실행 → Kaggle `hleditor-inbox` 로그 확인 |
| "Google 인증 만료" 알림 | PC에서 `py -3 tools/make_kaggle_secrets.py` 재실행 → Kaggle `hl-secrets` 데이터셋에 **New Version**으로 `hl_secrets.json` 업로드. ntfy topic은 그대로 유지됩니다 |
| "Kaggle 자동 실행 중지" 알림 | Kaggle 로그로 원인을 고친 뒤 Apps Script에서 `resetBreaker` 실행 |
| "처리 실패" 알림 | 알림에 적힌 이유 확인. 원본은 `04_failed`에 있음 → `01_inbox`로 옮기면 재시도 |
| "다음 파트가 오지 않아…" | 나뉜 파일 중 일부만 올라간 상태로 40분이 지남. 나머지를 올리면 따로 처리되니, 합쳐야 하면 PC 앱으로 처리 |
| 공휴일 등 다른 요일 경기 | 스크립트 속성 `FAST_DAYS`에 그 요일 번호를 잠깐 추가(1=월 … 7=일). 또는 아래 실행 링크 사용 |
| Drive 저장공간 부족 | 무료 15GB는 한 경기일 분량(최대 약 15GB)도 빠듯함 → Google One 100GB 또는 `RETENTION_DAYS`를 줄이기 |

## 선택 기능

**실행 링크(지금 바로 확인):** Apps Script → 배포 → 새 배포 → 유형 '웹 앱'
→ 실행 계정 '나', 액세스 권한 '모든 사용자' → 배포 → URL 복사.
`URL?key=<KICK_KEY>` 를 폰 홈 화면에 추가하면, 누를 때마다 요일과 상관없이 바로 확인합니다.

**단축어 자동화(업로드 끝나면 즉시 시작):** 단축어 앱 → 자동화 → + → **앱** → Google Drive
→ '닫힘' 선택 → **즉시 실행** → 동작 'URL 콘텐츠 가져오기'에 위 실행 링크를 넣고, '실행 시 알림'은 끕니다.
Drive 앱에서 나오는 순간 처리가 시작됩니다. 일·월에는 5분 확인과 2~3분 차이라 없어도 됩니다.

## 설정값 (`hl_secrets.json`에 추가 가능 — 고친 뒤 데이터셋 새 버전 업로드)

| 키 | 기본값 | 뜻 |
|---|---|---|
| `DEFAULT_TITLE` | PC `.env`의 값 | 워터마크·YouTube 제목 앞부분 |
| `YOUTUBE_PRIVACY` | PC `.env`의 값 | `public` / `unlisted` / `private` |
| `SENSITIVITY` | `normal` | 후보 검출 민감도: `more` / `normal` / `strict` |
| `QUALITY` | `balanced` | 인코딩: `size` / `balanced` / `quality` |
| `RETENTION_DAYS` | `7` | `03_done` 원본 보관 일수 |
| `WAIT_NEXT_PART_MIN` | `40` | 나뉜 파일의 다음 파트를 기다리는 시간(분) |
| `SETTLE_SEC` | `180` | 마지막 파일이 도착하고 처리를 시작하기까지 기다리는 시간(초) |
| `MIN_GAME_SEC` | `300` | 이보다 짧은 영상은 처리하지 않음(실수로 올린 영상 보호) |

## PC에서 시험하기 (Drive·Kaggle 없이)

```bash
py -3 -m kaggle_runner.inbox_runner --local-inbox D:\hl_test --dry-run --no-vision --settle-sec 0 --once
```

`D:\hl_test\01_inbox`에 영상을 넣고 실행합니다. YouTube 업로드와 알림은 콘솔 출력으로 대신하고,
만든 하이라이트는 `05_output`에 남습니다. `--no-vision`을 빼면 Gemini 판별까지 합니다(비용 발생).
