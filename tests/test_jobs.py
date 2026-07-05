"""jobs.py의 순수 로직(신뢰도 플래그 분류) 유닛 테스트."""

import json
import os
import time

import config
import jobs
import soccer_highlights as sh


class TestFlag:
    def test_no_vision_always_auto(self):
        assert jobs.flag({}, conf_auto=0.7, vision_used=False) == "auto"
        assert jobs.flag({"highlight": False}, conf_auto=0.7, vision_used=False) == "auto"

    def test_high_confidence_is_auto(self):
        c = {"highlight": True, "confidence": 0.9}
        assert jobs.flag(c, conf_auto=0.7, vision_used=True) == "auto"

    def test_mid_confidence_is_maybe(self):
        c = {"highlight": True, "confidence": 0.5}
        assert jobs.flag(c, conf_auto=0.7, vision_used=True) == "maybe"

    def test_low_confidence_is_reject(self):
        c = {"highlight": True, "confidence": 0.1}
        assert jobs.flag(c, conf_auto=0.7, vision_used=True) == "reject"

    def test_not_highlight_is_reject_regardless_of_confidence(self):
        c = {"highlight": False, "confidence": 0.99}
        assert jobs.flag(c, conf_auto=0.7, vision_used=True) == "reject"

    def test_boundary_at_conf_auto(self):
        c = {"highlight": True, "confidence": 0.7}
        assert jobs.flag(c, conf_auto=0.7, vision_used=True) == "auto"

    def test_boundary_at_conf_maybe(self):
        c = {"highlight": True, "confidence": sh.CONF_MAYBE}
        assert jobs.flag(c, conf_auto=0.7, vision_used=True) == "maybe"


class TestNewJob:
    def test_creates_job_with_expected_defaults(self):
        jid = jobs.new_job("C:/fake/video.mp4", sensitivity="strict", workers=3)
        try:
            job = jobs.JOBS[jid]
            assert job["video_name"] == "video.mp4"
            assert job["sensitivity"] == "strict"
            assert job["workers"] == 3
            assert job["status"] == "pending"
            assert job["cancel_requested"] is False
            assert job["pending_delete"] is False
        finally:
            jobs.JOBS.pop(jid, None)

    def test_update_ignores_missing_job(self):
        # 삭제된(존재하지 않는) jid에 대한 update는 예외 없이 조용히 무시돼야 한다.
        jobs.update("no-such-jid", status="error")  # 예외가 나지 않으면 통과


class TestRestoreRecentResults:
    def _write_results_json(self, results_dir, jid, video_path, mtime=None):
        payload = {
            "video": str(video_path),
            "duration": 42.0,
            "vision_used": True,
            "params": {"PRE_SEC": 8.0, "POST_SEC": 5.0},
            "candidates": [{"peak": 5.0, "highlight": True, "confidence": 0.9}],
        }
        path = results_dir / f"results_{jid}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_restores_recent_result_as_ready_job(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        self._write_results_json(tmp_path, "abc12345", video)

        n = jobs.restore_recent_results(max_age_hours=24)
        try:
            assert n == 1
            assert "abc12345" in jobs.JOBS
            job = jobs.JOBS["abc12345"]
            assert job["status"] == "ready"
            assert job["vision_used"] is True
            assert len(job["candidates"]) == 1
            assert job["workdir"] and os.path.isdir(job["workdir"])
        finally:
            job = jobs.JOBS.pop("abc12345", None)
            if job and job.get("workdir"):
                import shutil
                shutil.rmtree(job["workdir"], ignore_errors=True)

    def test_skips_results_older_than_max_age(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        old_time = time.time() - 48 * 3600  # 48시간 전
        self._write_results_json(tmp_path, "oldjid01", video, mtime=old_time)

        n = jobs.restore_recent_results(max_age_hours=24)
        assert n == 0
        assert "oldjid01" not in jobs.JOBS

    def test_skips_when_original_video_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        self._write_results_json(tmp_path, "novideo1", tmp_path / "gone.mp4")

        n = jobs.restore_recent_results(max_age_hours=24)
        assert n == 0
        assert "novideo1" not in jobs.JOBS

    def test_does_not_duplicate_already_loaded_job(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        video = tmp_path / "video.mp4"
        video.write_bytes(b"fake")
        self._write_results_json(tmp_path, "dup00001", video)

        jobs.JOBS["dup00001"] = {"status": "building"}  # 이미 처리 중인 것처럼 시뮬레이션
        try:
            n = jobs.restore_recent_results(max_age_hours=24)
            assert n == 0
            assert jobs.JOBS["dup00001"]["status"] == "building"  # 덮어쓰지 않음
        finally:
            jobs.JOBS.pop("dup00001", None)


class TestDeleteResultsFile:
    def test_removes_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        path = tmp_path / "results_xyz.json"
        path.write_text("{}", encoding="utf-8")
        jobs.delete_results_file("xyz")
        assert not path.exists()

    def test_missing_file_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
        jobs.delete_results_file("does-not-exist")  # 예외 없이 통과해야 함
