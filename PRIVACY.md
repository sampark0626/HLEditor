# Privacy Policy / 개인정보처리방침

**HLEditor — 축구 하이라이트 자동 추출 및 게시 도구**

Last updated: 2026-06-07

---

## English

### Overview
HLEditor is a local desktop tool for amateur soccer clubs. It processes match videos locally, extracts highlight clips using audio analysis and AI vision (Google Gemini), and optionally posts the resulting YouTube link to a designated BAND group.

### Data We Collect
| Data | Purpose | Storage |
|------|---------|---------|
| BAND OAuth token | Authenticate with BAND API to post YouTube links | Local file (`band_token.json`) on user's machine only |
| YouTube OAuth token | Authenticate with YouTube API to upload videos | Local file (`youtube_token.json`) on user's machine only |
| Video files | Local processing for highlight extraction | Processed locally, temporary files deleted after use |

### What We Do NOT Collect
- No personal information of BAND group members
- No video content is sent to any server other than YouTube (by the user's explicit action)
- No analytics, tracking, or telemetry of any kind

### Third-Party Services
- **Google Gemini API**: Frame images from candidate highlight segments are sent for AI classification. See [Google Privacy Policy](https://policies.google.com/privacy).
- **YouTube Data API v3**: Used solely to upload highlight videos to the user's own YouTube channel.
- **BAND API**: Used solely to post YouTube video links to the user's designated BAND group.

### BAND API Usage
This application uses the following BAND API endpoints:
- `band.getBands` — to identify the target group
- `band.writePost` — to post YouTube video link(s) once per match day

All posts are initiated **manually by the club manager**. No automated or scheduled posting occurs without explicit human confirmation.

### Data Retention
OAuth tokens are stored locally and can be deleted at any time by removing the token files. No data is retained on any remote server by this application.

### Contact
For questions or concerns, please open an issue at:
https://github.com/sampark0626/HLEditor/issues

---

## 한국어

### 개요
HLEditor는 아마추어 축구 동호회를 위한 로컬 데스크탑 도구입니다. 경기 영상을 로컬에서 처리하여 하이라이트 클립을 추출하고, 생성된 YouTube 링크를 지정된 BAND 그룹에 게시하는 기능을 제공합니다.

### 수집 항목
| 항목 | 목적 | 저장 위치 |
|------|------|----------|
| BAND OAuth 인증 토큰 | BAND API 인증 (YouTube 링크 게시용) | 사용자 로컬 파일(`band_token.json`)에만 저장 |
| YouTube OAuth 인증 토큰 | YouTube API 인증 (영상 업로드용) | 사용자 로컬 파일(`youtube_token.json`)에만 저장 |
| 영상 파일 | 하이라이트 추출 (로컬 처리) | 로컬에서만 처리, 임시 파일은 작업 후 자동 삭제 |

### 수집하지 않는 정보
- BAND 그룹 회원의 개인정보 일체
- 영상 콘텐츠 (YouTube 업로드 외 어떠한 외부 서버에도 전송하지 않음)
- 사용 통계, 트래킹, 원격 분석 정보 없음

### 제3자 서비스
- **Google Gemini API**: 하이라이트 판별을 위해 후보 구간의 프레임 이미지를 전송합니다. [Google 개인정보처리방침](https://policies.google.com/privacy) 참조.
- **YouTube Data API v3**: 사용자 본인의 YouTube 채널에 하이라이트 영상을 업로드하는 용도로만 사용합니다.
- **BAND API**: 지정된 BAND 그룹에 YouTube 영상 링크를 게시하는 용도로만 사용합니다.

### BAND API 사용 범위
본 애플리케이션이 사용하는 BAND API 엔드포인트:
- `band.getBands` — 게시 대상 밴드 그룹 확인
- `band.writePost` — 경기일 당 1회, YouTube 영상 링크 게시

모든 게시는 **클럽 관리자가 직접 수동으로 실행**합니다. 사용자의 명시적 확인 없이 자동 또는 예약 게시는 발생하지 않습니다.

### 보유 기간
OAuth 토큰은 로컬에 저장되며, 토큰 파일을 삭제하면 언제든지 제거할 수 있습니다. 이 애플리케이션은 어떠한 원격 서버에도 데이터를 보관하지 않습니다.

### 제3자 제공
수집한 정보를 제3자에게 제공하지 않습니다.

### 문의
https://github.com/sampark0626/HLEditor/issues
