"""후보 분석 공용부 — 팬 궤적 분석과 Gemini 판별을 동시에 돌린다.

팬 분석(ffmpeg로 전체 영상 디코드, CPU)과 Gemini 판별(네트워크 대기 위주)은 서로
독립이라 병렬로 돌리면 경기당 몇 분이 준다(9/6 PC 실측: 팬 5~7분, 판별 4~8분).
팬 가산치(pan_bonus)는 둘 다 끝난 뒤에 기록한다 — 후보 dict를 두 스레드가 동시에
고치지 않게 하기 위함이다. 팬 분석이 실패해도 신호 없이 계속한다(jobs._process와 같은 원칙).

inbox_runner와 run_match(수동 배치)가 같이 쓴다.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def analyze_candidates(video, workdir, cands: list, *, gemini_key: str | None = None,
                       workers: int | None = None, log=print) -> dict:
    """후보에 Gemini 판별 결과와 팬 보정을 채워 넣고 요약을 돌려준다.

    반환: {"vision_used": bool, "usage": dict|None, "pan": dict|None}
      usage: classify_all_parallel 사용량 + cost_usd
      pan:   {"range_px", "reliable", "boosted"} 또는 {"error": ...}
    """
    import pan_signal
    import soccer_highlights as sh

    info = {"vision_used": False, "usage": None, "pan": None}
    if not cands:
        return info

    workdir = Path(workdir)
    pan_dir = workdir / "pan"
    vision_dir = workdir / "vision"
    pan_dir.mkdir(parents=True, exist_ok=True)
    vision_dir.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pan_future = pool.submit(pan_signal.compute_pan_series, str(video), pan_dir, sh.run)

        if gemini_key:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            usage = sh.classify_all_parallel(cands, str(video), vision_dir, client,
                                             sh.CONF_AUTO, workers or sh.VISION_WORKERS)
            usage["cost_usd"] = round(usage["in"] / 1e6 * sh.PRICE_IN_PER_M
                                      + usage["out"] / 1e6 * sh.PRICE_OUT_PER_M, 4)
            info["vision_used"] = True
            info["usage"] = usage

        try:
            series = pan_future.result()
            boosted = pan_signal.annotate_candidates(cands, series)
            info["pan"] = {"range_px": round(float(series["range_px"]), 1),
                           "reliable": bool(series["reliable"]), "boosted": boosted}
        except Exception as e:
            log(f"팬 궤적 분석 실패 — 신호 없이 진행: {e!r}")
            info["pan"] = {"error": repr(e)[:200]}
    return info
