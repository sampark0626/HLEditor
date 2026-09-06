"""원스톱 파이프라인이 중간에 끊기지 않고, 멈춘 단계부터 재개되도록 하는 계약 테스트.

여기서 검증하는 것은 서버 API가 "이미 처리된 상태"를 오류가 아니라 정상(200)으로
응답하는지다. 이 성질이 깨지면 프론트의 post()가 예외를 던져 파이프라인이
업로드 단계에서 통째로 중단된다 (원클릭 실패의 실제 원인).
"""

import os
from unittest.mock import patch

import pytest

import app as app_module
import jobs


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
    with jobs.JLOCK:
        jobs.JOBS.clear()
    while not jobs.JOB_QUEUE.empty():
        jobs.JOB_QUEUE.get_nowait()


def _make_built_job(video_path="C:/fake/video.mp4", yt_status=None, output="C:/fake/out.mp4"):
    """빌드까지 끝난(status=done, output 있음) 잡을 주입한다."""
    jid = jobs.new_job(video_path, sensitivity="normal", workers=2)
    with jobs.JLOCK:
        job = jobs.JOBS[jid]
        job["status"] = "done"
        job["output"] = output
        job["approved"] = [0]
        job["candidates"] = [
            {"peak": 10.0, "delta_db": 12.0, "highlight": True, "type": "goal",
             "confidence": 0.9, "reason": "테스트 골"},
        ]
        job["yt_status"] = yt_status
    return jid


# ─── 1. 중복 업로드 호출이 파이프라인을 죽이지 않아야 한다 ────────────────────
def test_upload_all_returns_ok_when_everything_already_uploading(client):
    """build-all의 auto_upload가 이미 업로드를 시작한 뒤 파이프라인이 안전망으로
    upload-all을 한 번 더 호출하는 것이 정상 경로다. 400을 주면 파이프라인이 끊긴다."""
    jid = _make_built_job(yt_status="uploading")
    with patch("routes_auth.yt_up.is_authenticated", return_value=True):
        r = client.post("/api/jobs/upload-all-youtube", json={"job_ids": [jid]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["n"] == 0        # 새로 시작한 업로드 없음
    assert body["already"] == 1  # 이미 진행 중


def test_upload_all_starts_only_missing_jobs(client):
    """auto_upload가 일부 잡에서만 실패한 경우, 그 잡만 집어내 업로드를 시작한다."""
    done_jid    = _make_built_job(yt_status="done")
    missing_jid = _make_built_job(yt_status=None)
    started = []
    with patch("routes_auth.yt_up.is_authenticated", return_value=True), \
         patch("routes_auth.threading.Thread") as mock_thread:
        mock_thread.side_effect = lambda *a, **kw: (
            started.append(kw["args"][0]) or type("T", (), {"start": lambda s: None})())
        r = client.post("/api/jobs/upload-all-youtube",
                        json={"job_ids": [done_jid, missing_jid]})
    assert r.status_code == 200
    assert r.get_json()["n"] == 1
    assert started == [missing_jid]


def test_upload_all_scoped_to_job_ids(client):
    """job_ids 밖의 잡(큐에 남아 있던 예전 작업)은 건드리지 않는다."""
    mine    = _make_built_job()
    stale   = _make_built_job(video_path="C:/fake/old.mp4")
    started = []
    with patch("routes_auth.yt_up.is_authenticated", return_value=True), \
         patch("routes_auth.threading.Thread") as mock_thread:
        mock_thread.side_effect = lambda *a, **kw: (
            started.append(kw["args"][0]) or type("T", (), {"start": lambda s: None})())
        client.post("/api/jobs/upload-all-youtube", json={"job_ids": [mine]})
    assert started == [mine]
    assert stale not in started


# ─── 2. 재개 시 이미 끝난 작업을 다시 하지 않아야 한다 ────────────────────────
def test_build_all_skips_already_built_jobs_on_resume(client, tmp_path):
    """[이어서 진행]으로 build-all을 다시 호출해도 이미 만들어진 영상은 재인코딩하지 않는다."""
    out = tmp_path / "out.mp4"
    out.write_bytes(b"fake")
    jid = _make_built_job(output=str(out))
    r = client.post("/api/jobs/build-all",
                    json={"job_ids": [jid], "skip_built": True, "quality": "balanced"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["n"] == 0
    assert body["already_built"] == 1


def test_build_all_rebuilds_when_output_file_is_gone(client):
    """skip_built여도 출력 파일이 실제로 없으면 다시 만든다."""
    jid = _make_built_job(output="C:/fake/does_not_exist.mp4")
    assert not os.path.exists("C:/fake/does_not_exist.mp4")
    with patch("routes_jobs.threading.Thread"):
        r = client.post("/api/jobs/build-all",
                        json={"job_ids": [jid], "skip_built": True})
    assert r.status_code == 200
    assert r.get_json()["n"] == 1


def test_approve_all_scoped_to_job_ids(client):
    """approve-all도 파이프라인 잡만 대상으로 해야 한다."""
    jid = jobs.new_job("C:/fake/a.mp4", "normal", 2)
    other = jobs.new_job("C:/fake/b.mp4", "normal", 2)
    for j in (jid, other):
        with jobs.JLOCK:
            jobs.JOBS[j]["status"] = "ready"
            jobs.JOBS[j]["vision_used"] = True
            jobs.JOBS[j]["candidates"] = [
                {"peak": 1.0, "delta_db": 9.0, "highlight": True, "type": "goal",
                 "confidence": 0.95, "reason": "골"},
            ]
    r = client.post("/api/jobs/approve-all", json={"job_ids": [jid]})
    assert r.status_code == 200
    assert [s["id"] for s in r.get_json()["saved"]] == [jid]
    assert jobs.JOBS[other]["approved"] is None   # 손대지 않음


# ─── 3. 인코딩만 실패한 잡은 재분석 없이 다시 만든다 ──────────────────────────
def test_retry_build_stage_keeps_analysis(client):
    """빌드 단계 실패 후 재개할 때 몇 분짜리 재분석을 다시 하지 않아야 한다."""
    jid = _make_built_job()
    with jobs.JLOCK:
        jobs.JOBS[jid]["status"] = "error"
        jobs.JOBS[jid]["error"] = "ffmpeg 실패"
        jobs.JOBS[jid]["output"] = None
    r = client.post(f"/api/jobs/{jid}/retry", json={"stage": "build"})
    assert r.status_code == 200
    job = jobs.JOBS[jid]
    assert job["status"] == "ready"        # 큐로 되돌아가지 않고 빌드 대기 상태
    assert job["error"] is None
    assert job["approved"] == [0]          # 승인 결과 유지
    assert len(job["candidates"]) == 1     # 분석 결과 유지
    assert jobs.JOB_QUEUE.empty()          # 재분석 큐에 들어가지 않음


def test_retry_build_stage_rejected_without_candidates(client):
    """분석 결과가 없으면 빌드만 재시도할 수 없다 (전체 재처리로 유도)."""
    jid = jobs.new_job("C:/fake/x.mp4", "normal", 2)
    with jobs.JLOCK:
        jobs.JOBS[jid]["status"] = "error"
        jobs.JOBS[jid]["candidates"] = []
    r = client.post(f"/api/jobs/{jid}/retry", json={"stage": "build"})
    assert r.status_code == 400


# ─── 4. 자동 업로드를 조용히 건너뛰지 않아야 한다 ─────────────────────────────
def test_auto_upload_skip_records_reason_on_job(client):
    """미인증으로 자동 업로드를 건너뛰면 잡에 사유가 남아야 한다 (예전엔 무음 실패)."""
    import routes_jobs
    jid = _make_built_job()
    with patch("routes_auth.yt_up.is_authenticated", return_value=False):
        ok = routes_jobs._try_auto_upload(jid, "제목", "public")
    assert ok is False
    job = jobs.JOBS[jid]
    assert job["yt_status"] == "error"
    assert "인증" in job["yt_error"]
