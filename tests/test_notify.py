"""kaggle_runner.notify — ntfy 알림 payload와 실패 격리."""

import json

from kaggle_runner.notify import MAX_MESSAGE_BYTES, Notifier, build_payload, truncate_utf8


def test_payload_without_click_so_tap_copies_message():
    p = build_payload("hl-topic", "1경기 완료", "본문", tags=["soccer"])
    assert p == {"topic": "hl-topic", "title": "1경기 완료", "message": "본문", "tags": ["soccer"]}


def test_payload_with_click():
    assert build_payload("t", "제목", "본문", click="https://x")["click"] == "https://x"


def test_truncate_keeps_valid_utf8_within_limit():
    text = "가" * 3000                       # 9000바이트
    out = truncate_utf8(text, MAX_MESSAGE_BYTES)
    assert len(out.encode("utf-8")) <= MAX_MESSAGE_BYTES
    assert out.endswith("…")
    assert truncate_utf8("짧음", 100) == "짧음"


class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_send_posts_json_to_ntfy():
    seen = {}

    def post(url, data, headers, timeout):
        seen.update(url=url, body=json.loads(data.decode("utf-8")), headers=headers)
        return _Resp(200)

    n = Notifier("hl-topic", post=post, log=lambda m: None)
    assert n.send("제목", "본문") is True
    assert seen["url"] == "https://ntfy.sh/"
    assert seen["body"]["topic"] == "hl-topic"
    assert seen["headers"]["Content-Type"] == "application/json"


def test_send_failure_never_raises():
    def post(*a, **k):
        raise ConnectionError("offline")

    logs = []
    n = Notifier("hl-topic", post=post, log=logs.append)
    assert n.send("제목", "본문") is False
    assert "알림 실패" in logs[0]


def test_http_error_reported_as_false():
    n = Notifier("hl-topic", post=lambda *a, **k: _Resp(500), log=lambda m: None)
    assert n.send("제목", "본문") is False


def test_dry_run_and_missing_topic_do_not_post():
    def post(*a, **k):
        raise AssertionError("보내면 안 됨")

    logs = []
    assert Notifier("hl-topic", dry_run=True, post=post, log=logs.append).send("제목", "본문")
    assert Notifier("", post=post, log=logs.append).send("제목", "본문")
    assert len(logs) == 2
