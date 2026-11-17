from agent_app.branding import LOGO, render_startup_screen


def test_startup_shows_brand_and_hints_without_duplicate_status():
    text = render_startup_screen()
    assert LOGO in text
    for value in ("PamuCode", "/help", "/quit"):
        assert value in text
    for label in ("工作目录：", "Provider：", "模型：", "状态："):
        assert label not in text
    assert "\x1b" not in text


def test_branding_shrinks_on_narrow_terminals_and_supports_color():
    text = render_startup_screen(width=30)
    assert "\x1b" not in text
    assert "PamuCode · AI Coding Agent" in text
    assert "____" not in text
    assert "\x1b[36m" in render_startup_screen(color=True)
