"""roboflow 호스팅 추론 모델 REST 클라이언트.

`inference` 패키지는 이 환경(Python 3.14)에서 설치 불가(휠 미지원, 상한 <3.12~3.13)
이므로 `requests`로 REST API를 직접 호출한다. 응답 JSON은 `inference` 패키지가
반환하는 것과 동일한 스키마이며, `supervision`의 `Detections.from_inference()` /
`KeyPoints.from_inference()`는 원래 이 dict를 그대로 받아들이도록 설계돼 있어
패키지 유무와 무관하게 동일하게 동작한다.

한 잡이 API를 수천 번 호출하므로(프레임당 2회) 일시적 오류 한 번에 전체 잡이
죽지 않도록 타임아웃·연결 오류·429/5xx는 지수 백오프로 재시도한다. 실측: 60초
클립 분석 25분 진행 중 ReadTimeout 1회로 잡 전체가 실패한 사례(2026-07-20).
"""
from __future__ import annotations

import base64
import re
import time

import cv2
import numpy as np
import requests

_INFER_URL = "https://detect.roboflow.com/{model_id}"
# 재시도 대상: 레이트리밋(429)·서버측 일시 오류(5xx). 401/403/404 등은 즉시 전파.
_RETRY_STATUS = {429, 500, 502, 503, 504}


class RoboflowAuthError(RuntimeError):
    """API 키가 없거나 거부됨(401/403) — 재시도해도 소용없는 영구 오류."""


class RoboflowTransientError(RuntimeError):
    """네트워크·서버 일시 오류. 재시도를 모두 소진했지만 키 문제는 아니다."""


def _redact(msg: str, api_key: str | None) -> str:
    """오류 메시지에 섞여 나오는 API 키를 가린다(요청 URL에 쿼리로 실림)."""
    if api_key:
        msg = msg.replace(api_key, "***")
    return re.sub(r"(api_key=)[^&\s\"']+", r"\1***", msg)


def infer(
    frame_bgr: np.ndarray,
    model_id: str,
    api_key: str | None,
    confidence: float = 0.0,
    timeout: float = 30.0,
    retries: int = 6,
) -> dict:
    """한 프레임을 roboflow 호스팅 모델에 보내고 원본 JSON 응답(dict)을 반환.

    일시적 실패(타임아웃·연결 오류·429/5xx)는 retries회까지 1→2→4→8→15→15초
    백오프로 재시도한다. 모두 소진하면 RoboflowTransientError(호출부가 프레임
    건너뛰기로 흡수), 401/403이면 RoboflowAuthError(즉시 중단)를 던진다.
    """
    if not api_key:
        raise RoboflowAuthError("Roboflow API 키가 없습니다 (.env의 ROBOFLOW_API_KEY 확인)")
    ok, buf = cv2.imencode(".jpg", frame_bgr)
    if not ok:
        raise RuntimeError("프레임 JPEG 인코딩 실패")
    b64 = base64.b64encode(buf).decode("ascii")

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                _INFER_URL.format(model_id=model_id),
                params={"api_key": api_key, "confidence": confidence, "format": "json"},
                data=b64,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
        else:
            if resp.status_code in (401, 403):
                raise RoboflowAuthError(
                    f"Roboflow가 인증을 거부했습니다(HTTP {resp.status_code}). "
                    ".env의 ROBOFLOW_API_KEY가 올바른지, 사용량 한도를 넘지 않았는지 확인하세요."
                )
            if resp.status_code not in _RETRY_STATUS:
                resp.raise_for_status()  # 그 밖의 4xx는 즉시 전파
                return resp.json()
            last_exc = RuntimeError(f"HTTP {resp.status_code} {resp.reason}")
        if attempt < retries:
            time.sleep(min(2 ** attempt, 15))
    raise RoboflowTransientError(
        f"Roboflow API {retries + 1}회 연속 실패(네트워크/서버 일시 문제): "
        f"{_redact(str(last_exc), api_key)}"
    )
