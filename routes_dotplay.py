#!/usr/bin/env python3
"""routes_dotplay.py — Dot-play(FM 스타일 2D 버드뷰) 잡 관리 API.

jobs.py/routes_jobs.py(하이라이트 파이프라인)와 완전히 분리된 별도 Blueprint.
"""

import logging
import os

from flask import Blueprint, jsonify, request, send_from_directory

import config
import jobs_dotplay as jd

log = logging.getLogger("hl")

bp_dotplay = Blueprint("dotplay_api", __name__)


@bp_dotplay.route("/api/dotplay/status")
def api_dotplay_status():
    """설정 상태(Roboflow API 키 존재 여부 등)를 UI에 알린다."""
    return jsonify({
        "has_roboflow_key": bool(config.load_roboflow_api_key()),
        "ffmpeg_ok": config.ffmpeg_available(),
    })


@bp_dotplay.route("/api/dotplay/jobs/add", methods=["POST"])
def api_dotplay_add():
    body = request.json or {}
    video = (body.get("video") or "").strip().strip('"')
    if not video:
        return jsonify({"error": "영상 경로가 비어 있습니다."}), 400
    abs_v = os.path.abspath(video)
    if not os.path.exists(abs_v):
        return jsonify({"error": f"파일 없음: {os.path.basename(video)}"}), 400

    with jd.DOTPLAY_LOCK:
        active = any(j["video"] == abs_v and j["status"] in ("pending", "running")
                     for j in jd.DOTPLAY_JOBS.values())
    if active:
        return jsonify({"error": "이미 처리 중인 영상입니다."}), 409

    stride = max(1, min(int(body.get("stride", 2)), 10))
    jid = jd.new_job(abs_v, stride=stride)
    jd.DOTPLAY_QUEUE.put(jid)
    log.info("[dotplay] 잡 추가: %s [%s] (stride=%s)", os.path.basename(abs_v), jid, stride)
    return jsonify({"id": jid})


@bp_dotplay.route("/api/dotplay/hl_jobs")
def api_dotplay_hl_jobs():
    """PiP 합성 대상이 될 수 있는 '완료된' 하이라이트 잡 목록 (UI 선택용)."""
    import jobs as hj  # 하이라이트 잡 저장소 — 순환 import 없음(읽기 전용)
    picked = []
    with hj.JLOCK:
        snapshot = [dict(j) for j in hj.JOBS.values()
                    if j["status"] == "done" and j.get("output") and j.get("approved")]
    for j in snapshot:
        if not os.path.exists(j["output"]) or not os.path.exists(j["video"]):
            continue  # 원본이 지워졌으면 PiP 분석 불가
        picked.append({
            "id": j["id"],
            "video_name": j["video_name"],
            "output_name": os.path.basename(j["output"]),
            "n_approved": len(j["approved"]),
        })
    return jsonify({"jobs": picked})


@bp_dotplay.route("/api/dotplay/jobs/add_pip", methods=["POST"])
def api_dotplay_add_pip():
    """완료된 하이라이트 잡을 골라 'dot-play PiP 합성' 잡을 추가한다.

    편집본이 아니라 원본 영상의 해당 구간들을 분석하므로(장면 전환에서 추적이
    깨짐), 하이라이트를 만들 때 쓴 것과 동일한 병합 타임라인을 여기서 계산해
    잡에 스냅샷으로 저장한다(이후 하이라이트 잡이 삭제/재빌드돼도 무관).
    """
    import jobs as hj
    import soccer_highlights as sh

    body = request.json or {}
    hl_jid = (body.get("hl_jid") or "").strip()
    stride = max(1, min(int(body.get("stride", 2)), 10))

    with hj.JLOCK:
        hl = hj.JOBS.get(hl_jid)
        hl = dict(hl) if hl else None
    if not hl or hl["status"] != "done" or not hl.get("output"):
        return jsonify({"error": "완료된(영상 생성까지 끝난) 하이라이트 잡이 아닙니다."}), 400
    if not os.path.exists(hl["output"]):
        return jsonify({"error": "하이라이트 출력 파일이 없습니다. 먼저 영상을 다시 생성하세요."}), 400
    if not os.path.exists(hl["video"]):
        return jsonify({"error": "원본 영상 파일을 찾을 수 없습니다 — PiP 분석에는 원본이 필요합니다."}), 400

    cands = hl.get("candidates") or []
    approved = hl.get("approved") or []
    selected = sorted([cands[i] for i in approved if i < len(cands)],
                      key=lambda c: c["peak"])
    if not selected:
        return jsonify({"error": "승인된 구간이 없습니다."}), 400

    merged, _ = sh.get_merged_timeline(
        selected, hl.get("pre_sec", sh.PRE_SEC), hl.get("post_sec", sh.POST_SEC))
    segments = [[c["start"], c["end"]] for c in merged]
    total = sum(e - s for s, e in segments)

    with jd.DOTPLAY_LOCK:
        active = any(j.get("hl_output") == hl["output"]
                     and j["status"] in ("pending", "running")
                     for j in jd.DOTPLAY_JOBS.values())
    if active:
        return jsonify({"error": "이미 이 하이라이트로 PiP 합성이 진행 중입니다."}), 409

    # 무재인코딩(copy) 빌드는 컷이 키프레임에 스냅돼 타임라인이 어긋날 수 있음 — 미리 경고
    note = None
    try:
        actual = sh.probe_duration(hl["output"])
        if abs(actual - total) > 1.0:
            note = (f"하이라이트 실제 길이({actual:.1f}초)와 구간 타임라인({total:.1f}초)이 "
                    f"{abs(actual - total):.1f}초 차이 — PiP 싱크가 어긋날 수 있습니다"
                    " ('최고 속도(무재인코딩)' 빌드가 원인일 수 있음)")
    except Exception:
        pass

    jid = jd.new_job(
        hl["video"], stride=stride, mode="pip", note=note,
        hl=dict(name=os.path.basename(hl["output"]), output=hl["output"],
                segments=segments, total=total),
    )
    jd.DOTPLAY_QUEUE.put(jid)
    log.info("[dotplay] PiP 잡 추가: %s ← %s (%d구간 %.0f초, stride=%s)",
             os.path.basename(hl["output"]), hl["video_name"],
             len(segments), total, stride)
    return jsonify({"id": jid, "n_segments": len(segments),
                    "total_sec": round(total, 1), "note": note})


@bp_dotplay.route("/api/dotplay/jobs")
def api_dotplay_list():
    with jd.DOTPLAY_LOCK:
        jobs_copy = list(jd.DOTPLAY_JOBS.values())
    return jsonify({"jobs": [jd.job_summary(j) for j in jobs_copy]})


@bp_dotplay.route("/api/dotplay/jobs/<jid>/cancel", methods=["POST"])
def api_dotplay_cancel(jid):
    ok = jd.request_cancel(jid)
    return jsonify({"ok": ok})


@bp_dotplay.route("/api/dotplay/jobs/<jid>", methods=["DELETE"])
def api_dotplay_delete(jid):
    ok = jd.request_delete(jid)
    return jsonify({"ok": ok})


@bp_dotplay.route("/api/dotplay/jobs/<jid>/video")
def api_dotplay_video(jid):
    with jd.DOTPLAY_LOCK:
        job = jd.DOTPLAY_JOBS.get(jid)
        name = job.get("output_video") if job else None
    if not name:
        return jsonify({"error": "아직 생성된 영상이 없습니다."}), 404
    return send_from_directory(config.DOTPLAY_DIR, name)


@bp_dotplay.route("/api/dotplay/jobs/<jid>/radar")
def api_dotplay_radar(jid):
    """PiP 잡의 합성 전 레이더 단독 영상."""
    with jd.DOTPLAY_LOCK:
        job = jd.DOTPLAY_JOBS.get(jid)
        name = job.get("output_radar") if job else None
    if not name:
        return jsonify({"error": "레이더 영상이 없습니다."}), 404
    return send_from_directory(config.DOTPLAY_DIR, name)


@bp_dotplay.route("/api/dotplay/jobs/<jid>/coords")
def api_dotplay_coords(jid):
    """스무딩된 좌표 테이블(parquet) 다운로드 — 외부 분석용."""
    with jd.DOTPLAY_LOCK:
        job = jd.DOTPLAY_JOBS.get(jid)
        name = job.get("output_coords") if job else None
    if not name:
        return jsonify({"error": "좌표 데이터가 없습니다."}), 404
    return send_from_directory(config.DOTPLAY_DIR, name, as_attachment=True)
