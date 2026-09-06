"""youtube_uploader.generate_description()의 챕터 타임스탬프 생성 로직 테스트."""

import youtube_uploader as yt_up


def test_no_goals_shows_placeholder():
    candidates = [
        {"peak": 5.0, "type": "shot", "reason": "슛"},
    ]
    desc = yt_up.generate_description(candidates, approved=[0], video_name="match.mp4")
    assert "(득점 장면 없음)" in desc


def test_goal_timestamps_are_cumulative_not_original():
    # 승인된 각 구간은 pre_sec+post_sec 길이로 하이라이트 영상에 이어붙여지므로,
    # 원본 peak 시각이 아니라 하이라이트 영상 내 누적 위치가 타임스탬프로 나와야 한다.
    candidates = [
        {"peak": 100.0, "type": "goal", "reason": "선제골"},
        {"peak": 500.0, "type": "shot", "reason": "슛"},   # 승인 안 됨
        {"peak": 900.0, "type": "goal", "reason": "쐐기골"},
    ]
    desc = yt_up.generate_description(
        candidates, approved=[0, 2], video_name="match.mp4", pre_sec=8.0, post_sec=5.0)
    lines = desc.splitlines()
    # 첫 구간: 누적 0:00, 두 번째 승인 구간(idx=2, shot 제외하고 goal만 순회하지만
    # cumulative는 approved 순서대로 매 구간마다 seg_dur(13초)씩 누적된다.
    assert "0:00 선제골" in "\n".join(lines)
    assert "(원본 1:40)" in "\n".join(lines)  # peak=100s = 1:40


def test_only_goal_type_becomes_chapter():
    candidates = [
        {"peak": 10.0, "type": "save", "reason": "선방"},
        {"peak": 20.0, "type": "goal", "reason": "득점"},
    ]
    desc = yt_up.generate_description(candidates, approved=[0, 1], video_name="match.mp4")
    assert "선방" not in desc
    assert "득점" in desc


def test_reason_fallback_when_missing():
    candidates = [{"peak": 10.0, "type": "goal", "reason": ""}]
    desc = yt_up.generate_description(candidates, approved=[0], video_name="match.mp4")
    assert "득점 #1" in desc


def test_footer_and_title_present():
    desc = yt_up.generate_description([], approved=[], video_name="2026-07-05_경기.mp4")
    assert desc.startswith("축구 하이라이트 — 2026-07-05_경기")
    assert "HLEditor" in desc


def test_get_credentials_refresh_error():
    import json
    from unittest.mock import MagicMock, patch

    from google.auth.exceptions import RefreshError

    token_data = {
        "token": "old_token",
        "refresh_token": "some_refresh_token",
        "client_id": "client_id",
        "client_secret": "client_secret",
    }

    with patch("youtube_uploader.TOKEN_FILE") as mock_token_file:
        mock_token_file.exists.return_value = True
        mock_token_file.read_text.return_value = json.dumps(token_data)

        with patch("google.oauth2.credentials.Credentials") as mock_creds_class:
            mock_creds = MagicMock()
            mock_creds.expired = True
            mock_creds.refresh_token = "some_refresh_token"
            mock_creds.refresh.side_effect = RefreshError("invalid_grant")
            mock_creds_class.return_value = mock_creds

            with patch("youtube_uploader.revoke") as mock_revoke:
                res = yt_up._get_credentials()
                assert res is None
                mock_revoke.assert_called_once()


def test_token_roundtrip_preserves_expiry(tmp_path, monkeypatch):
    """expiry를 저장하지 않으면 creds.expired가 항상 False가 되어
    만료된 액세스 토큰으로 업로드를 시작하게 된다."""
    from datetime import timedelta
    from unittest.mock import MagicMock

    monkeypatch.setattr(yt_up, "TOKEN_FILE", tmp_path / "token.json")
    expiry = (yt_up._utcnow_naive() + timedelta(days=1)).replace(microsecond=0)

    creds = MagicMock()
    creds.token = "at"
    creds.refresh_token = "rt"
    creds.token_uri = "https://oauth2.googleapis.com/token"
    creds.client_id = "cid"
    creds.client_secret = "sec"
    creds.scopes = yt_up.SCOPES
    creds.expiry = expiry
    yt_up._save_token(creds)

    assert "expiry" in yt_up.TOKEN_FILE.read_text(encoding="utf-8")
    loaded = yt_up._get_credentials()
    assert loaded.expiry == expiry     # 만료 시각이 복원돼야 refresh 판단이 가능하다
    assert loaded.expired is False


def test_transient_refresh_failure_keeps_token(tmp_path, monkeypatch):
    """네트워크 순단으로 토큰 갱신이 실패해도 refresh token을 지우면 안 된다.
    (지우면 일시적 장애 한 번에 매번 재인증을 하게 된다)"""
    import json
    from unittest.mock import MagicMock, patch

    from google.auth.exceptions import TransportError

    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({
        "token": "at", "refresh_token": "rt",
        "client_id": "cid", "client_secret": "sec",
    }), encoding="utf-8")
    monkeypatch.setattr(yt_up, "TOKEN_FILE", token_file)

    with patch("google.oauth2.credentials.Credentials") as mock_cls:
        creds = MagicMock()
        creds.expired = True
        creds.refresh_token = "rt"
        creds.refresh.side_effect = TransportError("getaddrinfo failed")
        mock_cls.return_value = creds
        with patch("youtube_uploader.revoke") as mock_revoke:
            res = yt_up._get_credentials()
    assert res is creds          # 기존 토큰으로 계속 시도
    mock_revoke.assert_not_called()
    assert token_file.exists()


def test_auth_status_network_error_reports_ok_and_keeps_token():
    """인증 확인이 네트워크 문제로 실패하면 미인증으로 단정하지 않는다.
    단정해 버리면 원스톱이 업로드 단계를 조용히 건너뛴다."""
    from unittest.mock import MagicMock, patch

    with patch.object(type(yt_up.TOKEN_FILE), "exists", return_value=True), \
         patch("youtube_uploader._get_credentials", return_value=MagicMock()), \
         patch("googleapiclient.discovery.build", side_effect=ConnectionError("timed out")), \
         patch("youtube_uploader.revoke") as mock_revoke:
        st = yt_up.auth_status()
    assert st["ok"] is True
    assert st["reason"] == "network"
    mock_revoke.assert_not_called()


def test_auth_status_401_revokes_token():
    """진짜 인증 오류(401)일 때만 토큰을 폐기한다."""
    from unittest.mock import MagicMock, patch

    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = 401
    err = HttpError(resp, b'{"error": "unauthorized"}')

    with patch.object(type(yt_up.TOKEN_FILE), "exists", return_value=True), \
         patch("youtube_uploader._get_credentials", return_value=MagicMock()), \
         patch("googleapiclient.discovery.build", side_effect=err), \
         patch("youtube_uploader.revoke") as mock_revoke:
        st = yt_up.auth_status()
    assert st["ok"] is False
    assert st["reason"] == "unauthorized"
    mock_revoke.assert_called_once()


def test_quota_error_does_not_revoke_token():
    """403(할당량 초과)은 토큰 문제가 아니므로 폐기 대상이 아니다."""
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = 403
    assert yt_up._is_hard_auth_error(HttpError(resp, b'{"error": "quotaExceeded"}')) is False


def test_upload_video_refresh_error():
    from unittest.mock import MagicMock, patch

    import pytest
    from google.auth.exceptions import RefreshError

    with patch("youtube_uploader._get_credentials") as mock_get_creds:
        mock_creds = MagicMock()
        mock_get_creds.return_value = mock_creds

        with patch("googleapiclient.discovery.build") as mock_build:
            mock_yt = MagicMock()
            mock_build.return_value = mock_yt

            mock_req = MagicMock()
            mock_yt.videos().insert.return_value = mock_req
            mock_req.next_chunk.side_effect = RefreshError("invalid_grant")

            with patch("youtube_uploader.revoke") as mock_revoke:
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_size = 100
                    with patch("googleapiclient.http.MediaFileUpload"):
                        with pytest.raises(RuntimeError) as exc_info:
                            yt_up.upload_video(
                                video_path="dummy.mp4",
                                title="Test Video",
                                description="Test Desc"
                            )
                        assert "YouTube 인증이 만료되었거나 취소되었습니다" in str(exc_info.value)
                        mock_revoke.assert_called_once()

