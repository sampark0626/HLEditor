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
import subprocess
import sys
import tempfile
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

PRE_SEC        = 8.0    # 급증(peak) 시점 기준 앞으로 포함할 길이 (초)
POST_SEC       = 5.0    # 급증 시점 기준 뒤로 포함할 길이 (초)

FRAME_INTERVAL = 0.5    # 비전 판별용 프레임 추출 간격 (초)
MAX_FRAMES     = 8      # 후보당 Gemini에 보낼 최대 프레임 수
VISION_MODEL   = "gemini-2.5-flash"   # 비용/속도 균형. 정확도 더 원하면 gemini-2.5-pro
VISION_WORKERS = 6      # 비전 호출 동시 병렬 수 (Gemini rate limit 고려해 4~8 권장)
CONF_AUTO      = 0.70   # 이 신뢰도 이상이면 자동 채택
CONF_MAYBE     = 0.40   # 이 값 이상 ~ AUTO 미만이면 '확인 필요'로 분류


# ----------------------------------------------------------------------------
def run(cmd):
    """ffmpeg/ffprobe 실행 헬퍼."""
    # encoding/errors 명시: Windows 한글 로케일(cp949)에서 ffmpeg의 비-cp949
    # stderr 출력을 디코딩하다 UnicodeDecodeError로 죽는 것을 방지한다.
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        raise RuntimeError(f"command failed: {' '.join(cmd[:3])}...")
    return r.stdout


def probe_duration(video):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", video])
    return float(out.strip())


# ----------------------------------------------------------------------------
def detect_spikes(video, workdir):
    """오디오를 추출하고 볼륨 급증 후보 구간을 반환."""
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

    thr = max(np.percentile(delta, SPIKE_PERCENTILE), SPIKE_MIN_DB)
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

    # 인접 spike 병합
    merged = []
    for s in spikes:
        if merged and s[0] - merged[-1][1] < MERGE_GAP_SEC:
            p = merged[-1]
            keep_peak = p[2] if p[3] >= s[3] else s[2]
            merged[-1] = [p[0], s[1], keep_peak, max(p[3], s[3])]
        else:
            merged.append(s)

    return [{"start": m[0], "end": m[1], "peak": m[2], "delta_db": m[3]}
            for m in merged]


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


def classify_with_gemini(frames, client):
    """프레임 묶음을 Gemini에 보내 판별 결과(dict) 반환."""
    from google.genai import types

    parts = [types.Part.from_text(text=VISION_PROMPT)]
    for fp in frames:
        parts.append(types.Part.from_bytes(
            data=fp.read_bytes(), mime_type="image/jpeg"))

    resp = client.models.generate_content(
        model=VISION_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    txt = resp.text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {"highlight": False, "type": "other", "confidence": 0.0,
                "reason": f"parse_error: {txt[:80]}"}


def classify_all_parallel(cands, video, workdir, client, conf_auto, workers):
    """모든 후보에 대해 프레임 추출 + Gemini 판별을 병렬로 실행.

    후보 간 순서 의존성이 없으므로 ThreadPoolExecutor로 동시 처리한다.
    각 future가 완료될 때마다 결과를 즉시 출력해 진행 상황을 실시간으로 보여준다.
    """
    # 완료 순서와 무관하게 원본 인덱스(idx)를 키로 결과를 모은다
    results = {}

    def _process(idx, cand):
        frames = extract_frames(video, cand["peak"], workdir, idx)
        if not frames:
            return idx, {"highlight": False, "type": "other",
                         "confidence": 0.0, "reason": "no_frames"}
        return idx, classify_with_gemini(frames, client)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_process, i, c): i
                   for i, c in enumerate(cands)}
        for future in as_completed(futures):
            idx, res = future.result()
            results[idx] = res
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

    # 원본 순서대로 결과를 cands에 반영
    for i, c in enumerate(cands):
        c.update(results[i])


# ----------------------------------------------------------------------------
def build_output(video, segments, out_path, workdir):
    """선택된 구간들을 잘라 이어붙여 최종 영상 생성."""
    clip_paths = []
    for i, seg in enumerate(segments):
        start = max(0.0, seg["peak"] - PRE_SEC)
        dur = PRE_SEC + POST_SEC
        clip = workdir / f"clip{i:03d}.mp4"
        run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", video,
             "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-c:a", "aac", "-avoid_negative_ts", "make_zero",
             str(clip), "-loglevel", "error"])
        clip_paths.append(clip)

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
            build_output(video, selected, os.path.abspath(args.output), workdir)
            print(f"      완료: {args.output}")
            return

        # ===== 일반 모드 =====
        if not video or not os.path.exists(video):
            sys.exit(f"파일을 찾을 수 없습니다: {video}")

        print(f"[1/4] 영상 길이 확인...")
        dur = probe_duration(video)
        print(f"      {dur:.1f}초")

        print(f"[2/4] 오디오 볼륨 급증 후보 검출...")
        cands = detect_spikes(video, workdir)
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
            classify_all_parallel(cands, video, workdir, client, args.conf, workers)
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

        print(f"[4/4] 클립 생성 및 병합 -> {args.output}")
        selected.sort(key=lambda x: x["peak"])
        build_output(video, selected, os.path.abspath(args.output), workdir)
        print(f"      완료: {args.output}")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
