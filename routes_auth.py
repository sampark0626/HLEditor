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
        return jsonify({"ok": False, "reason": "패키지 없음"})
    authenticated = yt_up.is_authenticated()
    channel = yt_up.get_channel_info() if authenticated else None
    return jsonify({
        "ok": authenticated,
        "has_secrets": yt_up.has_client_secrets(),
        "channel": channel,
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

    body    = request.json or {}
    titles  = body.get("titles", {})
    privacy = config.get_youtube_privacy()

    with jobs.JLOCK:
        to_upload = [
            {"id": j["id"]} for j in jobs.JOBS.values()
            if j.get("status") == "done"
            and j.get("output")
            and j.get("yt_status") not in ("uploading", "done")
        ]
    if not to_upload:
        return jsonify({"error": "업로드할 영상이 없습니다."}), 400

    for job in to_upload:
        jid   = job["id"]
        title = titles.get(jid, "").strip() or config.get_default_title()
        threading.Thread(
            target=upload_job_to_youtube,
            args=(jid, title, privacy),
            daemon=True,
        ).start()

    return jsonify({"ok": True, "n": len(to_upload)})
