"""soccer_highlights._resolve_title_font — 워터마크 폰트 경로 결정 규칙."""

import soccer_highlights as sh


def test_env_override_wins(monkeypatch):
    monkeypatch.setattr(sh.config, "get_env", lambda k, d="": "/fonts/custom.ttf" if k == "HL_TITLE_FONT" else d)
    assert sh._resolve_title_font() == "/fonts/custom.ttf"


def test_first_existing_candidate_is_used(monkeypatch):
    monkeypatch.setattr(sh.config, "get_env", lambda k, d="": d)
    monkeypatch.setattr(sh, "_TITLE_FONT_CANDIDATES", ["/nope/a.ttf", "/yes/b.ttf", "/yes/c.ttf"])
    monkeypatch.setattr(sh.os.path, "exists", lambda p: p.startswith("/yes/"))
    assert sh._resolve_title_font() == "/yes/b.ttf"


def test_falls_back_to_first_candidate_when_none_exist(monkeypatch):
    # 아무 폰트도 없으면 첫 후보를 돌려주고, build_output이 워터마크만 생략한다(빌드는 계속)
    monkeypatch.setattr(sh.config, "get_env", lambda k, d="": d)
    monkeypatch.setattr(sh, "_TITLE_FONT_CANDIDATES", ["/nope/a.ttf", "/nope/b.ttf"])
    monkeypatch.setattr(sh.os.path, "exists", lambda p: False)
    assert sh._resolve_title_font() == "/nope/a.ttf"
