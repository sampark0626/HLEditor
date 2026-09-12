#!/usr/bin/env python3
"""
HLEditor Kaggle 러너 (v0 — 미검증 초안)
=========================================

로컬 서버 없이 Kaggle 커널(GPU) 하나로 "원본 영상 -> 하이라이트 추출
-> (하이라이트 구간만, 또는 원본 전체) Dot Play 2D 변환 -> 합성"까지 끝낸다.

HLEditor 레포(soccer_highlights.py, dotplay/)를 그대로 git clone해 재사용한다
(Flask 의존성이 없는 순수 라이브러리라 그대로 import 가능).

사용법 (제일 쉬운 첫 실행 — Kaggle CLI 없이):
  1. kaggle.com에서 새 Notebook 생성, 이 파일 내용을 그대로 붙여넣기
  2. 우측 설정에서: Accelerator = GPU T4, Internet = On
  3. Add-ons > Secrets 에 GEMINI_API_KEY 등록 (선택: ROBOFLOW_API_KEY)
  4. Add-ons > Data 에서 원본 경기 영상을 담은 Dataset을 Input으로 추가
  5. 아래 "사용자 설정" 블록의 MATCH_VIDEO 경로를 실제 파일 경로로 수정
     (Kaggle 입력 데이터는 보통 /kaggle/input/<데이터셋-slug>/<파일명> 형태)
  6. Save & Run All (Kaggle이 알아서 백그라운드로 끝까지 실행)
  7. 끝나면 /kaggle/working/ 아래 산출물을 다운로드

⚠️ 주의: 이 스크립트는 아직 Kaggle에서 실제로 한 번도 돌려보지 않았다.
첫 실행에서 에러가 나면 그 로그를 그대로 알려주면 다음 버전에서 고친다.
특히 아래가 검증되지 않은 지점:
  - Roboflow에서 로컬 가중치(.pt) export가 무료로 되는지 (안 되면 자동으로
    기존 REST 방식으로 폴백하지만, 그러면 402 문제가 재발할 수 있음)
  - Kaggle 기본 이미지의 torch/opencv 버전과 이 프로젝트 의존성의 호환 여부
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── 사용자 설정 ──────────────────────────────────────────────────────────
MATCH_VIDEO = "/kaggle/input/YOUR-DATASET-SLUG/YOUR-VIDEO.mp4"  # ← 반드시 수정
MODE = "highlights"     # "highlights"(기본, 구간만 변환) | "full"(원본 전체 변환)
SENSITIVITY = "normal"  # "wide" | "normal" | "strict"
STRIDE = 2              # dot-play 프레임 샘플링(2 = 격프레임, 클수록 빠르고 성김)
WORK_DIR = Path("/kaggle/working")
REPO_URL = "https://github.com/sampark0626/HLEditor.git"
# ─────────────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_secret(name: str, default: str | None = None) -> str | None:
    """Kaggle Secrets에서 먼저 찾고, 없으면 환경변수로 폴백."""
    try:
        from kaggle_secrets import UserSecretsClient
        val = UserSecretsClient().get_secret(name)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(name, default)


def bootstrap() -> Path:
    """HLEditor 레포를 받아 sys.path에 추가하고, 부족한 의존성만 설치한다.

    torch/opencv/pandas/numpy/scipy 등은 Kaggle GPU 이미지에 이미 있으므로
    건드리지 않는다(잘못 재설치하면 CUDA 빌드가 깨질 수 있음). 이 프로젝트
    전용 패키지만 추가로 설치한다.
    """
    repo_dir = WORK_DIR / "HLEditor"
    if not repo_dir.exists():
        log("HLEditor 레포 클론 중...")
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(repo_dir)], check=True)
    sys.path.insert(0, str(repo_dir))

    log("추가 의존성 설치 중 (torch/opencv 등 기본 이미지 것은 건드리지 않음)...")
    extra = [
        "supervision>=0.24", "pyarrow>=15.0", "google-genai>=2.6,<3",
        "umap-learn>=0.5", "scikit-learn>=1.4", "transformers>=4.44", "timm>=1.0",
        "git+https://github.com/roboflow/sports.git",
        "roboflow", "gdown",
    ]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *extra], check=False)
    return repo_dir


class ModelsUnavailable(Exception):
    """검출 모델을 어떤 방식으로도 확보하지 못함 — 2D 변환 단계 전체를 건너뛴다."""


# roboflow/sports 레포 공식 예제(examples/soccer/setup.sh)가 쓰는 사전학습 가중치.
# Google Drive 공개 파일이라 Roboflow 계정/API 키/크레딧이 전혀 필요 없다.
# https://github.com/roboflow/sports/blob/main/examples/soccer/setup.sh
_GDRIVE_PLAYER_ID = "17PXFNlx-jI7VjVo_vQnB1sONjRyvoB-q"
_GDRIVE_FIELD_ID = "1Ma5Kt86tgpdjCTKfum79YMgNnSjcoOyf"


def _download_reference_weights(weights_dir: Path):
    """roboflow/sports 공식 예제의 사전학습 가중치를 Google Drive에서 내려받는다.

    Roboflow 계정과 무관 — 무료/유료 플랜, 크레딧 상태와 상관없이 항상 된다.
    한 번 받으면 WORK_DIR에 남으므로 같은 세션 재실행 시 다시 받지 않는다.
    """
    player_pt, field_pt = weights_dir / "player.pt", weights_dir / "field.pt"
    if player_pt.exists() and field_pt.exists():
        return player_pt, field_pt
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "gdown"], check=False)
    import gdown
    log("참조 저장소(roboflow/sports)의 공개 가중치 다운로드 중 (Roboflow 계정 불필요)...")
    gdown.download(id=_GDRIVE_PLAYER_ID, output=str(player_pt), quiet=False)
    gdown.download(id=_GDRIVE_FIELD_ID, output=str(field_pt), quiet=False)
    if not (player_pt.exists() and field_pt.exists()):
        raise RuntimeError("gdown 다운로드가 완료됐는데 파일이 없습니다 — Google Drive 접근이 막혔을 수 있음")
    return player_pt, field_pt


def resolve_models(device: str):
    """검출 모델을 우선순위대로 시도한다.

    1) 이미 학습해 둔 로컬 .pt 가중치(자체 Dataset으로 붙인 입력) — 있으면 최우선
    2) roboflow/sports 공식 예제의 사전학습 가중치를 Google Drive에서 직접 다운로드
       — **Roboflow 계정/API 키/크레딧 전혀 불필요**, 기본 경로로 권장
    3) Roboflow 가중치 export — 무료 플랜은 지원 안 함(Core 유료 플랜 이상만),
       https://docs.roboflow.com/models/model-weights/download-roboflow-model-weights
    4) Roboflow REST 호스팅 추론 — 크레딧 소진 시 402(이전 세션에서 실측)

    전부 안 되면 ModelsUnavailable을 던져 호출부가 2D 변환만 건너뛰고
    하이라이트 영상은 그대로 완성하게 한다.
    """
    from dotplay.pipeline import ModelSpec

    # 1) 자체 학습 가중치 — kaggle_runner/train_weights.py 로 한 번 만들어 두고
    #    이후로는 이 Dataset을 Input에 붙이기만 하면 된다.
    for base in Path("/kaggle/input").glob("*"):
        p, f = base / "player.pt", base / "field.pt"
        if p.exists() and f.exists():
            log(f"자체 학습 가중치 발견: {base.name} — 이걸 사용")
            return ModelSpec(player_weights=str(p), field_weights=str(f))

    # 2) 참조 저장소 공개 가중치 (Roboflow 계정 불필요, 기본 권장 경로)
    weights_dir = WORK_DIR / "weights"
    weights_dir.mkdir(exist_ok=True)
    try:
        player_pt, field_pt = _download_reference_weights(weights_dir)
        log("참조 가중치 다운로드 성공 — GPU 로컬 추론 사용 (Roboflow 미사용)")
        return ModelSpec(player_weights=str(player_pt), field_weights=str(field_pt))
    except Exception as e:
        log(f"참조 가중치 다운로드 실패({e!r}) — Roboflow 경로로 폴백")

    roboflow_key = get_secret("ROBOFLOW_API_KEY")
    player_model_id = os.environ.get("DOTPLAY_PLAYER_MODEL_ID", "football-players-detection-3zvbc/11")
    field_model_id = os.environ.get("DOTPLAY_FIELD_MODEL_ID", "football-field-detection-f07vi/14")

    # 2) Roboflow 가중치 export (유료 플랜 전용 — 무료면 실패가 정상, 계속 진행)
    weights_dir = WORK_DIR / "weights"
    weights_dir.mkdir(exist_ok=True)
    player_pt, field_pt = weights_dir / "player.pt", weights_dir / "field.pt"
    if roboflow_key:
        try:
            import roboflow
            rf = roboflow.Roboflow(api_key=roboflow_key)
            for model_id, dst in ((player_model_id, player_pt), (field_model_id, field_pt)):
                proj_slug, ver = model_id.split("/")
                # 이 두 모델은 우리 워크스페이스가 아니라 roboflow/sports가 쓰는
                # 공개 Universe 워크스페이스(roboflow-jvuqo) 소속이다. 기본
                # rf.workspace()(내 워크스페이스)로는 project()를 못 찾는다.
                proj = rf.workspace("roboflow-jvuqo").project(proj_slug)
                weights_path = proj.version(int(ver)).model.download("pytorch", location=str(weights_dir))
                if Path(weights_path).is_file():
                    Path(weights_path).rename(dst)
            if player_pt.exists() and field_pt.exists():
                log("Roboflow 가중치 export 성공 — GPU 로컬 추론 사용")
                return ModelSpec(player_weights=str(player_pt), field_weights=str(field_pt))
        except Exception as e:
            log(f"가중치 export 실패(무료 플랜이면 정상): {e!r}")

    # 3) REST 호스팅 추론 — 마지막 수단, 크레딧 있어야 동작
    if roboflow_key:
        log("Roboflow REST 호스팅 추론으로 시도 (크레딧 소진 시 여기서 실패할 수 있음)")
        return ModelSpec(player_model_id=player_model_id, field_model_id=field_model_id, api_key=roboflow_key)

    raise ModelsUnavailable("검출 모델을 확보할 방법이 없습니다 (자체 가중치 없음, ROBOFLOW_API_KEY 없음)")


def composite_pip(main_path, pip_path, out_path, run_fn,
                   width_ratio: float = 0.28, margin: int = 18, opacity: float = 0.9) -> None:
    """하이라이트 영상 하단 중앙에 dot-play 레이더를 얹는다.
    jobs_dotplay._composite_pip와 동일 로직 — Flask 결합을 피하려 여기 복제."""
    import cv2

    cap = cv2.VideoCapture(str(main_path))
    main_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    cap.release()
    if main_w <= 0:
        raise RuntimeError(f"하이라이트 영상을 열 수 없습니다: {main_path}")

    pip_w = max(2, int(round(main_w * width_ratio / 2)) * 2)
    fc = (
        f"[1:v]scale={pip_w}:-2,format=yuva420p,colorchannelmixer=aa={opacity}[pip];"
        f"[0:v][pip]overlay=(W-w)/2:H-h-{margin}:eof_action=repeat[v]"
    )
    run_fn([
        "ffmpeg", "-y", "-i", str(main_path), "-i", str(pip_path),
        "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "copy", str(out_path), "-loglevel", "error",
    ])


def main() -> None:
    if not Path(MATCH_VIDEO).exists():
        raise FileNotFoundError(
            f"MATCH_VIDEO 경로가 없습니다: {MATCH_VIDEO}\n"
            "스크립트 상단 MATCH_VIDEO 를 Kaggle Dataset 안의 실제 파일 경로로 바꾸세요."
        )
    bootstrap()

    import soccer_highlights as sh
    from dotplay.config import PipelineConfig
    from dotplay.device import resolve_device
    from dotplay.pipeline import run_radar, run_radar_segments

    work = WORK_DIR / "_work"
    work.mkdir(exist_ok=True)

    # ── 1) 하이라이트 추출 (오디오 스파이크 + Gemini 판별) ──────────────
    log("오디오 스파이크 검출 중...")
    dur = sh.probe_duration(MATCH_VIDEO)
    sp = sh.SENSITIVITY_PRESETS[SENSITIVITY]
    cands = sh.detect_spikes(MATCH_VIDEO, work, percentile=sp["percentile"], min_db=sp["min_db"])
    log(f"오디오 후보 {len(cands)}개 (영상 길이 {dur/60:.1f}분)")

    gemini_key = get_secret("GEMINI_API_KEY")
    vision_used = False
    if gemini_key and cands:
        from google import genai
        client = genai.Client(api_key=gemini_key)
        log("Gemini 비전 판별 중...")
        usage = sh.classify_all_parallel(cands, MATCH_VIDEO, work, client, sh.CONF_AUTO, sh.VISION_WORKERS)
        vision_used = True
        log(f"판별 완료 (호출 {usage.get('calls')}회)")
    else:
        log("GEMINI_API_KEY 없음 — 비전 판별 생략, 오디오 후보 전체 채택")

    selected, maybe = sh.select_segments(cands, sh.CONF_AUTO, vision_used)
    log(f"채택 {len(selected)}개 / 확인필요(자동 제외) {len(maybe)}개")
    if not selected:
        raise RuntimeError("채택된 하이라이트 구간이 없습니다 — 민감도(SENSITIVITY)를 낮춰 재시도하세요.")

    hl_out = WORK_DIR / "highlight.mp4"
    log(f"하이라이트 영상 생성 중 -> {hl_out}")
    sh.build_output(MATCH_VIDEO, selected, str(hl_out), work)

    # ── 2) Dot Play 2D 변환 (모델을 못 구하면 여기만 건너뛰고 하이라이트는 살린다) ──
    final_out = hl_out
    dotplay_error = None
    try:
        device = resolve_device("auto")
        log(f"추론 디바이스: {device}")
        cfg = PipelineConfig(device=device, stride=STRIDE)
        models = resolve_models(device)

        if MODE == "full":
            radar_out = WORK_DIR / "dotplay_full.mp4"
            log("원본 전체 2D 변환 중 (시간이 오래 걸릴 수 있음)...")
            result = run_radar(MATCH_VIDEO, models, cfg, device, out_video=str(radar_out))
            final_out = radar_out
        else:
            merged, _ = sh.get_merged_timeline(selected, sh.PRE_SEC, sh.POST_SEC)
            segments = [(c["start"], c["end"]) for c in merged]
            radar_out = WORK_DIR / "dotplay_radar.mp4"
            log(f"하이라이트 {len(segments)}개 구간만 2D 변환 중...")
            result = run_radar_segments(MATCH_VIDEO, segments, models, cfg, device, out_video=str(radar_out))

            final_out = WORK_DIR / "highlight_with_dotplay.mp4"
            log(f"하이라이트 + 2D 변환 합성 중 -> {final_out}")
            composite_pip(hl_out, radar_out, final_out, sh.run)

        if not result.coords.empty:
            coords_out = WORK_DIR / "coords.parquet"
            result.coords.to_parquet(coords_out)
            log(f"좌표 저장: {coords_out} ({result.coords['track_id'].nunique()}개 트랙)")
    except Exception as e:
        dotplay_error = repr(e)
        log(f"2D 변환 단계 실패 — 건너뛰고 하이라이트 영상만 남김: {dotplay_error}")

    summary = {
        "mode": MODE, "sensitivity": SENSITIVITY, "stride": STRIDE,
        "duration_sec": dur, "n_candidates": len(cands), "n_selected": len(selected),
        "n_maybe_excluded": len(maybe), "vision_used": vision_used,
        "final_output": str(final_out), "dotplay_error": dotplay_error,
    }
    (WORK_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    log(f"완료! 산출물: {WORK_DIR}")
    log(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
