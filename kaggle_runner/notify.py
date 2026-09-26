"""ntfy 푸시 알림 (https://ntfy.sh).

아이폰 ntfy 앱은 click URL이 없는 메시지를 탭하면 본문을 복사한다. 그래서 완료 알림은
click 없이 BAND 글 전체를 본문으로 보낸다(MOBILE_PLAN.md F8).
알림이 실패해도 처리 파이프라인은 멈추지 않는다.
"""

from __future__ import annotations

import json

NTFY_URL = "https://ntfy.sh/"
MAX_MESSAGE_BYTES = 4096   # ntfy 메시지 한도. 넘으면 첨부파일로 바뀌므로 잘라서 보낸다


def truncate_utf8(text: str, max_bytes: int) -> str:
    """UTF-8 바이트 기준으로 자른다(한글이 중간에 깨지지 않게)."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[: max_bytes - 3].decode("utf-8", errors="ignore") + "…"


def build_payload(topic: str, title: str, message: str, *,
                  tags: list[str] | None = None, click: str | None = None) -> dict:
    payload = {
        "topic": topic,
        "title": title,
        "message": truncate_utf8(message, MAX_MESSAGE_BYTES),
    }
    if tags:
        payload["tags"] = list(tags)
    if click:
        payload["click"] = click
    return payload


class Notifier:
    """ntfy 발송기. dry_run이거나 topic이 없으면 보내지 않고 로그로만 남긴다."""

    def __init__(self, topic: str, *, dry_run: bool = False, post=None, log=print):
        self.topic = topic or ""
        self.dry_run = dry_run or not self.topic
        self._post = post
        self._log = log
        self.sent: list[dict] = []   # 보낸(또는 dry-run으로 출력한) payload 기록 — 요약·테스트용

    def send(self, title: str, message: str, *, tags: list[str] | None = None,
             click: str | None = None) -> bool:
        payload = build_payload(self.topic, title, message, tags=tags, click=click)
        self.sent.append(payload)
        if self.dry_run:
            self._log(f"[알림 dry-run] {title}\n{payload['message']}"
                      + (f"\n(click: {click})" if click else ""))
            return True
        try:
            post = self._post
            if post is None:
                import requests
                post = requests.post
            resp = post(NTFY_URL, data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, timeout=15)
            ok = 200 <= resp.status_code < 300
            if not ok:
                self._log(f"[알림 실패] HTTP {resp.status_code}: {title}")
            return ok
        except Exception as e:  # 알림 실패로 처리를 멈추지 않는다
            self._log(f"[알림 실패] {e!r}: {title}")
            return False
