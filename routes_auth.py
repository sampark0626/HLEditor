#!/usr/bin/env python3
"""routes_auth.py — YouTube OAuth 인증 및 업로드 라우트."""

import logging
import os
import tempfile
import threading

from flask import Blueprint, jsonify, redirect, request

import config
import jobs

log = logging.getLogger("hl")

# YouTube 모듈 (선택적 — 패키지 미설치 시 기능 비활성화)
try:
    import youtube_uploader as yt_up
    HAS_YT = True
except Exception:
    HAS_YT = False

bp_auth = Blueprint("auth_youtube", __name__)


def _yt_redirect_uri():
    return f"http://localhost:{config.get_port()}/auth/youtube/callback"


@bp_auth.route("/auth/youtube")
def auth_youtube():
    if not HAS_YT:
        return "google-auth-oauthlib 패키지 없음. pip install google-auth-oauthlib", 500
    try:
        url = yt_up.get_auth_url(_yt_redirect_uri())
        return redirect(url)
    except FileNotFoundError as e:
        return str(e), 400


@bp_auth.route("/auth/youtube/callback")
def auth_youtube_callback():
    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    if not code:
        return "인증 코드 없음", 400
    try:
        yt_up.exchange_code(code, _yt_redirect_uri(), state=state)
        log.info("YouTube 인증 완료")
        return redirect("/?yt=ok")
    except Exception as e:
        log.exception("YouTube 인증 실패")
        return f"YouTube 인증 실패: {e}", 500


@bp_auth.route("/api/auth/youtube/status")
def api_yt_status():
    if not HAS_YT:
        return jsonify({"ok": False, "reason": "no_package", "channel": None})
    st = yt_up.auth_status()
    return jsonify({
        "ok": st["ok"],
        "reason": st["reason"],          # ok | no_token | unauthorized | no_channel | network
        "detail": st.get("detail", ""),
        "has_secrets": yt_up.has_client_secrets(),
        "channel": st["channel"],
    })


@bp_auth.route("/auth/youtube/revoke", methods=["POST"])
def auth_youtube_revoke():
    if HAS_YT:
        yt_up.revoke()
    return jsonify({"ok": True})


# ─── YouTube 업로드 ───────────────────────────────────────
def upload_job_to_youtube(jid: str, title: str, privacy: str):
    """배경 스레드: 하이라이트 영상 → YouTube 업로드.

    build-all의 auto_upload 분기(routes_jobs.py)에서도 호출된다.
    """
    job = jobs.JOBS.get(jid)
    if not job:
        return
    output = job.get("output")
    if not output or not os.path.exists(output):
        jobs.update(jid, yt_status="error", yt_error="출력 파일 없음")
        return

    jobs.update(jid, yt_status="uploading", yt_progress={"done": 0, "total": 0}, yt_error=None)
    log.info("[%s] YouTube 업로드 시작: %s", jid, title)

    thumb_path = None
    try:
        # 썸네일 추출: 가장 신뢰도 높은 승인 후보
        approved = job.get("approved") or []
        cands     = job.get("candidates") or []
        if approved and cands and HAS_YT:
            best = max(
                (cands[i] for i in approved if i < len(cands)),
                key=lambda c: float(c.get("confidence") or 0),
                default=None,
            )
            if best:
                tf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tf.close()
                peak = float(best.get("peak", 0))
                if yt_up.extract_thumbnail(job["video"], peak, tf.name):
                    thumb_path = tf.name
                else:
                    try: os.unlink(tf.name)
                    except Exception: pass

        # 설명 생성
        import soccer_highlights as sh
        desc = yt_up.generate_description(
            cands, approved, job["video_name"],
            pre_sec=job.get("pre_sec", sh.PRE_SEC),
            post_sec=job.get("post_sec", sh.POST_SEC)
        )

        def _on_prog(done, total):
            jobs.update(jid, yt_progress={"done": done, "total": total})

        yt_url = yt_up.upload_video(
            output, title, desc,
            thumbnail_path=thumb_path,
            privacy=privacy,
            on_progress=_on_prog,
        )
        jobs.update(jid, yt_status="done", yt_url=yt_url, yt_progress={"done": 1, "total": 1})
        log.info("[%s] YouTube 업로드 완료: %s", jid, yt_url)
    except Exception as e:
        msg = str(e)[:300]
        jobs.update(jid, yt_status="error", yt_error=msg)
        log.exception("[%s] YouTube 업로드 실패", jid)
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try: os.unlink(thumb_path)
            except Exception: pass


@bp_auth.route("/api/jobs/<jid>/upload-youtube", methods=["POST"])
def api_upload_youtube(jid):
    if not HAS_YT:
        return jsonify({"error": "google-auth-oauthlib 패키지 없음"}), 500
    if not yt_up.is_authenticated():
        return jsonify({"error": "YouTube 인증 필요. /auth/youtube 로 인증하세요."}), 401
    job = jobs.JOBS.get(jid)
    if not job:
        return jsonify({"error": "잡 없음"}), 404
    if job.get("status") != "done" or not job.get("output"):
        return jsonify({"error": "영상이 아직 생성되지 않았습니다."}), 400
    if job.get("yt_status") == "uploading":
        return jsonify({"error": "업로드 중입니다."}), 409

    body    = request.json or {}
    title   = body.get("title", "").strip() or config.get_default_title()
    privacy = config.get_youtube_privacy()

    threading.Thread(
        target=upload_job_to_youtube,
        args=(jid, title, privacy),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@bp_auth.route("/api/jobs/upload-all-youtube", methods=["POST"])
def api_upload_all_youtube():
    if not HAS_YT:
        return jsonify({"error": "google-auth-oauthlib 패키지 없음"}), 500
    if not yt_up.is_authenticated():
        return jsonify({"error": "YouTube 인증 필요"}), 401

    body     = request.json or {}
    titles   = body.get("titles", {})
    job_ids  = body.get("job_ids")   # None이면 전체 대상 (수동 [전체 업로드] 버튼)
    privacy  = config.get_youtube_privacy()
    id_filter = set(job_ids) if job_ids else None

    with jobs.JLOCK:
        target = [
            j for j in jobs.JOBS.values()
            if (id_filter is None or j["id"] in id_filter)
            and j.get("status") == "done"
            and j.get("output")
        ]
        # 이미 업로드 중이거나 끝난 잡은 건너뛴다 (build-all의 auto_upload가 먼저
        # 시작했거나, 파이프라인 [이어서 진행]으로 재호출된 경우).
        to_upload = [j["id"] for j in target
                     if j.get("yt_status") not in ("uploading", "done")]
        already   = [j["id"] for j in target
                     if j.get("yt_status") in ("uploading", "done")]

    for jid in to_upload:
        title = titles.get(jid, "").strip() or config.get_default_title()
        threading.Thread(
            target=upload_job_to_youtube,
            args=(jid, title, privacy),
            daemon=True,
        ).start()

    # 대상이 0건이어도 오류가 아니다. 이미 업로드가 진행/완료된 정상 상태일 수 있으므로
    # 200으로 응답해야 원스톱 파이프라인이 중단되지 않는다.
    log.info("업로드 요청: 신규 %d개, 이미 진행/완료 %d개", len(to_upload), len(already))
    return jsonify({"ok": True, "n": len(to_upload), "already": len(already)})
