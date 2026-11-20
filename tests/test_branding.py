from agent_app.branding import LOGO, render_startup_screen


def test_startup_shows_brand_and_hints_without_duplicate_status():
    text = render_startup_screen()
    for row in LOGO.split("\n"):
        assert row in text
    for value in ("PamuCode · AI Coding Agent", "/help", "/quit", "@ 引用文件"):
        assert value in text
    for label in ("工作目录：", "Provider：", "模型：", "状态："):
        assert label not in text
    assert "\x1b" not in text
    lines = text.rstrip("\n").split("\n")
    assert lines[0].startswith("╭") and lines[-1].startswith("╰")


def test_branding_shrinks_on_narrow_terminals_and_supports_color(monkeypatch):
    text = render_startup_screen(width=30)
    assert "\x1b" not in text
    assert "PamuCode" in text
    assert "██" not in text
    assert all(len(line) <= 30 for line in text.split("\n"))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    colored = render_startup_screen(color=True)
    assert "\x1b[" in colored and "PamuCode · AI Coding Agent" in colored
