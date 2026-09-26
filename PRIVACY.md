# Privacy Policy / 개인정보처리방침

**HLEditor — 축구 하이라이트 자동 추출 및 게시 도구**

Last updated: 2026-09-26

---

## English

### Overview
HLEditor is a tool for amateur soccer clubs. It extracts highlight clips from match videos using audio analysis and AI vision (Google Gemini), uploads the result to the user's own YouTube channel, and optionally posts the YouTube link to a designated BAND group.

It runs in two modes, both operated by the same single user (the club manager) with their own accounts:
- **Desktop mode** — videos are processed on the user's own PC.
- **Automated mode (optional)** — the user uploads match videos to a folder in their own Google Drive (`HLEditor/`); a Google Apps Script in the user's own Google account starts the user's own private Kaggle notebook, which processes the videos and sends the user a push notification.

### Data We Collect
| Data | Purpose | Storage |
|------|---------|---------|
| BAND OAuth token | Authenticate with BAND API to post YouTube links | Local file (`band_token.json`) on user's machine only |
| YouTube OAuth token | Authenticate with YouTube API to upload videos | Local file (`youtube_token.json`) on user's machine only |
| Google OAuth refresh token (automated mode: Google Drive + YouTube) | Read the user's uploaded match videos and upload the highlight to the user's YouTube channel | The user's own **private** Kaggle dataset, accessible only to the user |
| Match videos (automated mode) | Highlight extraction | Uploaded by the user to their own Google Drive folder; processed in the user's own Kaggle session (temporary copies deleted after use); originals moved to the Drive trash after 7 days by default |
| Processing records (automated mode) | Game numbering, the BAND post text, AI classification results | JSON files in the same Drive folder (`HLEditor/_state`) |
| Video files (desktop mode) | Local processing for highlight extraction | Processed locally, temporary files deleted after use |

### Google Drive access
Although the Google Drive permission technically covers the user's whole Drive, the application only lists, reads, moves and writes files **inside the `HLEditor/` folder** it creates in the user's own Drive. It does not access any other files.

### What We Do NOT Collect
- No personal information of BAND group members
- No video content is sent anywhere except the user's own Google Drive, the user's own Kaggle session, Google Gemini (short low-resolution clips for classification) and the user's own YouTube channel
- No analytics, tracking, or telemetry of any kind

### Third-Party Services
- **Google Gemini API**: Short low-resolution clips of candidate highlight segments are sent for AI classification. See [Google Privacy Policy](https://policies.google.com/privacy).
- **YouTube Data API v3**: Used solely to upload highlight videos to the user's own YouTube channel.
- **Google Drive API** (automated mode): Used solely to read and organize the videos inside the `HLEditor/` folder of the user's own Drive.
- **Kaggle** (automated mode): Runs the processing in the user's own private notebook. See [Kaggle Privacy Policy](https://www.kaggle.com/privacy).
- **ntfy.sh** (automated mode): Delivers push notifications to the user's phone. A notification contains only the game label, YouTube links and the BAND post text — no video or personal data.
- **BAND API**: Used solely to post YouTube video links to the user's designated BAND group.

### BAND API Usage
This application uses the following BAND API endpoints:
- `band.getBands` — to identify the target group
- `band.writePost` — to post YouTube video link(s) once per match day

All posts are initiated **manually by the club manager**. No automated or scheduled posting occurs without explicit human confirmation.

### Data Retention
In desktop mode, OAuth tokens are stored locally and can be deleted at any time by removing the token files. In automated mode, data stays in the user's own Google Drive and Kaggle accounts; access can be revoked at any time in Google Account → Security → Third-party access, and the token is removed by deleting the private Kaggle dataset. The application operates no server of its own and keeps no data anywhere else.

### Contact
For questions or concerns, please open an issue at:
https://github.com/sampark0626/HLEditor/issues

---

## 한국어

### 개요
HLEditor는 아마추어 축구 동호회를 위한 도구입니다. 경기 영상에서 오디오 분석과 AI 비전(Google Gemini)으로 하이라이트를 추출해 사용자 본인의 YouTube 채널에 올리고, 원하면 그 링크를 지정된 BAND 그룹에 게시합니다.

두 가지 방식이 있으며, 모두 한 명의 사용자(클럽 관리자)가 본인 계정으로만 운영합니다.
- **PC 방식** — 사용자 본인 PC에서 영상을 처리합니다.
- **자동 처리 방식(선택)** — 사용자가 본인 Google Drive의 `HLEditor/` 폴더에 경기 영상을 올리면, 사용자 본인 Google 계정의 Apps Script가 본인의 비공개 Kaggle 노트북을 실행해 영상을 처리하고 폰으로 알림을 보냅니다.

### 수집 항목
| 항목 | 목적 | 저장 위치 |
|------|------|----------|
| BAND OAuth 인증 토큰 | BAND API 인증 (YouTube 링크 게시용) | 사용자 로컬 파일(`band_token.json`)에만 저장 |
| YouTube OAuth 인증 토큰 | YouTube API 인증 (영상 업로드용) | 사용자 로컬 파일(`youtube_token.json`)에만 저장 |
| Google OAuth refresh token (자동 처리: Google Drive + YouTube) | 사용자가 올린 경기 영상 읽기, 하이라이트를 사용자 본인 YouTube 채널에 업로드 | 사용자 본인만 접근할 수 있는 **비공개** Kaggle 데이터셋 |
| 경기 영상 (자동 처리) | 하이라이트 추출 | 사용자가 본인 Google Drive 폴더에 업로드. 본인 Kaggle 세션에서 처리(임시 사본은 작업 후 삭제). 원본은 기본 7일 뒤 Drive 휴지통으로 이동 |
| 처리 기록 (자동 처리) | 경기 번호, BAND 게시 문구, AI 판별 결과 | 같은 Drive 폴더(`HLEditor/_state`)의 JSON 파일 |
| 영상 파일 (PC 방식) | 하이라이트 추출 (로컬 처리) | 로컬에서만 처리, 임시 파일은 작업 후 자동 삭제 |

### Google Drive 접근 범위
Google Drive 권한은 기술적으로 Drive 전체에 해당하지만, 이 애플리케이션은 사용자 본인 Drive에 만든 **`HLEditor/` 폴더 안의 파일만** 조회·읽기·이동·쓰기 합니다. 다른 파일에는 접근하지 않습니다.

### 수집하지 않는 정보
- BAND 그룹 회원의 개인정보 일체
- 영상 콘텐츠는 사용자 본인 Google Drive, 본인 Kaggle 세션, Google Gemini(판별용 저해상도 짧은 클립), 본인 YouTube 채널 외에는 어디에도 전송하지 않음
- 사용 통계, 트래킹, 원격 분석 정보 없음

### 제3자 서비스
- **Google Gemini API**: 하이라이트 판별을 위해 후보 구간의 저해상도 짧은 클립을 전송합니다. [Google 개인정보처리방침](https://policies.google.com/privacy) 참조.
- **YouTube Data API v3**: 사용자 본인의 YouTube 채널에 하이라이트 영상을 업로드하는 용도로만 사용합니다.
- **Google Drive API** (자동 처리): 사용자 본인 Drive의 `HLEditor/` 폴더 안 영상을 읽고 정리하는 용도로만 사용합니다.
- **Kaggle** (자동 처리): 사용자 본인의 비공개 노트북에서 처리를 실행합니다. [Kaggle 개인정보처리방침](https://www.kaggle.com/privacy) 참조.
- **ntfy.sh** (자동 처리): 사용자 폰으로 알림을 보냅니다. 알림에는 경기 번호, YouTube 링크, BAND 게시 문구만 담기며 영상이나 개인정보는 포함되지 않습니다.
- **BAND API**: 지정된 BAND 그룹에 YouTube 영상 링크를 게시하는 용도로만 사용합니다.

### BAND API 사용 범위
본 애플리케이션이 사용하는 BAND API 엔드포인트:
- `band.getBands` — 게시 대상 밴드 그룹 확인
- `band.writePost` — 경기일 당 1회, YouTube 영상 링크 게시

모든 게시는 **클럽 관리자가 직접 수동으로 실행**합니다. 사용자의 명시적 확인 없이 자동 또는 예약 게시는 발생하지 않습니다.

### 보유 기간
PC 방식에서 OAuth 토큰은 로컬에 저장되며, 토큰 파일을 삭제하면 언제든지 제거할 수 있습니다. 자동 처리 방식에서 데이터는 사용자 본인의 Google Drive와 Kaggle 계정 안에만 있습니다. 접근 권한은 Google 계정 → 보안 → 서드 파티 액세스에서 언제든지 취소할 수 있고, 비공개 Kaggle 데이터셋을 삭제하면 토큰도 제거됩니다. 이 애플리케이션은 자체 서버를 운영하지 않으며 그 밖의 곳에 데이터를 보관하지 않습니다.

### 제3자 제공
수집한 정보를 제3자에게 제공하지 않습니다.

### 문의
https://github.com/sampark0626/HLEditor/issues
