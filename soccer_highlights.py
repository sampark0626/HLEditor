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
import subprocess
import sys
import tempfile
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from scipy.io import wavfile


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
MAX_FRAMES     = 8      # 후보당 Gemini에 보낼 최대 프레임 수
VISION_MODEL   = "gemini-2.5-flash"   # 비용/속도 균형. 정확도 더 원하면 gemini-2.5-pro
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

# --- Gemini 2.5 Flash 단가 (USD per 1M tokens, 2025 기준 추정) ---
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
def detect_spikes(video, workdir, percentile=None, min_db=None):
    """오디오를 추출하고 볼륨 급증 후보 구간을 반환.

    percentile/min_db: 지정 시 모듈 기본값(SPIKE_PERCENTILE/SPIKE_MIN_DB)을
        덮어쓴다. 값이 클수록 후보가 줄어든다(엄선).
    """
    percentile = SPIKE_PERCENTILE if percentile is None else percentile
    min_db = SPIKE_MIN_DB if min_db is None else min_db
    wav = workdir / "audio.wav"
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
        # 구간 겹침량 = (prev_peak + POST_SEC) - (curr_peak - PRE_SEC)
        overlap = (prev[2] + POST_SEC) - (s[2] - PRE_SEC)
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
def extract_frames(video, peak, workdir, idx):
    """후보 구간(peak 앞뒤)에서 프레임을 추출해 경로 리스트 반환."""
    seg_start = max(0.0, peak - PRE_SEC)
    seg_len = PRE_SEC + POST_SEC
    out_pat = workdir / f"cand{idx}_%03d.jpg"
    run(["ffmpeg", "-y", "-ss", f"{seg_start:.2f}", "-i", video,
         "-t", f"{seg_len:.2f}", "-vf", f"fps=1/{FRAME_INTERVAL},scale=640:-1",
         "-q:v", "4", str(out_pat), "-loglevel", "error"])
    frames = sorted(workdir.glob(f"cand{idx}_*.jpg"))
    # 너무 많으면 균등 샘플링
    if len(frames) > MAX_FRAMES:
        sel = np.linspace(0, len(frames) - 1, MAX_FRAMES).astype(int)
        frames = [frames[i] for i in sel]
    return frames


# ----------------------------------------------------------------------------
VISION_PROMPT = """당신은 아마추어(동호회) 축구 경기 영상의 하이라이트를 분류하는 분석가입니다.
아래 이미지들은 한 후보 구간에서 0.5초 간격으로 추출한 연속 프레임입니다.
카메라는 고정된 광각 풀샷이라 선수와 공이 작게 보일 수 있습니다.

이 구간이 하이라이트(득점, 슈팅, 결정적 세이브, 빠른 역습, 골문 앞 밀집 공방 등)인지 판단하세요.
단순한 패스 돌리기, 경기 중단, 킥오프 대기, 선수 이동만 있는 장면은 하이라이트가 아닙니다.

다음 JSON 형식으로만 답하세요. 다른 텍스트는 절대 포함하지 마세요:
{"highlight": true/false, "type": "goal|shot|save|attack|other", "confidence": 0.0~1.0, "reason": "한 줄 근거"}"""


def _is_transient(exc):
    """503/429/5xx 등 잠시 후 재시도하면 풀릴 수 있는 오류인지 판단."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).upper()
    return any(k in msg for k in ("UNAVAILABLE", "503", "429", "RESOURCE_EXHAUSTED",
                                  "OVERLOADED", "DEADLINE", "INTERNAL"))


def classify_with_gemini(frames, client):
    """프레임 묶음을 Gemini에 보내 (판별 결과 dict, 토큰 사용량 dict) 반환.

    503(과부하)/429(rate limit) 같은 일시적 오류는 지수 백오프로 재시도한다.
    재시도까지 모두 실패하면 예외를 올려 호출부(_process)가 후보 단위로 격리한다.
    usage: {"in": 입력토큰, "out": 출력토큰}
    """
    from google.genai import types

    parts = [types.Part.from_text(text=VISION_PROMPT)]
    for fp in frames:
        parts.append(types.Part.from_bytes(
            data=fp.read_bytes(), mime_type="image/jpeg"))

    last_exc = None
    for attempt in range(VISION_RETRIES):
        try:
            resp = client.models.generate_content(
                model=VISION_MODEL,
                contents=[types.Content(role="user", parts=parts)],
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
    else:  # pragma: no cover
        raise last_exc

    um = getattr(resp, "usage_metadata", None)
    usage = {
        "in": int(getattr(um, "prompt_token_count", 0) or 0),
        "out": int(getattr(um, "candidates_token_count", 0) or 0),
    }
    txt = resp.text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(txt), usage
    except json.JSONDecodeError:
        return ({"highlight": False, "type": "other", "confidence": 0.0,
                 "reason": f"parse_error: {txt[:80]}"}, usage)


def classify_all_parallel(cands, video, workdir, client, conf_auto, workers,
                          should_cancel=None, on_progress=None):
    """모든 후보에 대해 프레임 추출 + Gemini 판별을 병렬로 실행.

    후보 간 순서 의존성이 없으므로 ThreadPoolExecutor로 동시 처리한다.
    각 future가 완료될 때마다 결과를 즉시 출력해 진행 상황을 실시간으로 보여준다.

    should_cancel: 호출하면 True를 돌려주는 콜러블. True가 되면 아직 시작되지
        않은 작업을 취소하고 루프를 빠져나온다(진행 중인 호출은 마무리됨).
    반환: 사용량/진행 요약 dict
        {"in":입력토큰, "out":출력토큰, "calls":실제호출수,
         "classified":판별완료수, "total":전체후보수, "cancelled":bool}
    """
    # 완료 순서와 무관하게 원본 인덱스(idx)를 키로 결과를 모은다
    results = {}
    usage_total = {"in": 0, "out": 0, "calls": 0}

    def _process(idx, cand):
        # 프레임 추출(ffmpeg)도 후보 단위로 격리: 한 후보의 spawn 실패가
        # 배치 전체를 죽이지 않도록 한다.
        try:
            frames = extract_frames(video, cand["peak"], workdir, idx)
        except Exception as e:
            return idx, {"highlight": False, "type": "other", "confidence": 0.0,
                         "reason": f"frame_error: {str(e)[:80]}"}, None
        if not frames:
            return idx, {"highlight": False, "type": "other",
                         "confidence": 0.0, "reason": "no_frames"}, None
        try:
            res, usage = classify_with_gemini(frames, client)
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
            results[idx] = res
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


def build_output(video, segments, out_path, workdir, title=None,
                 preset=None, crf=None, copy_mode=False, max_height=None,
                 on_progress=None):
    """선택된 구간들을 잘라 이어붙여 최종 영상 생성.

    title: 비어있지 않으면 영상 우상단에 작은 타이틀 워터마크를 입힌다.
    preset/crf: 인코딩 속도/용량 (기본 ENCODE_PRESET/ENCODE_CRF).
    copy_mode: True면 stream-copy(재인코딩 없음)로 가장 빠르게. 단 키프레임
        경계로 시작점이 약간 어긋날 수 있고 **타이틀/다운스케일이 적용 안 됨**.
    max_height: 다운스케일 상한 (기본 MAX_HEIGHT). copy_mode면 무시.
    on_progress: on_progress(done, total) 콜백 (진행 표시용).
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

    total = len(segments)
    clip_paths = []
    for i, seg in enumerate(segments):
        start = max(0.0, seg["peak"] - PRE_SEC)
        dur = PRE_SEC + POST_SEC
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
        clip_paths.append(clip)
        if on_progress:
            on_progress(i + 1, total)

    listfile = workdir / "concat.txt"
    # concat 데모서는 백슬래시를 이스케이프 문자로 해석하므로 Windows에서도
    # forward slash 경로(as_posix)를 써야 클립을 정상적으로 연다.
    listfile.write_text(
        "".join(f"file '{c.as_posix()}'\n" for c in clip_paths),
        encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(out_path), "-loglevel", "error"])


# ----------------------------------------------------------------------------
def select_segments(cands, conf_auto, use_vision):
    """판별된 후보 리스트에서 채택/확인필요 구간을 분류해 반환.

    cands: 각 dict에 최소 'peak'가 있고, 비전 사용 시 'highlight','confidence' 포함.
    반환: (selected, maybe)
    """
    if not use_vision:
        return list(cands), []
    selected, maybe = [], []
    for c in cands:
        conf = float(c.get("confidence", 0))
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


def main():
    ap = argparse.ArgumentParser(description="축구 하이라이트 자동 추출")
    ap.add_argument("video", nargs="?", default=None,
                    help="입력 영상 경로 (--from-json 사용 시 생략 가능)")
    ap.add_argument("-o", "--output", default="highlights.mp4", help="출력 파일")
    ap.add_argument("--api-key", default=os.environ.get("GEMINI_API_KEY"),
                    help="Gemini API 키 (또는 GEMINI_API_KEY 환경변수)")
    ap.add_argument("--no-vision", action="store_true",
                    help="비전 판별 끄고 오디오 후보만 사용")
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
                conf = float(c.get("confidence", 0))
                flag = "✅ 채택" if c in selected else (
                       "⚠️  확인필요" if c in maybe else "❌")
                print(f"        peak@{c['peak']:6.1f}s "
                      f"[{c.get('type','?'):6}] conf={conf:.2f} {flag}")
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

        print(f"[1/4] 영상 길이 확인...")
        dur = probe_duration(video)
        print(f"      {dur:.1f}초")

        print(f"[2/4] 오디오 볼륨 급증 후보 검출... (민감도: {args.sensitivity})")
        sp = SENSITIVITY_PRESETS[args.sensitivity]
        cands = detect_spikes(video, workdir, percentile=sp["percentile"],
                              min_db=sp["min_db"])
        print(f"      {len(cands)}개 후보:")
        for i, c in enumerate(cands):
            print(f"        #{i+1:2d}  peak@{c['peak']:6.1f}s  +{c['delta_db']:4.1f}dB")

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
            print(f"[3/4] 비전 판별 생략 — 오디오 후보 전체 사용")

        # 결과 저장 (재선택용) — 비전 호출이 있었다면 특히 유용
        if args.save_json:
            save_results(args.save_json, video, dur, cands, use_vision)

        selected, maybe = select_segments(cands, args.conf, use_vision)

        print(f"\n  자동 채택: {len(selected)}개" +
              (f" / 확인필요: {len(maybe)}개" if use_vision else ""))
        if maybe:
            print("  ⚠️ 확인필요 구간 (필요시 --conf 낮춰서 포함):")
            for c in maybe:
                print(f"     peak@{c['peak']:.1f}s conf={c.get('confidence',0):.2f} "
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
