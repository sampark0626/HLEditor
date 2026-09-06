"""한 경기가 여러 파일로 나뉜 경우(XbotGo 30분 자동 분할) 파트 병합 기능 검증.

- soccer_highlights.concat_videos(): concat 목록 파일 작성 + ffmpeg 호출 형태
- jobs.new_job(source_videos=...): 병합 잡의 필드 세팅
- /api/jobs/add: {jobs:[{videos:[a,b]}]} → 병합 잡 1개 (하이라이트도 1개)
"""

import pytest

import app as app_module
import jobs
import soccer_highlights as sh


def _mkfile(path, data=b"x"):
    path.write_bytes(data)
    return str(path)


# ─── concat_videos ──────────────────────────────────────────
def test_concat_videos_writes_list_and_calls_ffmpeg_copy(tmp_path, monkeypatch):
    p1 = tmp_path / "1부.mp4"
    p2 = tmp_path / "2부.mp4"
    _mkfile(p1)
    _mkfile(p2)
    calls = []
    monkeypatch.setattr(sh, "run", lambda cmd, cwd=None: calls.append(cmd) or "")

    out = tmp_path / "merged.mp4"
    sh.concat_videos([p1, p2], out, workdir=tmp_path)

    listfile = (tmp_path / "merge_parts.txt").read_text(encoding="utf-8").splitlines()
    assert listfile == [f"file '{p1.as_posix()}'", f"file '{p2.as_posix()}'"]
    assert len(calls) == 1
    assert calls[0][0] == "ffmpeg"
    assert "-c" in calls[0] and "copy" in calls[0]
    assert str(out) in calls[0]


def test_concat_videos_falls_back_to_reencode_when_copy_fails(tmp_path, monkeypatch):
    p1 = tmp_path / "1.mp4"
    p2 = tmp_path / "2.mp4"
    _mkfile(p1)
    _mkfile(p2)
    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(cmd)
        if len(calls) == 1:
            raise RuntimeError("command failed: ffmpeg...")
        return ""

    monkeypatch.setattr(sh, "run", fake_run)
    sh.concat_videos([p1, p2], tmp_path / "m.mp4", workdir=tmp_path)

    assert len(calls) == 2                    # copy 시도 → 재인코딩 폴백
    assert "libx264" in calls[1]


def test_concat_videos_requires_two_parts(tmp_path):
    p1 = tmp_path / "only.mp4"
    _mkfile(p1)
    with pytest.raises(ValueError):
        sh.concat_videos([p1], tmp_path / "m.mp4")


def test_concat_videos_missing_part_raises(tmp_path):
    p1 = tmp_path / "there.mp4"
    _mkfile(p1)
    with pytest.raises(FileNotFoundError):
        sh.concat_videos([p1, tmp_path / "gone.mp4"], tmp_path / "m.mp4")


# ─── new_job(source_videos=...) ─────────────────────────────
def test_new_job_marks_merge_and_keeps_first_part_name(tmp_path):
    a = _mkfile(tmp_path / "경기_1부.mp4")
    b = _mkfile(tmp_path / "경기_2부.mp4")
    jid = jobs.new_job(None, source_videos=[a, b])
    try:
        job = jobs.JOBS[jid]
        assert job["needs_merge"] is True
        assert job["source_videos"] == [a, b]
        assert job["video_name"] == "경기_1부.mp4"          # 표시 이름은 첫 파트
        assert job["video"].endswith(f"{jid}.mp4")          # 병합 결과 경로
        assert "_merged" in job["video"].replace("\\", "/")
        assert jobs.job_summary(job)["n_parts"] == 2
    finally:
        jobs.JOBS.pop(jid, None)


def test_new_job_single_source_is_not_a_merge(tmp_path):
    a = _mkfile(tmp_path / "solo.mp4")
    jid = jobs.new_job(None, source_videos=[a])
    try:
        job = jobs.JOBS[jid]
        assert job["needs_merge"] is False
        assert job["video_name"] == "solo.mp4"
    finally:
        jobs.JOBS.pop(jid, None)


# ─── /api/jobs/add 병합 경로 ────────────────────────────────
@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
    with jobs.JLOCK:
        jobs.JOBS.clear()
    while not jobs.JOB_QUEUE.empty():
        jobs.JOB_QUEUE.get_nowait()


def test_add_with_videos_list_creates_one_merge_job(client, tmp_path):
    a = _mkfile(tmp_path / "part1.mp4")
    b = _mkfile(tmp_path / "part2.mp4")
    r = client.post("/api/jobs/add",
                    json={"jobs": [{"videos": [a, b], "sensitivity": "normal"}]})
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["added"]) == 1
    assert not body["errors"]

    job = jobs.JOBS[body["added"][0]]
    assert job["needs_merge"] is True
    assert job["source_videos"] == [a, b]


def test_add_rejects_merge_group_with_missing_part(client, tmp_path):
    a = _mkfile(tmp_path / "ok.mp4")
    r = client.post("/api/jobs/add",
                    json={"jobs": [{"videos": [a, str(tmp_path / "nope.mp4")]}]})
    body = r.get_json()
    assert body["added"] == []
    assert body["errors"]


def test_add_dedupes_part_already_in_active_merge_job(client, tmp_path):
    a = _mkfile(tmp_path / "p1.mp4")
    b = _mkfile(tmp_path / "p2.mp4")
    first = client.post("/api/jobs/add", json={"jobs": [{"videos": [a, b]}]})
    assert len(first.get_json()["added"]) == 1
    # 같은 파트를 단일 파일로 다시 추가하려 하면 중복으로 건너뛴다
    again = client.post("/api/jobs/add", json={"jobs": [{"video": a}]})
    body = again.get_json()
    assert body["added"] == []
    assert body["skipped"]
