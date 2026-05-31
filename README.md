# HLEditor — 축구 하이라이트 자동 추출

동호회/아마추어 축구 경기 영상에서 **음성 볼륨 급증 + Gemini 비전 판별**로 하이라이트 구간을 자동으로 찾아 하나의 영상으로 만드는 도구입니다.

---

## 동작 구조

영상을 사람이 다 보지 않고 하이라이트를 찾기 위해 **2단계 필터**를 씁니다.

```
오디오 추출 → RMS 볼륨 + rolling baseline → 급증(spike) 후보 검출   ← 1차: 싸고 빠름
            → 후보 구간 프레임 추출 → Gemini 비전 판별(유형/신뢰도)   ← 2차: 비싸지만 정확
            → 신뢰도 임계 이상만 채택 → ffmpeg로 클립 분할·병합
```

- **1차 (오디오)**: 골·슈팅·결정적 장면에서는 환호·외침으로 볼륨이 순간 튄다. baseline 대비 상대 급증으로 "언제"를 좁힌다.
- **2차 (비전)**: 급증 구간의 프레임을 Gemini에 보내 "무엇"인지 판별. 단순 킥오프·패스 돌리기는 여기서 걸러진다.

싼 오디오 필터로 후보를 좁히고 비싼 비전 판별을 최소화하는 비용·정확도 균형 설계입니다.

---

## 파일 구성

| 파일 | 설명 |
|------|------|
| `soccer_highlights.py` | 메인 스크립트. 전체 파이프라인 단일 파일 구현 |
| `PROJECT.md` | 설계 의도·검증 기록·주의사항 핸드오프 문서 |
| `.env` | `GEMINI_API_KEY` 등 환경변수 (git 제외) |
| `README.md` | 이 문서 |

스크립트 상단의 **조절 파라미터 블록**(`WIN_SEC` ~ `CONF_MAYBE`)이 동작 대부분을 좌우합니다. 로직보다 이 값들을 먼저 보세요.

---

## 설치

### 1) 의존성

```bash
pip install numpy scipy google-genai
```

`ffmpeg` / `ffprobe` 가 PATH에 있어야 합니다.

- **macOS**: `brew install ffmpeg`
- **Ubuntu**: `sudo apt install ffmpeg`
- **Windows**: `winget install Gyan.FFmpeg` (또는 [ffmpeg.org](https://ffmpeg.org/download.html)에서 받아 PATH 등록)

> **Windows 참고**: Microsoft Store의 `python` 별칭(0바이트 스텁)이 아닌 [python.org](https://www.python.org/downloads/) 정식 설치본 또는 `winget install Python.Python.3.12` 을 사용하세요. 설치 시 "Add Python to PATH"를 체크합니다.

### 2) API 키

`.env` 파일에 키를 넣습니다 (이 저장소는 `.env`를 git에서 제외합니다).

```
GEMINI_API_KEY=your_key_here
```

또는 환경변수로 직접 설정:

```bash
export GEMINI_API_KEY="..."          # macOS/Linux
$env:GEMINI_API_KEY="..."            # Windows PowerShell
```

> 스크립트는 `--api-key` 인자 → `GEMINI_API_KEY` 환경변수 순으로 키를 읽습니다. `.env` 파일을 쓰려면 셸에서 export 하거나 `python-dotenv` 등으로 로드하세요.

---

## 웹 UI (권장)

명령줄이 익숙하지 않다면 브라우저 기반 UI를 쓰세요. CLI와 동일한 파이프라인을 클릭으로 진행합니다.

```bash
pip install flask          # 최초 1회 (requirements.txt 에 포함)
python app.py              # http://127.0.0.1:5000 자동 오픈
```

화면은 4단계로 구성됩니다.

1. **영상 선택 & 후보 검출** — `찾아보기…`로 파일을 고르고(또는 경로 입력) 오디오 급증 후보를 검출 (무료). **감지 민감도**(많이 잡기/보통/엄선)로 후보 수를 조절
2. **Gemini 비전 판별** — 병렬 수를 정하고 실행. **진행 바**로 "32/72" 식 진행률 표시. 결과가 표에 채택/확인필요/제외로 표시되고 `results.json`에 자동 저장. **■ 중단** 버튼으로 진행 중 취소 가능(이미 시작된 호출만 마무리). 끝나면 **사용 토큰·예상 비용(USD)** 이 표시됨
3. **신뢰도 임계 조정** — 슬라이더를 움직이면 **API 재호출 없이** 채택 구간이 실시간으로 바뀜
4. **하이라이트 영상 생성** — **품질**(용량우선/균형/화질우선/초고속 복사)과 **제목(우상단)** 을 정하고 생성. 진행 바로 인코딩 진행률 표시, 완료되면 결과 영상을 바로 미리보기
   - 품질: 용량은 CRF가, 속도는 preset이 좌우. **균형**(fast/CRF25)이 기본, **용량 우선**(CRF26)은 더 작게, **초고속**은 무손실 복사(가장 빠름·타이틀 없음)

> 화면 하단 **진행 로그** 패널에 각 단계의 시작/완료/실패가 타임스탬프로 기록되고, 서버는 `app.log`에도 동일 로그를 남깁니다.

> API 키는 `app.py`가 `.env`의 `GEMINI_API_KEY`를 자동으로 읽습니다 (CLI와 달리 별도 export 불필요).

아래 CLI는 자동화·세밀한 제어가 필요할 때 사용합니다.

---

## 사용법 (CLI)

권장 흐름: **항상 `--dry-run`으로 후보·판별 결과를 먼저 확인하고**, 파라미터를 맞춘 뒤 마지막에 영상을 생성합니다.

```bash
# 1단계: 오디오 후보만 확인 (API 호출 없음, 무료, 빠름)
python3 soccer_highlights.py 경기.mov --no-vision --dry-run

# 2단계: 비전 판별 결과까지 확인 (영상 생성은 생략)
python3 soccer_highlights.py 경기.mov --dry-run

# 3단계: 실제 하이라이트 영상 생성
python3 soccer_highlights.py 경기.mov -o highlights.mp4
```

### 권장 워크플로 (30분 영상에서 특히 중요)

비전을 **한 번만** 돌려 JSON으로 저장하고, 임계 튜닝은 API 재호출 없이 합니다.

```bash
# 1) 비전 한 번 돌리고 결과를 JSON으로 저장 (영상은 아직 안 만듦)
python3 soccer_highlights.py 경기.mov --save-json results.json --dry-run

# 2) 저장된 결과로 임계만 바꿔 재선택 — Gemini 재호출 0회, 무료/즉시
python3 soccer_highlights.py --from-json results.json --conf 0.55 --dry-run

# 3) 마음에 들면 그대로 영상 생성 (역시 비전 재호출 없음)
python3 soccer_highlights.py --from-json results.json --conf 0.55 -o highlights.mp4
```

`--from-json` 사용 시 영상 경로는 JSON에 기록돼 있어 생략 가능합니다(원본 위치가 바뀌었으면 첫 인자로 새 경로를 넘기면 덮어씁니다).

### 옵션

| 옵션 | 설명 |
|------|------|
| `-o, --output PATH` | 출력 파일 (기본 `highlights.mp4`) |
| `--api-key KEY` | 환경변수 대신 키 직접 전달 |
| `--no-vision` | 비전 판별 생략, 오디오 후보 전체 사용 |
| `--dry-run` | 후보/판별만 출력, 영상 미생성 |
| `--conf 0.5` | 자동 채택 신뢰도 임계 조정 (기본 0.70) |
| `--workers N` | **비전 호출 병렬 수** (기본 6, 1~8로 클램프) |
| `--title "..."` | 영상 우상단에 넣을 타이틀 텍스트 (예: `"한울타리 FC 경기영상"`) |
| `--sensitivity` | 후보 검출 민감도: `more`(많이)/`normal`(기본)/`strict`(엄선) |
| `--quality` | 출력 품질: `size`/`balanced`(기본)/`quality`/`copy`(무손실 복사) |
| `--save-json PATH` | 후보/비전 판별 결과를 JSON으로 저장 (재선택용) |
| `--from-json PATH` | 저장된 JSON에서 읽어 비전 재호출 없이 재선택·재생성 |

판별 결과는 `✅ 채택` / `⚠️ 확인필요` / `❌` 로 표시됩니다. `확인필요`는 신뢰도 애매(0.40~0.70) 구간으로, 사람이 빠르게 검토하는 용도입니다.

---

## 비전 호출 병렬화

후보가 많은 30분 영상에서는 비전 판별이 가장 느린 단계였습니다. 이제 `ThreadPoolExecutor`로 후보를 **동시에 N개씩** 처리합니다.

- 기본 동시성 6 (`VISION_WORKERS`), `--workers` 로 조정 가능 (1~8 클램프)
- 완료되는 순서대로 결과를 실시간 출력하되, 내부적으로 원본 순서를 유지해 결과가 엉키지 않음
- 후보가 100개 이상인 풀경기에서 순차 처리 대비 수 배 빠름

```bash
# rate limit(429)이 자주 뜨면 동시성을 낮춤
python3 soccer_highlights.py 경기.mov --workers 4 --dry-run
```

> Gemini rate limit을 고려해 동시성은 8을 넘기지 않도록 강제합니다. 그래도 429가 나면 `--workers`를 낮추세요.

---

## 파라미터 가이드

스크립트 상단에서 조정합니다.

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
| `VISION_WORKERS` (6) | 비전 호출 병렬 수 | rate limit 뜨면 ↓ |
| `CONF_AUTO` (0.70) | 자동 채택 임계 | 좋은 장면 누락시 ↓ |
| `CONF_MAYBE` (0.40) | '확인필요' 분류 하한 | — |

---

## 30분 영상 적용 시 주의사항

- **비전 API 비용**: 후보마다 Gemini 호출 1번. 먼저 `--no-vision --dry-run`으로 후보 수를 보고, 과하면 `SPIKE_PERCENTILE`(97~98)·`SPIKE_MIN_DB`(10+)를 올려 비전 호출 자체를 줄이세요.
- **최종 재인코딩 시간**: `build_output`은 채택 구간을 `libx264`로 재인코딩 후 concat합니다. 구간이 수십 개면 이 단계가 가장 느립니다.
- **메모리**: 오디오는 16kHz mono(30분≈60MB), 프레임은 후보별로 추출 후 작업폴더와 함께 삭제되어 누적되지 않습니다.

자세한 설계 의도·검증 기록은 [PROJECT.md](PROJECT.md)를 참고하세요.

---

## 환경 의존성

- Python 3.x, `numpy`, `scipy`, `google-genai`
- `ffmpeg` / `ffprobe` (PATH 등록 필요)
- `GEMINI_API_KEY` (비전 사용 시)
