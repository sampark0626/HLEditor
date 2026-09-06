#!/usr/bin/env python3
"""
soccer_highlights.py
====================
축구 영상에서 음성 볼륨 급증 + Gemini 비전 판별로 하이라이트를 자동 추출합니다.

파이프라인:
  1) ffmpeg로 오디오 추출 (mono 16kHz)
  2) RMS 볼륨 + rolling baseline으로 급증(spike) 후보 구간 검출
  3) 각 후보 구간에서 프레임을 0.5초 간격으로 추출
  4) Gemini 비전 모델에 프레임 묶음을 보내 하이라이트 여부/유형/신뢰도 판별 (병렬)
  5) 신뢰도 임계 이상 구간을 ffmpeg로 잘라 이어붙여 최종 하이라이트 영상 생성

사용법:
  pip install numpy scipy google-genai
  export GEMINI_API_KEY="..."   (또는 --api-key 로 전달)
  python3 soccer_highlights.py input.mov

  # 비전 판별 끄고 오디오 후보만 보기:
  python3 soccer_highlights.py input.mov --no-vision --dry-run
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from scipy.io import wavfile

import pan_signal
import config

# Windows cp949 콘솔에서 진행 로그의 이모지(✅⚠️❌)를 print할 때
# UnicodeEncodeError가 발생해 처리 스레드가 죽는 것을 방지 — stdout/stderr를
# UTF-8로 재설정한다(모듈 임포트 시 1회, CLI 직접 실행/앱 임포트 양쪽에 적용).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 조절 파라미터 (여기만 만지면 됩니다)
# ----------------------------------------------------------------------------
WIN_SEC        = 0.5    # RMS 계산 윈도우 (초). 짧을수록 민감
HOP_SEC        = 0.25   # 윈도우 이동 간격 (초)
BASELINE_SEC   = 20.0   # rolling baseline 추정 구간 (초)
SPIKE_PERCENTILE = 95   # delta 분포 상위 몇 %를 spike로 볼지 (높을수록 후보 적음)
SPIKE_MIN_DB   = 8.0    # baseline 대비 최소 상승폭 (dB). 절대 하한
MERGE_GAP_SEC  = 4.0    # 이 간격 이내 spike는 한 장면으로 병합
OVERLAP_MAX_SEC = 2.0   # 실제 잘라낼 구간이 이 초 이상 겹치면 delta_db 높은 쪽만 유지

PRE_SEC        = 8.0    # 급증(peak) 시점 기준 앞으로 포함할 길이 (초)
POST_SEC       = 5.0    # 급증 시점 기준 뒤로 포함할 길이 (초)

FRAME_INTERVAL = 0.5    # 비전 판별용 프레임 추출 간격 (초)
MAX_FRAMES     = int(config.get_env("MAX_FRAMES", "12"))      # 후보당 Gemini에 보낼 최대 프레임 수
VISION_MODEL   = config.get_env("GEMINI_MODEL", "gemini-2.5-flash")   # 비용/속도 균형. 정확도 더 원하면 gemini-2.5-pro
VISION_WORKERS = 6      # 비전 호출 동시 병렬 수 (Gemini rate limit 고려해 4~8 권장)
VISION_RETRIES = 4      # 503/429 등 일시적 오류 시 후보당 최대 재시도 횟수
RETRY_BASE_SEC = 2.0    # 재시도 지수 백오프 기준 (2,4,8,... 초 + 지터)
CONF_AUTO      = 0.70   # 이 신뢰도 이상이면 자동 채택
CONF_MAYBE     = 0.40   # 이 값 이상 ~ AUTO 미만이면 '확인 필요'로 분류

SPAWN_RETRIES  = 4      # ffmpeg 프로세스 생성 실패(WinError 5 등) 시 재시도 횟수

# --- 출력 인코딩 (용량/화질 트레이드오프) ---
# 속도는 preset이, 용량은 CRF가 좌우한다. fast+CRF25가 속도·용량 균형점.
ENCODE_PRESET  = "fast"    # 압축 속도. veryfast(빠름,큼) ~ slow(느림,작음)
ENCODE_CRF     = 25        # 화질/용량. 낮을수록 고화질·고용량 (18~28 권장)
MAX_HEIGHT     = 1080      # 이 높이를 넘으면 다운스케일 (None이면 원본 유지)
BUILD_CLIP_WORKERS = 3     # 클립별 재인코딩 동시 처리 수 (ffmpeg는 별도 프로세스라 GIL 영향 없음)

# 출력 품질 프리셋 (UI/CLI 선택용). copy=stream-copy(재인코딩 없음, 타이틀 불가)
QUALITY_PRESETS = {
    "size":    {"preset": "fast",   "crf": 26, "label": "용량 우선"},
    "balanced":{"preset": "fast",   "crf": 25, "label": "균형(기본)"},
    "quality": {"preset": "medium", "crf": 23, "label": "화질 우선"},
    "copy":    {"copy": True,                  "label": "초고속(무손실 복사·타이틀 없음)"},
}

# 검출 민감도 프리셋 (UI/CLI 선택용) → (percentile, min_db)
SENSITIVITY_PRESETS = {
    "more":   {"percentile": 90, "min_db": 6.0, "label": "많이 잡기"},
    "normal": {"percentile": 95, "min_db": 8.0, "label": "보통(기본)"},
    "strict": {"percentile": 98, "min_db": 11.0, "label": "엄선"},
}

# --- 타이틀 워터마크 (영상 우상단 작은 제목) ---
TITLE_TEXT     = ""     # 비우면 타이틀 없음. 예: "한울타리 FC 경기영상"
TITLE_FONT     = r"C:\Windows\Fonts\NanumGothic.ttf"  # 한글 지원 폰트
TITLE_FONTSIZE = 22
TITLE_MARGIN   = 24     # 우/상단 여백 (px)

# --- Gemini 단가 (USD per 1M tokens, 모델별 가변) ---
if "pro" in VISION_MODEL.lower():
    PRICE_IN_PER_M  = 3.00
    PRICE_OUT_PER_M = 9.00
else:
    PRICE_IN_PER_M  = 0.30
    PRICE_OUT_PER_M = 2.50


# ----------------------------------------------------------------------------
def run(cmd, cwd=None):
    """ffmpeg/ffprobe 실행 헬퍼.

    Windows에서 여러 ffmpeg를 동시에 spawn할 때 간헐적으로 발생하는
    PermissionError([WinError 5])는 일시적이므로 짧게 재시도한다.
    cwd: 지정 시 해당 디렉터리에서 실행 (drawtext 폰트를 콜론 없는 상대경로로
         참조하기 위해 사용).
    """
    last_exc = None
    for attempt in range(SPAWN_RETRIES):
        try:
            # encoding/errors 명시: Windows 한글 로케일(cp949)에서 ffmpeg의 비-cp949
            # stderr 출력을 디코딩하다 UnicodeDecodeError로 죽는 것을 방지한다.
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=cwd)
            break
        except (PermissionError, OSError) as e:
            last_exc = e
            if attempt == SPAWN_RETRIES - 1:
                raise
            time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.3))
    else:  # pragma: no cover
        raise last_exc
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        raise RuntimeError(f"command failed: {' '.join(cmd[:3])}...")
    return r.stdout


def probe_duration(video):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", video])
    return float(out.strip())


# ----------------------------------------------------------------------------
def detect_spikes(video, workdir, percentile=None, min_db=None,
                  pre_sec=None, post_sec=None):
    """오디오를 추출하고 볼륨 급증 후보 구간을 반환.

    percentile/min_db: 지정 시 모듈 기본값(SPIKE_PERCENTILE/SPIKE_MIN_DB)을
        덮어쓴다. 값이 클수록 후보가 줄어든다(엄선).
    pre_sec/post_sec: 구간 앞뒤 길이(초). 탈중복 계산에 사용.
    """
    wav = workdir / "audio.wav"
    run(["ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", str(wav), "-loglevel", "error"])

    sr, data = wavfile.read(str(wav))
    return detect_spikes_from_signal(sr, data, percentile=percentile, min_db=min_db,
                                     pre_sec=pre_sec, post_sec=post_sec)


def detect_spikes_from_signal(sr, data, percentile=None, min_db=None,
                              pre_sec=None, post_sec=None):
    """순수 신호 처리부: (샘플레이트, PCM 배열)에서 볼륨 급증 후보 구간을 계산한다.

    ffmpeg/파일 I/O가 없어 합성 신호로 유닛 테스트하기 쉽다.
    """
    percentile = SPIKE_PERCENTILE if percentile is None else percentile
    min_db = SPIKE_MIN_DB if min_db is None else min_db
    pre_sec  = PRE_SEC  if pre_sec  is None else pre_sec
    post_sec = POST_SEC if post_sec is None else post_sec

    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float64)
    data /= (np.max(np.abs(data)) + 1e-9)

    win = int(sr * WIN_SEC)
    hop = int(sr * HOP_SEC)
    rms, times = [], []
    for start in range(0, len(data) - win, hop):
        seg = data[start:start + win]
        rms.append(np.sqrt(np.mean(seg ** 2)))
        times.append(start / sr)
    rms = np.array(rms)
    times = np.array(times)
    db = 20 * np.log10(rms + 1e-9)

    # rolling median baseline
    bwin = int(BASELINE_SEC / HOP_SEC)
    baseline = np.array([np.median(db[max(0, i - bwin):i + bwin + 1])
                         for i in range(len(db))])
    delta = db - baseline

    thr = max(np.percentile(delta, percentile), min_db)
    above = delta > thr

    # 연속 구간 묶기
    spikes = []
    i = 0
    while i < len(above):
        if above[i]:
            j = i
            while j < len(above) and above[j]:
                j += 1
            k = i + int(np.argmax(delta[i:j]))
            spikes.append([times[i], times[j - 1], times[k], float(delta[k])])
            i = j
        else:
            i += 1

    # 인접 spike 병합 (peak 간격 기준)
    merged = []
    for s in spikes:
        if merged and s[0] - merged[-1][1] < MERGE_GAP_SEC:
            p = merged[-1]
            keep_peak = p[2] if p[3] >= s[3] else s[2]
            merged[-1] = [p[0], s[1], keep_peak, max(p[3], s[3])]
        else:
            merged.append(s)

    # 구간 겹침 탈중복: 실제 잘라낼 세그먼트(peak ± PRE/POST)가 OVERLAP_MAX_SEC 초 초과
    # 겹치면 delta_db 높은 쪽 peak만 남긴다.
    # 예) PRE=8, POST=5 → 13초 구간. peak 간격 11초 미만이면 2초 이상 겹침.
    deduped = []
    for s in merged:
        if not deduped:
            deduped.append(s)
            continue
        prev = deduped[-1]
        # 구간 겹침량 = (prev_peak + post_sec) - (curr_peak - pre_sec)
        overlap = (prev[2] + post_sec) - (s[2] - pre_sec)
        if overlap > OVERLAP_MAX_SEC:
            # 겹침이 큼 → 더 강한 spike(delta_db 높은 것)의 peak만 유지
            if s[3] > prev[3]:
                deduped[-1] = s   # 현재 것이 더 강하면 교체
            # else: 이전 것이 더 강하므로 그냥 버림
        else:
            deduped.append(s)

    return [{"start": m[0], "end": m[1], "peak": m[2], "delta_db": m[3]}
            for m in deduped]


# ----------------------------------------------------------------------------
def extract_video_clip_and_thumbnail(video, peak, workdir, idx, pre_sec=None, post_sec=None):
    """후보 구간(peak 앞뒤)을 잘라 저용량 mp4 파일로 인코딩하고 대표 썸네일(JPEG) 1장도 함께 추출."""
    pre_sec  = PRE_SEC  if pre_sec  is None else pre_sec
    post_sec = POST_SEC if post_sec is None else post_sec
    seg_start = max(0.0, peak - pre_sec)
    seg_len = pre_sec + post_sec
    video_path = workdir / f"cand{idx}.mp4"
    
    # 1. 비디오 클립 추출 (480p 저해상도 + 매우 빠른 인코딩 + 높은 CRF로 용량 대폭 축소)
    cmd_video = [
        "ffmpeg", "-y", "-ss", f"{seg_start:.2f}", "-i", video,
        "-t", f"{seg_len:.2f}", "-vf", "scale=-2:480",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "64k",
        str(video_path), "-loglevel", "error"
    ]
    run(cmd_video)
    
    # 2. 대표 썸네일 프레임 추출 (세그먼트 중간 시점, Web UI 리뷰 탭 렌더링용)
    thumb_path = workdir / f"cand{idx}_000.jpg"
    thumb_ts = seg_start + (seg_len / 2.0)
    cmd_thumb = [
        "ffmpeg", "-y", "-ss", f"{thumb_ts:.2f}", "-i", video,
        "-vframes", "1", "-vf", "scale=640:-1",
        str(thumb_path), "-loglevel", "error"
    ]
    try:
        run(cmd_thumb)
    except Exception:
        pass  # 썸네일 추출 실패는 파이프라인 진행을 중단시키지 않음
        
    return video_path


# ----------------------------------------------------------------------------
VISION_PROMPT = """당신은 아마추어(동호회) 축구 경기 유튜브 하이라이트 영상의 전문 편집자입니다.
제공된 파일은 한 후보 구간의 축구 경기 비디오 클립으로, 비디오(영상)와 오디오(음향)가 모두 포함되어 있습니다.
반드시 영상 화면과 오디오 트랙을 모두 종합적으로 분석하여 판정하십시오.

[멀티모달 분석 방법]
1. **비주얼 분석**:
   - 카메라는 AI 추적 카메라로, 공과 경기 흐름을 따라 좌우로 패닝(회전)합니다. 배경 이동을 고려하여 공의 궤적과 선수의 몸짓을 추적하세요.
   - 광각 풀샷이라 선수와 공이 매우 작으므로 집중해서 모니터링해야 합니다.
   - **중요 경고 (골 그물 vs 보호 그물망)**: 골대 바로 뒤나 경기장 외곽에 설치된 '보호 그물망(안전망/철조망/fence)'과 실제 '골대 안쪽의 골망(Goal net)'을 절대 혼동하지 마십시오. 공이 골대 위나 옆으로 크게 벗어나 뒤쪽의 보호 그물망에 부딪힌 상황은 득점(goal)이나 유효슈팅이 아닌 '단순히 빗나간 플레이'로 엄격히 판정해야 합니다. 실제 골망을 흔들었는지 여부를 선수들의 위치와 함께 상세히 분석하세요.

2. **오디오 분석**:
   - 제공된 클립의 소리(오디오)를 귀 기울여 분석하세요.
   - **득점(goal) 상황**: 득점이 일어나는 즉시 선수들과 주변 사람들의 큰 환호성, 박수, 기쁨의 함성, 또는 심판의 득점 휘슬 소리가 동반됩니다.
   - **슈팅/선방/위협적인 상황**: 아슬아슬하게 비껴간 슛이나 선방이 일어날 때는 주변의 안타까운 탄식("아~", "어우~")이나 박수 소리, 격려의 목소리가 들릴 수 있습니다.
   - 화면이 멀어서 시각적으로 골 여부가 모호할 때, **오디오의 환호 소리와 기쁨의 외침 여부를 결정적인 증거로 삼아** 판정을 교정하십시오. (아무 반응이 없거나 탄식만 있다면 골이 아닐 확률이 매우 높습니다.)

[판단 기준]
다음 기준을 참고하여 해당 구간이 하이라이트일 가능성이 조금이라도 있다면 `highlight`를 `true`로 판단하십시오.
독단적으로 비하이라이트(`false`)로 배제하지 말고, 장면이 애매하거나 불확실하다면 신뢰도(`confidence` 수치)를 낮게(예: 0.40 ~ 0.65) 부여하여 사용자가 UI 임계 조절을 통해 판단할 수 있도록 지원하십시오.

1. **goal (득점)**: 공이 골라인을 넘어가 골이 되는 순간, 혹은 골 직후 골망을 흔들거나 골 세레머니를 하는 장면이 명확히 관찰될 때.
2. **shot (슈팅)**: 골대를 향한 슛(골 포스트를 벗어나거나 수비수에 막히는 등 득점이 되지 않은 상황).
3. **save (세이브)**: 골키퍼가 상대의 슈팅을 쳐내거나 잡아내는 명확한 선방 동작.
4. **attack (결정적 공격/공방)**: 골문 앞 혼전 상황, 위협적인 크로스, 코너킥/프리킥 세트피스 상황, 골대로 향하는 날카로운 패스나 돌파.
5. **other (일반 플레이/비하이라이트)**: 단순 패스 돌리기, 빌드업, 골킥, 스로인, 드롭볼, 킥오프 대기, 경기 중단, 경기와 무관한 단순 움직임. 이 경우에만 `highlight`를 `false`로 설정하십시오.

[출력 형식 및 작성 가이드]
- **reason**: 유튜브 하이라이트 영상의 챕터 제목 스타일로 작성하세요.
  - 15자 이내의 짧고 임팩트 있는 한국어 문구.
  - 장면의 사실적 내용을 구체적으로 묘사하세요. (예: 어떤 방향에서, 누가, 어떻게 처리했는지)
  - 확실하지 않은 득점은 득점으로 작성하지 마세요.
  - 예시: "오른쪽 돌파 후 크로스", "아쉬운 헤더 슈팅", "골키퍼 정면 세이브!", "역습 상황 차단", "코너킥 기회", "선제골! 강한 오른발 슛", "골문 앞 혼전 상황"

반드시 다음 JSON 형식으로만 응답해야 하며, 다른 부가 설명이나 백틱(```json 등)은 절대 포함하지 마십시오:
{"highlight": true/false, "type": "goal|shot|save|attack|other", "confidence": 0.0~1.0, "reason": "사실 기반의 유튜브 캡션 한 줄"}"""


def _is_transient(exc):
    """503/429/5xx 등 잠시 후 재시도하면 풀릴 수 있는 오류인지 판단."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).upper()
    return any(k in msg for k in ("UNAVAILABLE", "503", "429", "RESOURCE_EXHAUSTED",
                                  "OVERLOADED", "DEADLINE", "INTERNAL",
                                  "11001", "GETADDRINFO", "CONNECTION", "TIMEOUT"))


def classify_with_gemini(video_path, client):
    """비디오 클립 파일을 Gemini Files API에 업로드하여 판별하고 (판별 결과 dict, 토큰 사용량 dict) 반환.

    503(과부하)/429(rate limit) 같은 일시적 오류는 지수 백오프로 재시도한다.
    재시도까지 모두 실패하면 예외를 올려 호출부(_process)가 후보 단위로 격리한다.
    usage: {"in": 입력토큰, "out": 출력토큰}
    """
    from google.genai import types

    last_exc = None
    for attempt in range(VISION_RETRIES):
        video_file = None
        try:
            # 1. Files API를 통해 비디오 업로드
            video_file = client.files.upload(file=video_path)
            # 2. 비디오가 ACTIVE 상태가 될 때까지 폴링
            while video_file.state.name == "PROCESSING":
                time.sleep(1)
                video_file = client.files.get(name=video_file.name)
            if video_file.state.name == "FAILED":
                raise RuntimeError("Gemini Files API video processing failed")
                
            # 3. 비디오 분석 호출
            resp = client.models.generate_content(
                model=VISION_MODEL,
                contents=[video_file, types.Part.from_text(text=VISION_PROMPT)],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            break
        except Exception as e:
            last_exc = e
            if not _is_transient(e) or attempt == VISION_RETRIES - 1:
                raise
            # 지수 백오프 + 지터로 동시 재시도 몰림 방지
            delay = RETRY_BASE_SEC * (2 ** attempt) + random.uniform(0, 1)
            print(f"        (일시적 오류, {delay:.1f}s 후 재시도 "
                  f"{attempt+1}/{VISION_RETRIES-1}): {str(e)[:60]}")
            time.sleep(delay)
        finally:
            # 4. 업로드된 파일 삭제 (리소스 정리)
            if video_file:
                try:
                    client.files.delete(name=video_file.name)
                except Exception:
                    pass
    else:  # pragma: no cover
        raise last_exc

    um = getattr(resp, "usage_metadata", None)
    usage = {
        "in": int(getattr(um, "prompt_token_count", 0) or 0),
        "out": int(getattr(um, "candidates_token_count", 0) or 0),
    }
    txt = resp.text.strip().replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError:
        return ({"highlight": False, "type": "other", "confidence": 0.0,
                 "reason": f"parse_error: {txt[:80]}"}, usage)
    return _coerce_classification(parsed, txt), usage


def _coerce_classification(parsed, raw_txt=""):
    """모델 응답을 반드시 판별 dict 형태로 만든다.

    JSON으로는 유효하지만 형태가 다른 응답(주로 객체를 배열로 감싼 `[{...}]`)이
    가끔 온다. 이걸 그대로 호출부에 넘기면 `cand.update(res)`가
    ValueError로 터지면서 후보 하나 때문에 30분짜리 잡 전체가 실패한다.
    """
    if isinstance(parsed, dict):
        return parsed
    # 객체 하나를 배열로 감싼 경우: 첫 dict 원소를 채택
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return item
    return {"highlight": False, "type": "other", "confidence": 0.0,
            "reason": f"parse_error: 예상과 다른 형식 {str(parsed)[:60] or raw_txt[:60]}"}


def classify_all_parallel(cands, video, workdir, client, conf_auto, workers,
                          should_cancel=None, on_progress=None,
                          pre_sec=None, post_sec=None):
    """모든 후보에 대해 프레임 추출 + Gemini 판별을 병렬로 실행.

    후보 간 순서 의존성이 없으므로 ThreadPoolExecutor로 동시 처리한다.
    각 future가 완료될 때마다 결과를 즉시 출력해 진행 상황을 실시간으로 보여준다.

    should_cancel: 호출하면 True를 돌려주는 콜러블. True가 되면 아직 시작되지
        않은 작업을 취소하고 루프를 빠져나온다(진행 중인 호출만 마무리됨).
    반환: 사용량/진행 요약 dict
        {"in":입력토큰, "out":출력토큰, "calls":실제호출수,
         "classified":판별완료수, "total":전체후보수, "cancelled":bool}
    """
    # 완료 순서와 무관하게 원본 인덱스(idx)를 키로 결과를 모은다
    results = {}
    usage_total = {"in": 0, "out": 0, "calls": 0}

    def _process(idx, cand):
        # 이미 성공적으로 비전 판별이 완료된 후보라면 비전 호출을 건너뛴다.
        # 단, 에러가 발생했던 후보는 재시도할 수 있도록 에러 프리픽스가 없을 때만 스킵한다.
        is_error = any(cand.get("reason", "").startswith(prefix)
                       for prefix in ("api_error:", "clip_error:", "parse_error:"))
        if cand.get("confidence") is not None and cand.get("type") is not None and not is_error:
            return idx, cand, None

        # 비디오 클립 및 썸네일 추출도 후보 단위로 격리
        try:
            video_path = extract_video_clip_and_thumbnail(video, cand["peak"], workdir, idx,
                                                           pre_sec=pre_sec, post_sec=post_sec)
        except Exception as e:
            return idx, {"highlight": False, "type": "other", "confidence": 0.0,
                         "reason": f"clip_error: {str(e)[:80]}"}, None
        if not video_path or not video_path.exists():
            return idx, {"highlight": False, "type": "other",
                         "confidence": 0.0, "reason": "no_video_clip"}, None
        try:
            res, usage = classify_with_gemini(video_path, client)
            return idx, res, usage
        except Exception as e:
            # 한 후보가 재시도까지 실패해도 배치 전체를 죽이지 않고 격리한다.
            return idx, {"highlight": False, "type": "other", "confidence": 0.0,
                         "reason": f"api_error: {str(e)[:80]}"}, None

    cancelled = False
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process, i, c): i
                   for i, c in enumerate(cands)}
        for future in as_completed(futures):
            if should_cancel and should_cancel():
                cancelled = True
                for f in futures:
                    f.cancel()  # 아직 시작 안 된 작업만 취소됨
                print("        (사용자 취소 요청 — 진행 중인 호출만 마무리합니다)")
                break
            idx, res, usage = future.result()
            # 후보 하나의 응답 형태가 이상해도 잡 전체를 죽이지 않는다 (2차 방어선)
            if not isinstance(res, dict):
                res = _coerce_classification(res)
            results[idx] = res

            # 실시간으로 후보자 딕셔너리를 업데이트하여 상위 단계의 상태 파일 저장 시 반영되도록 함
            cands[idx].update(res)

            if usage:
                usage_total["in"] += usage["in"]
                usage_total["out"] += usage["out"]
                usage_total["calls"] += 1
            c = cands[idx]
            conf = float(res.get("confidence", 0))
            flag = "❌"
            if res.get("highlight") and conf >= conf_auto:
                flag = "✅ 채택"
            elif res.get("highlight") and conf >= CONF_MAYBE:
                flag = "⚠️  확인필요"
            print(f"        #{idx+1:2d} peak@{c['peak']:6.1f}s "
                  f"[{res.get('type','?'):6}] conf={conf:.2f} {flag}  "
                  f"{res.get('reason','')}")
            if on_progress:
                on_progress(len(results), len(cands))

    # 판별된 후보만 결과 반영 (취소된 경우 일부는 비전 결과 없음)
    for i, c in enumerate(cands):
        if i in results:
            c.update(results[i])

    usage_total.update({"classified": len(results), "total": len(cands),
                        "cancelled": cancelled})
    return usage_total


# ----------------------------------------------------------------------------
def _drawtext_filter(title, font_name):
    """우상단 타이틀 워터마크용 drawtext 필터 문자열 생성 (없으면 None).

    font_name: 작업폴더 기준 상대 폰트 파일명(콜론 없는 경로). Windows 드라이브
        콜론 이스케이프 문제를 피하려고 폰트를 작업폴더에 복사해 쓴다.
    """
    title = (title or "").strip()
    if not title:
        return None

    def esc(s):  # drawtext text 값 이스케이프
        return (s.replace("\\", "\\\\").replace(":", "\\:")
                 .replace("'", "\\'").replace("%", "\\%"))

    return (f"drawtext=fontfile={font_name}:text={esc(title)}:"
            f"fontcolor=white:fontsize={TITLE_FONTSIZE}:"
            f"x=w-tw-{TITLE_MARGIN}:y={TITLE_MARGIN}:"
            f"box=1:boxcolor=black@0.35:boxborderw=10:"
            f"shadowcolor=black@0.5:shadowx=1:shadowy=1")


def get_merged_timeline(selected_cands, pre_sec, post_sec):
    """선택된 후보 구간들을 분석하여 겹치는 구간을 하나로 병합하고, 
    최종 영상 내에서의 각 후보의 정확한 상대 peak 타임스탬프(초)를 계산한다.
    
    반환:
      - merged_clips: [{"start": float, "end": float, "cands": [dict, ...]}]
      - cand_timestamps: {id(cand_dict): float}  # candidate 객체 레퍼런스 id 기준 타임스탬프
    """
    if not selected_cands:
        return [], {}

    # 시작 시간 기준으로 정렬
    sorted_cands = sorted(selected_cands, key=lambda c: float(c["peak"]) - pre_sec)
    
    merged_clips = []
    for c in sorted_cands:
        peak = float(c["peak"])
        start = max(0.0, peak - pre_sec)
        end = peak + post_sec
        
        if not merged_clips:
            merged_clips.append({"start": start, "end": end, "cands": [c]})
        else:
            prev = merged_clips[-1]
            if start < prev["end"]:
                prev["end"] = max(prev["end"], end)
                prev["cands"].append(c)
            else:
                merged_clips.append({"start": start, "end": end, "cands": [c]})
                
    cand_timestamps = {}
    cumulative_time = 0.0
    for clip in merged_clips:
        clip_start = clip["start"]
        for c in clip["cands"]:
            peak = float(c["peak"])
            # 각 candidate 구간의 시작 지점(peak - pre_sec)을 마커로 사용한다. (음수 방지 클램핑)
            cand_start = max(0.0, peak - pre_sec)
            relative_start = cand_start - clip_start
            cand_timestamps[id(c)] = cumulative_time + relative_start
            
        clip_duration = clip["end"] - clip["start"]
        cumulative_time += clip_duration
        
    return merged_clips, cand_timestamps


def build_output(video, segments, out_path, workdir, title=None,
                 preset=None, crf=None, copy_mode=False, max_height=None,
                 on_progress=None, should_cancel=None, pre_sec=None, post_sec=None):
    """선택된 구간들을 잘라 이어붙여 최종 영상 생성.

    title: 비어있지 않으면 영상 우상단에 작은 타이틀 워터마크를 입힌다.
    preset/crf: 인코딩 속도/용량 (기본 ENCODE_PRESET/ENCODE_CRF).
    copy_mode: True면 stream-copy(재인코딩 없음)로 가장 빠르게. 단 키프레임
        경계로 시작점이 약간 어긋날 수 있고 **타이틀/다운스케일이 적용 안 됨**.
    max_height: 다운스케일 상한 (기본 MAX_HEIGHT). copy_mode면 무시.
    on_progress: on_progress(done, total) 콜백 (진행 표시용).
    should_cancel: 호출하면 True/False를 돌려주는 콜러블. True가 되면 다음
        클립 처리 전에 RuntimeError를 발생시켜 중단한다(이미 시작한 ffmpeg
        호출은 끝까지 완료됨).
    """
    preset = preset or ENCODE_PRESET
    crf = ENCODE_CRF if crf is None else crf
    max_height = MAX_HEIGHT if max_height is None else max_height

    vf = None
    if not copy_mode:
        title = title if title is not None else TITLE_TEXT
        drawtext = None
        if (title or "").strip():
            # 폰트를 작업폴더에 복사해 콜론 없는 상대경로(cwd 기준)로 참조한다.
            try:
                shutil.copyfile(TITLE_FONT, workdir / "title_font.ttf")
                drawtext = _drawtext_filter(title, "title_font.ttf")
            except Exception as e:
                sys.stderr.write(f"타이틀 폰트 로드 실패, 타이틀 생략: {e}\n")
        # 비디오 필터 체인: 다운스케일(상한 초과 시) → 타이틀 순서
        filters = []
        if max_height:
            filters.append(f"scale=-2:'min(ih,{max_height})'")
        if drawtext:
            filters.append(drawtext)
        vf = ",".join(filters) if filters else None

    _pre  = PRE_SEC  if pre_sec  is None else pre_sec
    _post = POST_SEC if post_sec is None else post_sec

    # 겹치는 구간 병합
    merged_clips, _ = get_merged_timeline(segments, _pre, _post)
    total = len(merged_clips)

    def _encode_clip(i, clip_info):
        start = clip_info["start"]
        dur = clip_info["end"] - clip_info["start"]
        clip = workdir / f"clip{i:03d}.mp4"
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", video,
               "-t", f"{dur:.2f}"]
        if copy_mode:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-c:v", "libx264", "-preset", preset,
                    "-crf", str(crf), "-c:a", "aac",
                    "-avoid_negative_ts", "make_zero"]
        cmd += [str(clip), "-loglevel", "error"]
        # cwd=workdir: drawtext가 title_font.ttf 를 상대경로로 찾도록
        run(cmd, cwd=str(workdir))
        return clip

    # 클립별 재인코딩은 서로 독립적이므로 병렬 처리한다. ffmpeg는 별도 프로세스라
    # GIL과 무관하게 실제로 동시에 돈다. concat 순서는 완료 순서가 아니라
    # clip{i:03d}.mp4 파일명(i)으로 보장되므로 완료 순서는 상관없다.
    clip_paths = [None] * total
    done = 0
    cancelled = False
    workers = max(1, min(BUILD_CLIP_WORKERS, total or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_encode_clip, i, clip_info): i
                   for i, clip_info in enumerate(merged_clips)}
        for future in as_completed(futures):
            i = futures[future]
            clip_paths[i] = future.result()
            done += 1
            if on_progress:
                on_progress(done, total)
            if should_cancel and should_cancel():
                cancelled = True
                for f in futures:
                    f.cancel()  # 아직 시작 안 된 것만 취소됨
                break

    if cancelled or (should_cancel and should_cancel()):
        raise RuntimeError("취소됨")

    listfile = workdir / "concat.txt"
    # concat 데모서는 백슬래시를 이스케이프 문자로 해석하므로 Windows에서도
    # forward slash 경로(as_posix)를 써야 클립을 정상적으로 연다.
    listfile.write_text(
        "".join(f"file '{c.as_posix()}'\n" for c in clip_paths),
        encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(out_path), "-loglevel", "error"])


# ----------------------------------------------------------------------------
def effective_conf(c):
    """팬 신호 가산치를 반영한 보정 신뢰도. 팬 주석이 없으면 원래 confidence 그대로."""
    conf = float(c.get("confidence", 0))
    return min(1.0, conf + float(c.get("pan_bonus") or 0.0))


def select_segments(cands, conf_auto, use_vision):
    """판별된 후보 리스트에서 채택/확인필요 구간을 분류해 반환.

    cands: 각 dict에 최소 'peak'가 있고, 비전 사용 시 'highlight','confidence' 포함.
    임계 비교는 보정 신뢰도(effective_conf: confidence + pan_bonus)로 한다.
    반환: (selected, maybe)
    """
    if not use_vision:
        return list(cands), []
    selected, maybe = [], []
    for c in cands:
        conf = effective_conf(c)
        if c.get("highlight") and conf >= conf_auto:
            selected.append(c)
        elif c.get("highlight") and conf >= CONF_MAYBE:
            maybe.append(c)
    return selected, maybe


def save_results(path, video, dur, cands, use_vision):
    """후보/판별 결과를 JSON으로 저장 (재선택 시 비전 재호출 방지용)."""
    payload = {
        "video": os.path.abspath(video),
        "duration": dur,
        "vision_used": use_vision,
        "params": {
            "PRE_SEC": PRE_SEC, "POST_SEC": POST_SEC,
            "SPIKE_PERCENTILE": SPIKE_PERCENTILE, "SPIKE_MIN_DB": SPIKE_MIN_DB,
            "VISION_MODEL": VISION_MODEL if use_vision else None,
        },
        "candidates": cands,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"      결과 저장: {path}")

def get_audio_signal(video, workdir, sensitivity="normal"):
    """영상에서 오디오를 추출하고 전체 시계열 볼륨(baseline 대비 delta) 데이터를 계산하여 반환."""
    wav = workdir / "audio.wav"
    if not wav.exists():
        run(["ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
             "-f", "wav", str(wav), "-loglevel", "error"])

    sr, data = wavfile.read(str(wav))
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float64)
    data /= (np.max(np.abs(data)) + 1e-9)

    win = int(sr * WIN_SEC)
    hop = int(sr * HOP_SEC)
    rms, times = [], []
    for start in range(0, len(data) - win, hop):
        seg = data[start:start + win]
        rms.append(np.sqrt(np.mean(seg ** 2)))
        times.append(start / sr)
    rms = np.array(rms)
    times = np.array(times)
    db = 20 * np.log10(rms + 1e-9)

    bwin = int(BASELINE_SEC / HOP_SEC)
    baseline = np.array([np.median(db[max(0, i - bwin):i + bwin + 1])
                         for i in range(len(db))])
    delta = db - baseline

    sp = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS["normal"])
    percentile = sp["percentile"]
    min_db = sp["min_db"]
    thr = max(np.percentile(delta, percentile), min_db)

    # 차트 가독성 및 UI 렌더링 성능 최적화를 위한 다운샘플링 (최대 2000포인트)
    max_points = 2000
    n = len(times)
    if n > max_points:
        step = int(np.ceil(n / max_points))
        times = times[::step]
        delta = delta[::step]

    return {
        "times": [round(float(t), 2) for t in times],
        "delta": [round(float(d), 2) for d in delta],
        "threshold": float(thr)
    }


def main():
    ap = argparse.ArgumentParser(description="축구 하이라이트 자동 추출")
    ap.add_argument("video", nargs="?", default=None,
                    help="입력 영상 경로 (--from-json 사용 시 생략 가능)")
    ap.add_argument("-o", "--output", default="highlights.mp4", help="출력 파일")
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"),
                    help="Gemini API 키 (또는 GEMINI_API_KEY 환경변수)")
    ap.add_argument("--no-vision", action="store_true",
                    help="비전 판별 끄고 오디오 후보만 사용")
    ap.add_argument("--no-pan", action="store_true",
                    help="카메라 팬 궤적 3차 신호 끄기 (기본: 켜짐, 로컬 계산·무료)")
    ap.add_argument("--dry-run", action="store_true",
                    help="후보/판별 결과만 출력하고 영상은 안 만듦")
    ap.add_argument("--conf", type=float, default=CONF_AUTO,
                    help=f"자동 채택 신뢰도 임계 (기본 {CONF_AUTO})")
    ap.add_argument("--workers", type=int, default=VISION_WORKERS,
                    help=f"비전 호출 병렬 수 (기본 {VISION_WORKERS}, 범위 1~8 권장)")
    ap.add_argument("--title", default=TITLE_TEXT,
                    help="영상 우상단에 넣을 타이틀 텍스트 (예: '한울타리 FC 경기영상')")
    ap.add_argument("--sensitivity", choices=list(SENSITIVITY_PRESETS),
                    default="normal",
                    help="후보 검출 민감도: more(많이)/normal(보통)/strict(엄선)")
    ap.add_argument("--quality", choices=list(QUALITY_PRESETS), default="balanced",
                    help="출력 품질: size/balanced/quality/copy")
    ap.add_argument("--save-json", metavar="PATH", default=None,
                    help="후보/비전 판별 결과를 JSON으로 저장 (재선택용)")
    ap.add_argument("--from-json", metavar="PATH", default=None,
                    help="저장된 JSON에서 결과를 읽어 비전 재호출 없이 재선택 "
                         "(--conf 만 바꿔 영상 재생성 가능)")
    args = ap.parse_args()

    video = os.path.abspath(args.video) if args.video else None

    workdir = Path(tempfile.mkdtemp(prefix="highlight_"))
    try:
        # ===== 재선택 모드: 저장된 JSON에서 읽어 비전 재호출 없이 재선택 =====
        if args.from_json:
            with open(args.from_json, encoding="utf-8") as f:
                data = json.load(f)
            video = video or data["video"]
            if not os.path.exists(video):
                sys.exit(f"원본 영상을 찾을 수 없습니다: {video}\n"
                         f"  (--from-json 사용 시 첫 인자로 영상 경로를 넘기면 덮어씁니다)")
            cands = data["candidates"]
            use_vision = data.get("vision_used", True)
            print(f"[재선택] {args.from_json} 에서 {len(cands)}개 후보 로드 "
                  f"(비전 재호출 없음)")
            selected, maybe = select_segments(cands, args.conf, use_vision)
            for c in cands:
                conf = effective_conf(c)
                flag = "✅ 채택" if c in selected else (
                       "⚠️  확인필요" if c in maybe else "❌")
                boost = f" (팬 +{c['pan_bonus']:.2f})" if c.get("pan_bonus") else ""
                print(f"        peak@{c['peak']:6.1f}s "
                      f"[{c.get('type','?'):6}] conf={conf:.2f}{boost} {flag}")
            print(f"\n  자동 채택: {len(selected)}개" +
                  (f" / 확인필요: {len(maybe)}개" if use_vision else ""))

            if args.dry_run:
                print("\n[dry-run] 영상 생성 생략.")
                return
            if not selected:
                print("\n채택된 구간이 없습니다. --conf 를 낮춰보세요.")
                return
            print(f"[생성] 클립 병합 -> {args.output}")
            selected.sort(key=lambda x: x["peak"])
            qk = QUALITY_PRESETS[args.quality]
            build_output(video, selected, os.path.abspath(args.output), workdir,
                         title=args.title, preset=qk.get("preset"),
                         crf=qk.get("crf"), copy_mode=qk.get("copy", False))
            print(f"      완료: {args.output}")
            return

        # ===== 일반 모드 =====
        if not video or not os.path.exists(video):
            sys.exit(f"파일을 찾을 수 없습니다: {video}")

        print("[1/4] 영상 길이 확인...")
        dur = probe_duration(video)
        print(f"      {dur:.1f}초")

        print(f"[2/4] 오디오 볼륨 급증 후보 검출... (민감도: {args.sensitivity})")
        sp = SENSITIVITY_PRESETS[args.sensitivity]
        cands = detect_spikes(video, workdir, percentile=sp["percentile"],
                              min_db=sp["min_db"])
        print(f"      {len(cands)}개 후보:")
        for i, c in enumerate(cands):
            print(f"        #{i+1:2d}  peak@{c['peak']:6.1f}s  +{c['delta_db']:4.1f}dB")

        # 팬 궤적 3차 신호 — 실패해도 파이프라인은 계속 (후보 단위 격리와 같은 원칙)
        if not args.no_pan and cands:
            print("[2.5/4] 카메라 팬 궤적 분석 (로컬)...")
            try:
                series = pan_signal.compute_pan_series(video, workdir, run=run)
                boosted = pan_signal.annotate_candidates(cands, series)
                if not series["reliable"]:
                    print(f"      팬 이동폭 부족({series['range_px']:.0f}px) 또는 "
                          f"추정 신뢰도 낮음 — 신호 미적용")
                else:
                    print(f"      팬 이동폭 {series['range_px']:.0f}px, "
                          f"지지 후보 {boosted}개 (신뢰도 +{pan_signal.PAN_BONUS:.2f} 보정)")
            except Exception as e:
                print(f"      팬 분석 실패 — 신호 없이 진행: {str(e)[:80]}")

        use_vision = not args.no_vision
        client = None
        if use_vision:
            if not args.api_key:
                sys.exit("Gemini API 키가 없습니다. --api-key 또는 GEMINI_API_KEY 설정. "
                         "(또는 --no-vision)")
            try:
                from google import genai
            except ImportError:
                sys.exit("google-genai 미설치: pip install google-genai  (또는 --no-vision)")
            client = genai.Client(api_key=args.api_key)

        if use_vision:
            workers = max(1, min(args.workers, 8))
            print(f"[3/4] Gemini 비전 판별 ({VISION_MODEL}, 병렬 {workers}개)...")
            usage = classify_all_parallel(cands, video, workdir, client,
                                          args.conf, workers)
            cost = (usage["in"] / 1e6 * PRICE_IN_PER_M +
                    usage["out"] / 1e6 * PRICE_OUT_PER_M)
            print(f"      토큰: 입력 {usage['in']:,} / 출력 {usage['out']:,} "
                  f"({usage['calls']}회 호출) ≈ ${cost:.4f}")
        else:
            print("[3/4] 비전 판별 생략 — 오디오 후보 전체 사용")

        # 결과 저장 (재선택용) — 비전 호출이 있었다면 특히 유용
        if args.save_json:
            save_results(args.save_json, video, dur, cands, use_vision)

        selected, maybe = select_segments(cands, args.conf, use_vision)

        print(f"\n  자동 채택: {len(selected)}개" +
              (f" / 확인필요: {len(maybe)}개" if use_vision else ""))
        if maybe:
            print("  ⚠️ 확인필요 구간 (필요시 --conf 낮춰서 포함):")
            for c in maybe:
                pan = c.get("pan") or {}
                pan_txt = (f" [카메라: {pan_signal.STATE_LABELS.get(pan.get('state'), '?')}]"
                           if pan else "")
                print(f"     peak@{c['peak']:.1f}s conf={effective_conf(c):.2f}{pan_txt} "
                      f"{c.get('reason','')}")

        if args.dry_run:
            print("\n[dry-run] 영상 생성 생략." +
                  (f" 결과는 {args.save_json} 에 저장됨 → "
                   f"--from-json 으로 임계만 바꿔 재생성 가능." if args.save_json else ""))
            return
        if not selected:
            print("\n채택된 구간이 없습니다. --conf 를 낮추거나 --no-vision 으로 시도하세요.")
            return

        print(f"[4/4] 클립 생성 및 병합 -> {args.output} (품질: {args.quality})")
        selected.sort(key=lambda x: x["peak"])
        qk = QUALITY_PRESETS[args.quality]
        build_output(video, selected, os.path.abspath(args.output), workdir,
                     title=args.title, preset=qk.get("preset"),
                     crf=qk.get("crf"), copy_mode=qk.get("copy", False))
        print(f"      완료: {args.output}")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
