"""Flask API 스모크 테스트: 큐 추가 → 승인 → 빌드 흐름.

실제 ffmpeg/Gemini 호출 없이 검증하기 위해, 오디오 분석·비전 판별이 이미 끝난
"ready" 상태 잡을 jobs.JOBS에 직접 주입하고, 빌드 단계는 soccer_highlights.build_output을
목(mock)으로 교체해 서버 API 계층(라우팅·상태 전이·응답 형식)만 검증한다.
"""

import time

import pytest

import app as app_module
import jobs
import routes_jobs
import soccer_highlights as sh


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
    with jobs.JLOCK:
        jobs.JOBS.clear()
    while not jobs.JOB_QUEUE.empty():
        jobs.JOB_QUEUE.get_nowait()


def _make_ready_job(video_path="C:/fake/video.mp4"):
    jid = jobs.new_job(video_path, sensitivity="normal", workers=2)
    with jobs.JLOCK:
        job = jobs.JOBS[jid]
        job["status"] = "ready"
        job["vision_used"] = True
        job["duration"] = 60.0
        job["workdir"] = "C:/fake/workdir"
        job["candidates"] = [
            {"peak": 10.0, "delta_db": 12.0, "highlight": True, "type": "goal",
             "confidence": 0.9, "reason": "테스트 골"},
            {"peak": 30.0, "delta_db": 9.0, "highlight": False, "type": "other",
             "confidence": 0.2, "reason": "패스"},
        ]
    return jid


def test_jobs_list_empty_initially(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.get_json()["jobs"] == []


def test_add_rejects_nonexistent_file(client):
    r = client.post("/api/jobs/add", json={"videos": ["C:/no/such/file.mp4"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["added"] == []
    assert body["errors"]


def test_candidates_and_approve_flow(client):
    jid = _make_ready_job()

    r = client.get(f"/api/jobs/{jid}/candidates?conf=0.7")
    assert r.status_code == 200
    cands = r.get_json()["candidates"]
    assert len(cands) == 2
    assert cands[0]["flag"] == "auto"    # confidence 0.9 >= 0.7
    assert cands[1]["flag"] == "reject"  # highlight=False

    r = client.post(f"/api/jobs/{jid}/approve", json={"approved": [0]})
    assert r.status_code == 200
    assert r.get_json()["n"] == 1

    r = client.get("/api/jobs")
    summary = next(j for j in r.get_json()["jobs"] if j["id"] == jid)
    assert summary["n_approved"] == 1
    assert summary["n_auto"] == 1


def test_approve_all_applies_ai_default(client):
    jid = _make_ready_job()
    r = client.post("/api/jobs/approve-all", json={"conf": 0.7})
    assert r.status_code == 200
    entry = next(s for s in r.get_json()["saved"] if s["id"] == jid)
    assert entry["n"] == 1  # 신뢰도 0.9짜리 후보 하나만 통과


def test_build_all_rejects_when_nothing_approved(client):
    _make_ready_job()
    r = client.post("/api/jobs/build-all", json={"quality": "balanced"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_build_all_invokes_build_output_and_marks_done(client, monkeypatch):
    jid = _make_ready_job()
    client.post(f"/api/jobs/{jid}/approve", json={"approved": [0]})

    calls = []

    def fake_build_output(video, segments, out_path, workdir, **kwargs):
        calls.append({"video": video, "out_path": out_path, "n_segments": len(segments)})

    monkeypatch.setattr(sh, "build_output", fake_build_output)

    r = client.post("/api/jobs/build-all", json={"quality": "balanced"})
    assert r.status_code == 200
    assert r.get_json()["n"] == 1

    # build-all은 백그라운드 스레드에서 실행되므로 완료를 짧게 폴링 대기
    for _ in range(50):
        with jobs.JLOCK:
            status = jobs.JOBS[jid]["status"]
        if status == "done":
            break
        time.sleep(0.05)

    assert status == "done"
    assert len(calls) == 1
    assert calls[0]["n_segments"] == 1


def test_delete_pending_job_removes_it(client):
    jid = jobs.new_job("C:/fake/video2.mp4")
    r = client.post(f"/api/jobs/{jid}/delete", json={})
    assert r.status_code == 200
    assert jid not in jobs.JOBS


def test_delete_active_job_is_deferred(client):
    jid = jobs.new_job("C:/fake/video3.mp4")
    with jobs.JLOCK:
        jobs.JOBS[jid]["status"] = "classifying"
    r = client.post(f"/api/jobs/{jid}/delete", json={})
    assert r.status_code == 200
    assert r.get_json().get("deferred") is True
    assert jid in jobs.JOBS  # 아직 실제로는 삭제되지 않음
    assert jobs.JOBS[jid]["cancel_requested"] is True


def test_cancel_requires_active_status(client):
    jid = jobs.new_job("C:/fake/video4.mp4")  # status="pending"
    r = client.post(f"/api/jobs/{jid}/cancel", json={})
    assert r.status_code == 400
def test_api_job_audio_signal(client, monkeypatch):
    jid = jobs.new_job("C:/fake/video.mp4")
    jobs.JOBS[jid]["workdir"] = "C:/fake/workdir"
    
    dummy_signal = {"times": [0.0, 1.0], "delta": [1.2, 3.4], "threshold": 8.0}
    
    # Mock sh.get_audio_signal
    monkeypatch.setattr(sh, "get_audio_signal", lambda video, workdir, sensitivity: dummy_signal)
    
    r = client.get(f"/api/jobs/{jid}/audio-signal")
    assert r.status_code == 200
    assert r.get_json() == dummy_signal
    
    # Verify cached in memory
    assert jobs.JOBS[jid]["audio_signal"] == dummy_signal


def test_api_job_add_manual_candidate(client):
    jid = jobs.new_job("C:/fake/video.mp4")
    # Set to ready
    jobs.JOBS[jid]["status"] = "ready"
    jobs.JOBS[jid]["approved"] = [0]
    jobs.JOBS[jid]["candidates"] = [
        {"peak": 10.0, "delta_db": 12.0, "highlight": True, "type": "goal"}
    ]
    
    r = client.post(f"/api/jobs/{jid}/candidates/add-manual", json={"peak": 30.0})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["idx"] == 1
    assert body["candidate"]["peak"] == 30.0
    assert body["candidate"]["type"] == "manual"
    
    # Assert added in JOBS
    assert len(jobs.JOBS[jid]["candidates"]) == 2
    assert jobs.JOBS[jid]["candidates"][1]["peak"] == 30.0
    assert jobs.JOBS[jid]["approved"] == [0, 1]
