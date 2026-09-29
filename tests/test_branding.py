from agent_app.branding import render_startup_screen


def test_ready_screen_uses_actual_state_without_ansi(tmp_path):
    cwd = str(tmp_path / "project")
    text = render_startup_screen(
        cwd=cwd, provider="deepseek", model="chosen-model", color=False
    )
    for value in (
        "PamuCode",
        cwd,
        "deepseek",
        "chosen-model",
        "已准备就绪",
        "/help",
        "/quit",
    ):
        assert value in text
    assert "\x1b" not in text


def test_unconfigured_and_error_states_are_not_ready(tmp_path):
    for failed in (False, True):
        text = render_startup_screen(
            cwd=str(tmp_path / "project"),
            provider=None,
            model=None,
            configuration_error=failed,
        )
        assert "/login" in text
        assert "已准备就绪" not in text
        assert ("配置异常" if failed else "尚未配置") in text


def test_branding_sanitizes_control_characters_and_shrinks_on_narrow_terminals(
    tmp_path,
):
    text = render_startup_screen(
        cwd=str(tmp_path / "\x1b[31mproject"),
        provider="deepseek",
        model="model\x1b[0m",
        width=30,
    )
    assert "\x1b" not in text
    assert "PamuCode · AI Coding Agent" in text
    assert "____" not in text
