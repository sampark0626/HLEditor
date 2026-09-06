"""dotplay/roboflow_client.py — HTTP 상태별 처리 및 API 키 마스킹 검증.

핵심: 402(Payment Required)는 재시도해도 소용없는 영구 오류(크레딧 소진)로
즉시 RoboflowAuthError를 던지고, 어떤 오류 메시지에도 api_key 원문이 남지 않는다.
"""

import numpy as np
import pytest
import requests

from dotplay import roboflow_client as rc

API_KEY = "SECRET_KEY_abc123"


class _Resp:
    def __init__(self, status, reason="", payload=None):
        self.status_code = status
        self.reason = reason
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error: {self.reason} for url: "
                f"https://detect.roboflow.com/m/1?api_key={API_KEY}&format=json"
            )


@pytest.fixture
def frame():
    return np.zeros((16, 16, 3), np.uint8)


def test_402_raises_auth_error_without_retry(frame, monkeypatch):
    calls = []
    monkeypatch.setattr(rc.requests, "post",
                        lambda *a, **k: calls.append(1) or _Resp(402, "Payment Required"))
    monkeypatch.setattr(rc.time, "sleep", lambda *_: None)

    with pytest.raises(rc.RoboflowAuthError) as ei:
        rc.infer(frame, "m/1", API_KEY, retries=6)

    assert len(calls) == 1                       # 재시도 없음
    assert "402" in str(ei.value)
    assert API_KEY not in str(ei.value)          # 키 노출 없음


def test_generic_4xx_error_message_is_redacted(frame, monkeypatch):
    monkeypatch.setattr(rc.requests, "post", lambda *a, **k: _Resp(400, "Bad Request"))
    monkeypatch.setattr(rc.time, "sleep", lambda *_: None)

    with pytest.raises(requests.HTTPError) as ei:
        rc.infer(frame, "m/1", API_KEY, retries=2)

    assert API_KEY not in str(ei.value)
    assert "api_key=***" in str(ei.value)


def test_401_still_raises_auth_error(frame, monkeypatch):
    monkeypatch.setattr(rc.requests, "post", lambda *a, **k: _Resp(401, "Unauthorized"))
    with pytest.raises(rc.RoboflowAuthError):
        rc.infer(frame, "m/1", API_KEY, retries=1)


def test_success_returns_payload(frame, monkeypatch):
    monkeypatch.setattr(rc.requests, "post",
                        lambda *a, **k: _Resp(200, payload={"predictions": []}))
    assert rc.infer(frame, "m/1", API_KEY) == {"predictions": []}
