#!/usr/bin/env python3
"""
HLEditor Kaggle 러너 (v1 — 배치 처리)
=========================================

로컬 서버 없이 Kaggle 커널(GPU) 하나로 "원본 영상들 -> 하이라이트 추출
-> (하이라이트 구간만, 또는 원본 전체) Dot Play 2D 변환 -> 합성"까지 끝낸다.
**한 번에 여러 경기를 처리**한다 — Dataset에 영상을 몇 개를 넣든, 코드 수정 없이
Input에 붙인 Dataset들 아래 있는 영상 파일을 전부 자동으로 찾아 순서대로 처리한다.

HLEditor 레포(soccer_highlights.py, dotplay/)를 그대로 git clone해 재사용한다
(Flask 의존성이 없는 순수 라이브러리라 그대로 import 가능).

사용법 (매주 반복하는 루틴 — Kaggle CLI 없이):
  1. 이번 주 경기 영상들(몇 개든)을 하나의 Kaggle Dataset에 업로드
     (기존 Dataset에 "새 버전"으로 올려 계속 재사용해도 되고, 새로 만들어도 됨)
  2. 저장해 둔 노트북을 열고, Input에 그 Dataset이 붙어 있는지 확인
     (버전만 바뀌었으면 자동 반영됨, 새 Dataset이면 Add Input으로 새로 붙이기)
  3. **코드는 그대로 두고** Save & Run All — MATCH_VIDEOS를 비워 두면(기본값)
     Input 아래 영상 파일을 전부 자동으로 찾아 하나씩 처리한다
  4. 끝나면 /kaggle/working/ 아래 영상별 산출물(highlight_<파일명>.mp4 등)을 다운로드

특정 영상만 처리하고 싶으면 MATCH_VIDEOS 에 경로 리스트를 직접 채우면 된다.

⚠️ 참고: 영상 하나 처리에 (지난 실행 실측) 하이라이트 추출만 약 15~20분,
2D 변환까지 하면 더 걸릴 수 있다. 여러 개를 한 번에 돌리면 그만큼 길어지니
Kaggle 세션 최대 길이(12시간)·주간 GPU 한도(약 30시간)를 감안할 것.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── 사용자 설정 ──────────────────────────────────────────────────────────
# 비워두면(기본) Input에 붙은 모든 Dataset에서 영상 파일을 자동으로 찾아 전부 처리한다.
# 특정 영상만 처리하려면 경로를 직접 채운다: ["/kaggle/input/.../1-1.mp4", ...]
MATCH_VIDEOS: list[str] = []
# 이미 처리해서 다시 돌리고 싶지 않은 영상의 파일명(확장자 제외, 예: "1-1")을 적어두면
# MATCH_VIDEOS 자동탐색에서 제외한다. 지난주 Dataset을 Input에서 안 지웠을 때의 안전장치.
# (가장 확실한 방법은 애초에 매주 새 이름의 Dataset을 쓰고 지난주 것은 Input에서 제거하는 것)
SKIP_STEMS: set[str] = set()
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi"}

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
        "ultralytics>=8.3",  # 로컬 .pt 가중치 추론(dotplay/detect.py의 ultralytics 백엔드)에 필요
        "tqdm>=4.66",  # dotplay/render.py 가 씀 — Kaggle 기본 이미지에 보통 있지만 방어적으로 명시
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
    #    이후로는 이 Dataset을 Input에 붙이기만 하면 된다. (마운트 깊이가
    #    일정하지 않아 재귀 탐색 — discover_match_videos()와 동일한 이유)
    for p in Path("/kaggle/input").rglob("player.pt"):
        f = p.parent / "field.pt"
        if f.exists():
            log(f"자체 학습 가중치 발견: {p.parent} — 이걸 사용")
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


def discover_match_videos() -> list[Path]:
    """Input에 붙은 모든 Dataset에서 영상 파일을 자동으로 찾는다.

    Kaggle의 Input 마운트 경로 깊이는 상황에 따라 다르다(실측:
    /kaggle/input/<slug>/<file> 뿐 아니라 /kaggle/input/datasets/<owner>/<slug>/<file>
    형태도 나옴) — 그래서 깊이를 가정하지 않고 /kaggle/input 전체를 재귀 탐색한다.
    자체 학습 가중치 Dataset(player.pt+field.pt 들어있는 폴더)은 영상이 아니므로,
    그 폴더 하위는 전부 제외한다.
    """
    root = Path("/kaggle/input")
    if not root.exists():
        return []
    weight_dirs = {p.parent for p in root.rglob("player.pt") if (p.parent / "field.pt").exists()}
    videos, skipped = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        if any(wd in p.parents for wd in weight_dirs):
            continue
        if p.stem in SKIP_STEMS:
            skipped.append(p.name)
            continue
        videos.append(p)
    if skipped:
        log(f"SKIP_STEMS로 제외됨(이미 처리한 것으로 표시): {skipped}")
    return videos


def process_match(video: str, out_dir: Path) -> dict:
    """영상 한 편을 처리: 하이라이트 추출 -> (가능하면) 2D 변환 -> 합성.

    out_dir 아래 <영상 이름 stem> 을 파일명에 붙여 저장하므로, 여러 편을
    한 세션에서 처리해도 서로 산출물을 덮어쓰지 않는다.
    """
    import soccer_highlights as sh
    from dotplay.config import PipelineConfig
    from dotplay.device import resolve_device
    from dotplay.pipeline import run_radar, run_radar_segments

    stem = Path(video).stem
    work = WORK_DIR / "_work" / stem
    work.mkdir(parents=True, exist_ok=True)

    # ── 1) 하이라이트 추출 (오디오 스파이크 + Gemini 판별) ──────────────
    log(f"[{stem}] 오디오 스파이크 검출 중...")
    dur = sh.probe_duration(video)
    sp = sh.SENSITIVITY_PRESETS[SENSITIVITY]
    cands = sh.detect_spikes(video, work, percentile=sp["percentile"], min_db=sp["min_db"])
    log(f"[{stem}] 오디오 후보 {len(cands)}개 (영상 길이 {dur/60:.1f}분)")

    gemini_key = get_secret("GEMINI_API_KEY")
    vision_used = False
    if gemini_key and cands:
        from google import genai
        client = genai.Client(api_key=gemini_key)
        log(f"[{stem}] Gemini 비전 판별 중...")
        usage = sh.classify_all_parallel(cands, video, work, client, sh.CONF_AUTO, sh.VISION_WORKERS)
        vision_used = True
        log(f"[{stem}] 판별 완료 (호출 {usage.get('calls')}회)")
    else:
        log(f"[{stem}] GEMINI_API_KEY 없음 — 비전 판별 생략, 오디오 후보 전체 채택")

    selected, maybe = sh.select_segments(cands, sh.CONF_AUTO, vision_used)
    log(f"[{stem}] 채택 {len(selected)}개 / 확인필요(자동 제외) {len(maybe)}개")
    if not selected:
        raise RuntimeError("채택된 하이라이트 구간이 없습니다 — 민감도(SENSITIVITY)를 낮춰 재시도하세요.")

    hl_out = out_dir / f"highlight_{stem}.mp4"
    log(f"[{stem}] 하이라이트 영상 생성 중 (클립 {len(selected)}개 재인코딩) -> {hl_out}")
    _t0 = time.monotonic()

    def _build_progress(done, total):
        if done == total or done % 5 == 0:
            log(f"[{stem}]   클립 인코딩 {done}/{total} ({time.monotonic() - _t0:.0f}초 경과)")

    sh.build_output(video, selected, str(hl_out), work, on_progress=_build_progress)
    log(f"[{stem}] 하이라이트 영상 완성 ({time.monotonic() - _t0:.0f}초 소요)")

    # ── 2) Dot Play 2D 변환 (모델을 못 구하면 여기만 건너뛰고 하이라이트는 살린다) ──
    final_out = hl_out
    dotplay_error = None
    try:
        device = resolve_device("auto")
        log(f"[{stem}] 추론 디바이스: {device}")
        cfg = PipelineConfig(device=device, stride=STRIDE)
        models = resolve_models(device)

        if MODE == "full":
            radar_out = out_dir / f"dotplay_full_{stem}.mp4"
            log(f"[{stem}] 원본 전체 2D 변환 중 (시간이 오래 걸릴 수 있음)...")
            result = run_radar(video, models, cfg, device, out_video=str(radar_out))
            final_out = radar_out
        else:
            merged, _ = sh.get_merged_timeline(selected, sh.PRE_SEC, sh.POST_SEC)
            segments = [(c["start"], c["end"]) for c in merged]
            radar_out = out_dir / f"dotplay_radar_{stem}.mp4"
            log(f"[{stem}] 하이라이트 {len(segments)}개 구간만 2D 변환 중...")
            result = run_radar_segments(video, segments, models, cfg, device, out_video=str(radar_out))

            final_out = out_dir / f"highlight_with_dotplay_{stem}.mp4"
            log(f"[{stem}] 하이라이트 + 2D 변환 합성 중 -> {final_out}")
            composite_pip(hl_out, radar_out, final_out, sh.run)

        if not result.coords.empty:
            coords_out = out_dir / f"coords_{stem}.parquet"
            result.coords.to_parquet(coords_out)
            log(f"[{stem}] 좌표 저장: {coords_out} ({result.coords['track_id'].nunique()}개 트랙)")
    except Exception as e:
        dotplay_error = repr(e)
        log(f"[{stem}] 2D 변환 단계 실패 — 건너뛰고 하이라이트 영상만 남김: {dotplay_error}")

    summary = {
        "video": str(video), "mode": MODE, "sensitivity": SENSITIVITY, "stride": STRIDE,
        "duration_sec": dur, "n_candidates": len(cands), "n_selected": len(selected),
        "n_maybe_excluded": len(maybe), "vision_used": vision_used,
        "final_output": str(final_out), "dotplay_error": dotplay_error,
    }
    (out_dir / f"summary_{stem}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    log(f"[{stem}] 완료 -> {final_out}")
    return summary


def main() -> None:
    bootstrap()

    videos = [Path(v) for v in MATCH_VIDEOS] or discover_match_videos()
    missing = [v for v in videos if not v.exists()]
    if missing:
        raise FileNotFoundError(f"영상을 찾을 수 없습니다: {missing}")
    if not videos:
        raise FileNotFoundError(
            "처리할 영상이 없습니다 — Input에 경기 영상이 든 Dataset을 붙였는지, "
            "또는 MATCH_VIDEOS를 직접 채웠는지 확인하세요."
        )
    log(f"처리할 영상 {len(videos)}개: {[v.name for v in videos]}")

    results = []
    for i, video in enumerate(videos, 1):
        log(f"===== [{i}/{len(videos)}] {video.name} 처리 시작 =====")
        try:
            results.append(process_match(str(video), WORK_DIR))
        except Exception as e:
            log(f"[{video.name}] 처리 실패 — 다음 영상으로 넘어감: {e!r}")
            results.append({"video": str(video), "error": repr(e)})

    ok = sum(1 for r in results if not r.get("error"))
    (WORK_DIR / "batch_summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    log(f"===== 배치 완료: {len(videos)}개 중 {ok}개 성공 =====")
    log(json.dumps(results, ensure_ascii=False, indent=2))

    done_stems = sorted({Path(r["video"]).stem for r in results if r.get("video")})
    log("같은 Dataset을 다음에도 재사용할 계획이면, 이번에 처리한 영상을 "
        f"SKIP_STEMS에 넣어 다음 실행에서 건너뛸 수 있습니다: {done_stems}")


if __name__ == "__main__":
    main()
