#!/usr/bin/env python3
"""routes_band.py — BAND OAuth 인증 및 게시 라우트."""

import logging
from datetime import date

from flask import Blueprint, jsonify, redirect, request

import config
import jobs

log = logging.getLogger("hl")

# BAND 모듈 (선택적 — 패키지 미설치 시 기능 비활성화)
try:
    import band_poster as bp
    HAS_BAND = True
except Exception:
    HAS_BAND = False

bp_band = Blueprint("auth_band", __name__)


def _band_redirect_uri():
    return f"http://localhost:{config.get_port()}/auth/band/callback"


@bp_band.route("/auth/band")
def auth_band():
    if not HAS_BAND:
        return "requests 패키지 없음. pip install requests", 500
    try:
        url = bp.get_auth_url(_band_redirect_uri())
        return redirect(url)
    except ValueError as e:
        return str(e), 400


@bp_band.route("/auth/band/callback")
def auth_band_callback():
    code = request.args.get("code", "")
    if not code:
        return "인증 코드 없음", 400
    try:
        bp.exchange_code(code, _band_redirect_uri())
        log.info("BAND 인증 완료")
        return redirect("/?band=ok")
    except Exception as e:
        log.exception("BAND 인증 실패")
        return f"BAND 인증 실패: {e}", 500


@bp_band.route("/api/auth/band/status")
def api_band_status():
    if not HAS_BAND:
        return jsonify({"ok": False, "reason": "패키지 없음"})
    authenticated = bp.is_authenticated()
    bands = []
    if authenticated:
        try:
            bands = bp.get_bands()
        except Exception:
            pass
    return jsonify({
        "ok": authenticated,
        "has_creds": bp.has_credentials(),
        "bands": bands,
    })


@bp_band.route("/auth/band/revoke", methods=["POST"])
def auth_band_revoke():
    if HAS_BAND:
        bp.revoke()
    return jsonify({"ok": True})


# ─── BAND 게시 ────────────────────────────────────────────
@bp_band.route("/api/jobs/post-band", methods=["POST"])
def api_post_band():
    if not HAS_BAND:
        return jsonify({"error": "requests 패키지 없음"}), 500
    if not bp.is_authenticated():
        return jsonify({"error": "BAND 인증 필요. /auth/band 로 인증하세요."}), 401

    body     = request.json or {}
    band_key = body.get("band_key", "").strip() or config.get_band_target_key()
    if not band_key:
        return jsonify({"error": "밴드를 선택하거나 BAND_TARGET_KEY를 .env에 설정하세요."}), 400

    job_ids   = body.get("job_ids")   # None이면 전체 대상
    id_filter = set(job_ids) if job_ids else None

    # 업로드 완료된 잡들을 날짜별 그룹핑
    with jobs.JLOCK:
        uploaded = [
            {
                "id":         j["id"],
                "video_name": j["video_name"],
                "yt_url":     j.get("yt_url", ""),
                "match_date": j.get("match_date", date.today().isoformat()),
            }
            for j in jobs.JOBS.values()
            if (id_filter is None or j["id"] in id_filter)
            and j.get("yt_url") and j.get("yt_status") == "done"
            and j.get("band_status") not in ("posting", "done")
        ]
    if not uploaded:
        # 이미 전부 게시된 상태일 수 있으므로 오류가 아니다 (재개 시 정상 경로).
        return jsonify({"posted": [], "errors": [], "n": 0,
                        "note": "BAND에 게시할 새 YouTube 링크가 없습니다."})

    groups = bp.group_by_date(uploaded)
    url_to_id = {u["yt_url"]: u["id"] for u in uploaded if u["yt_url"]}

    results = []
    errors  = []
    for match_date, video_links in groups.items():
        content = bp.format_post_content(video_links, match_date=match_date)
        try:
            post_url = bp.write_post(band_key, content)
            # 해당 잡들 상태 업데이트
            for vname, vurl in video_links:
                jid = url_to_id.get(vurl)
                if jid:
                    jobs.update(jid, band_status="done", band_post_url=post_url)
            results.append({"date": match_date, "n": len(video_links), "url": post_url})
            log.info("BAND 게시 완료: %s, %d개 영상", match_date, len(video_links))
        except Exception as e:
            errors.append({"date": match_date, "error": str(e)[:200]})
            log.exception("BAND 게시 실패: %s", match_date)

    return jsonify({"posted": results, "errors": errors,
                    "n": sum(r["n"] for r in results)})
