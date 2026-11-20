"""Inline terminal prompts, local setup menus and slash suggestions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic
from typing import Any, cast

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import BeforeInput, PasswordProcessor, Processor
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from .cli_exit import ExitGesture, QuitRequested
from .cli_progress import background_shell_status, watch_shell_status
from .file_mentions import (
    MATCH_LIMIT,
    complete_mention,
    match_entries,
    mention_token,
    render_matches,
    workspace_entries,
)
from .slash_commands import match_commands
from .thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_COLORS,
    normalize_level,
    style_class,
)

STYLE = Style.from_dict(
    {
        "selected": "bg:#164e63 #ffffff bold",
        "muted": "#888888",
        "error": "#ff6666",
        **{f"thinking-{level}": color for level, color in THINKING_COLORS.items()},
    }
)
NOTICE_SECONDS = 3.0


def model_status(model: str, level: str) -> str:
    """Footer label under the prompt box, formatted like pi's status line."""
    if not model:
        return ""
    level = normalize_level(level) or DEFAULT_THINKING_LEVEL
    return f"{model} • {'thinking off' if level == 'off' else level}"


class TerminalUI:
    def __init__(self, *, input=None, output=None, exit_gesture=None, workdir=None):
        self.input = input
        self.output = output
        self.history = InMemoryHistory()
        self.exit_gesture = exit_gesture or ExitGesture()
        self.model_name = ""
        self.thinking_level = "off"
        self.working_mode = "execute"
        self.context_used: int | None = None
        self.workdir = Path(workdir) if workdir is not None else None
        self._entry_index = None
        self.notice = ""
        self._notice_deadline = 0.0
        self._notice_timer = None
        self._active_app = None
        # Standalone `pamu login` keeps its historical one-shot Ctrl+C.
        # The REPL enables the shared two-press gesture before prompting.
        self.repl_mode = False

    def status_text(self) -> str:
        """Right-aligned context, model and mode under the prompt box."""
        status = model_status(self.model_name, self.thinking_level)
        if status:
            status += f" • {'plan · 只读' if self.working_mode == 'plan' else 'execute'}"
        if self.context_used is None:
            return status
        usage = f"Context used: {self.context_used}%"
        return f"{usage} • {status}" if status else usage

    def notify(self, text):
        self.notice = text
        self._notice_deadline = monotonic() + NOTICE_SECONDS
        if self._active_app is not None:
            self._schedule_notice_expiry()
            self._active_app.invalidate()

    def _cancel_notice_timer(self):
        if self._notice_timer is not None:
            self._notice_timer.cancel()
            self._notice_timer = None

    def _schedule_notice_expiry(self):
        self._cancel_notice_timer()
        if not self.notice:
            return
        remaining = self._notice_deadline - monotonic()
        if remaining <= 0:
            self._expire_notice()
        else:
            self._notice_timer = asyncio.get_running_loop().call_later(
                remaining, self._expire_notice
            )

    def _expire_notice(self):
        self.notice = ""
        self._notice_timer = None
        if self._active_app is not None:
            self._active_app.invalidate()

    def _workspace_entries(self):
        if self.workdir is None:
            return []
        if self._entry_index is None:
            self._entry_index = workspace_entries(self.workdir)
        return self._entry_index

    def _notice_window(self):
        return ConditionalContainer(
            Window(
                FormattedTextControl(lambda: self.notice),
                dont_extend_height=True,
                style="class:error",
                wrap_lines=True,
            ),
            Condition(lambda: bool(self.notice)),
        )

    def _run(self, root, bindings, *, focus=None):
        running_shell = ConditionalContainer(
            Window(FormattedTextControl(background_shell_status), height=1, style="class:muted", wrap_lines=False),
            Condition(lambda: bool(background_shell_status(mark_visible=False))),
        )
        app = Application(
            layout=Layout(HSplit([running_shell, self._notice_window(), root]), focused_element=focus),
            key_bindings=bindings,
            input=self.input,
            output=self.output,
            style=STYLE,
            full_screen=False,
            erase_when_done=True,
        )
        # Bare Escape remains usable without slowing down arrow sequences.
        app.ttimeoutlen = 0.05
        def start_notice_timer():
            self._active_app = app
            self._schedule_notice_expiry()

        app.pre_run_callables.append(start_notice_timer)
        try:
            with patch_stdout(raw=True), watch_shell_status(app.invalidate):
                return app.run()
        finally:
            self._cancel_notice_timer()
            self._active_app = None

    def choose(
        self,
        title: str,
        choices: list[tuple[str, str]],
        *,
        initial: str | None = None,
    ) -> str | None:
        if not choices:
            return None
        keys = [key for key, _ in choices]
        index = keys.index(initial) if initial in keys else 0
        bindings = KeyBindings()

        @bindings.add("up")
        def up(event):
            nonlocal index
            index = (index - 1) % len(choices)

        @bindings.add("down")
        def down(event):
            nonlocal index
            index = (index + 1) % len(choices)

        @bindings.add("enter")
        def accept(event):
            event.app.exit(result=choices[index][0])

        @bindings.add("escape")
        @bindings.add("c-c")
        @bindings.add("c-d")
        def cancel(event):
            if event.key_sequence[0].key == "c-c" and self.repl_mode:
                if self.exit_gesture.press():
                    event.app.exit(exception=QuitRequested())
                    return
                self.notify("再次按 Ctrl+C 退出 PamuCode")
                event.app.exit(result=None)
                return
            event.app.exit(result=None)

        def rows():
            result = []
            for position, (_, label) in enumerate(choices):
                selected = position == index
                result.append(
                    (
                        "class:selected" if selected else "",
                        f"{'❯' if selected else ' '} {label}",
                    )
                )
                if position < len(choices) - 1:
                    result.append(("", "\n"))
            return result

        control = FormattedTextControl(
            rows,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=index),
        )
        return self._run(
            HSplit(
                [
                    Window(FormattedTextControl(title), height=1),
                    Window(control, height=Dimension(max=10), dont_extend_height=True),
                    Window(
                        FormattedTextControl("↑↓ 选择 · Enter 确认 · Esc 取消"),
                        height=1,
                        style="class:muted",
                    ),
                ]
            ),
            bindings,
            focus=control,
        )

    def secret(self, title: str, *, on_save) -> str | None:
        return self._read(title, password=True, on_save=on_save)

    def text(self, title: str) -> str | None:
        return self._read(title)

    def _read(self, title, *, password=False, on_save=None):
        buffer = Buffer(multiline=False)
        bindings = KeyBindings()

        @bindings.add("c-s" if password else "enter")
        def accept(event):
            value = buffer.text.strip()
            if not value:
                self.notify("请输入 API Key。" if password else "请输入内容。")
                return
            try:
                if on_save:
                    on_save(value)
            except OSError:
                self.notify("保存失败，请检查凭证文件的写入权限或磁盘空间后重试。")
                return
            event.app.exit(result=value)

        if password:

            @bindings.add("enter")
            def keep_editing(event):
                pass

        @bindings.add("escape")
        def cancel(event):
            event.app.exit(result=None)

        @bindings.add("c-c")
        @bindings.add("c-d")
        def abort(event):
            if event.key_sequence[0].key == "c-c" and self.repl_mode:
                if self.exit_gesture.press():
                    event.app.exit(exception=QuitRequested())
                else:
                    event.app.exit(result=None)
                return
            event.app.exit(exception=KeyboardInterrupt())

        processors: list[Processor] = [PasswordProcessor()] if password else []
        processors.append(BeforeInput("API Key: " if password else "输入: "))
        control = BufferControl(buffer, input_processors=processors)
        root = HSplit(
            [
                Window(FormattedTextControl(title), height=1),
                Window(control, height=1),
                Window(
                    FormattedTextControl(
                        "Ctrl+S 保存 · Esc 返回"
                        if password
                        else "Enter 确认 · Esc 返回"
                    ),
                    height=1,
                    style="class:muted",
                ),
            ]
        )
        try:
            return self._run(root, bindings, focus=control)
        finally:
            buffer.reset()  # Secret buffers have no shared history.

    def prompt(self) -> str:
        buffer = Buffer(multiline=False, history=self.history)
        index = 0
        hidden = False
        mention_index = 0
        mention_hidden = False
        self._entry_index = None
        bindings = KeyBindings()

        def changed(_):
            nonlocal index, hidden, mention_index, mention_hidden
            index, hidden = 0, False
            mention_index, mention_hidden = 0, False
            self.exit_gesture.reset()

        def cursor_changed(_):
            nonlocal mention_index
            mention_index = 0

        buffer.on_text_changed += changed
        buffer.on_cursor_position_changed += cursor_changed

        def slash_command():
            return buffer.text.lstrip().startswith("/")

        visible = Condition(lambda: slash_command() and not hidden)

        mention_cache: dict[str, Any] = {"key": None, "value": None}

        def mention_state():
            key = (buffer.text, buffer.cursor_position, mention_hidden)
            if mention_cache["key"] == key:
                return mention_cache["value"]
            state = None
            if not mention_hidden and not slash_command():
                token = mention_token(buffer.text, buffer.cursor_position)
                if token is not None:
                    ranked = match_entries(token.query, self._workspace_entries())
                    if ranked:
                        state = (ranked[:MATCH_LIMIT], len(ranked))
            mention_cache["key"] = key
            mention_cache["value"] = state
            return state

        mention_visible = Condition(lambda: mention_state() is not None)

        # Browse whole user messages, including from a multiline pasted draft.
        # Active suggestion menus retain their own arrow-key navigation.
        history_visible = ~visible & ~mention_visible

        @bindings.add('up', filter=history_visible)
        def previous_message(event):
            nonlocal hidden, mention_hidden
            buffer.history_backward(count=event.arg)
            hidden = mention_hidden = True

        @bindings.add('down', filter=history_visible)
        def next_message(event):
            nonlocal hidden, mention_hidden
            buffer.history_forward(count=event.arg)
            hidden = mention_hidden = True

        @bindings.add("up", filter=mention_visible)
        @bindings.add("down", filter=mention_visible)
        def move_mention(event):
            nonlocal mention_index
            state = mention_state()
            if state is not None:
                mention_index = (
                    mention_index
                    + (-1 if event.key_sequence[0].key == "up" else 1)
                ) % len(state[0])

        def apply_mention():
            state = mention_state()
            if state is None:
                return
            matches, _ = state
            path = matches[mention_index % len(matches)]
            text, position = complete_mention(
                buffer.text, buffer.cursor_position, path
            )
            buffer.text = text
            buffer.cursor_position = position

        @bindings.add("tab", filter=mention_visible)
        def complete_file(event):
            apply_mention()

        @bindings.add("up", filter=visible)
        @bindings.add("down", filter=visible)
        def move(event):
            nonlocal index
            items = match_commands(buffer.text)
            if items:
                index = (
                    index + (-1 if event.key_sequence[0].key == "up" else 1)
                ) % len(items)

        def complete_selection(*, submit=False):
            items = match_commands(buffer.text)
            if items:
                item = items[index % len(items)]
                parts = buffer.text.lstrip().split(maxsplit=1)
                suffix = ""
                if item.arguments:
                    suffix = (
                        " " + parts[1] if len(parts) > 1 else ("" if submit else " ")
                    )
                buffer.text = item.name + suffix
                buffer.cursor_position = len(buffer.text)

        @bindings.add("tab", filter=visible)
        def complete(event):
            complete_selection()

        @bindings.add("escape")
        def hide(event):
            nonlocal hidden, mention_hidden
            if mention_state() is not None:
                mention_hidden = True
            else:
                hidden = True

        @bindings.add("enter")
        def accept(event):
            if mention_visible():
                apply_mention()
                return
            if visible():
                complete_selection(submit=True)
            self.exit_gesture.reset()
            self.notice = ""
            buffer.append_to_history()
            event.app.exit(result=buffer.text)

        @bindings.add("c-c")
        def interrupt(event):
            if buffer.text:
                buffer.text = ""
            if self.exit_gesture.press():
                event.app.exit(exception=QuitRequested())
            else:
                self.notify("再次按 Ctrl+C 退出 PamuCode")

        @bindings.add("c-d")
        def eof(event):
            event.app.exit(exception=EOFError())

        def hints() -> list[tuple[str, str]]:
            state = mention_state()
            if state is not None:
                matches, total = state
                return render_matches(matches, total, mention_index)
            items = match_commands(buffer.text)
            if not items:
                return [("class:muted", "  无匹配命令")]
            result = []
            for position, item in enumerate(items):
                selected = position == index % len(items)
                result.append(
                    (
                        "class:selected" if selected else "",
                        f"{'❯' if selected else ' '} {item.usage:<23} {item.description}",
                    )
                )
                result.append(("", "\n"))
            result.append(("class:muted", "  ↑↓ 选择 · Enter 执行选中命令 · Tab 补全"))
            return result

        control = BufferControl(buffer)
        border_style = style_class(self.thinking_level)
        return self._run(
            HSplit(
                [
                    Window(char="─", height=1, style=border_style),
                    Window(control, height=1),
                    Window(char="─", height=1, style=border_style),
                    ConditionalContainer(
                        VSplit(
                            [
                                Window(
                                    FormattedTextControl(lambda: str(self.workdir) if self.workdir is not None else ""),
                                    wrap_lines=False,
                                    style="class:muted",
                                ),
                                Window(
                                    FormattedTextControl(self.status_text),
                                    width=lambda: Dimension(
                                        min=0,
                                        preferred=get_cwidth(self.status_text()),
                                        max=get_cwidth(self.status_text()),
                                    ),
                                    align=WindowAlign.RIGHT,
                                    wrap_lines=False,
                                    style="class:muted",
                                ),
                            ],
                            height=1,
                            padding=1,
                        ),
                        Condition(lambda: self.workdir is not None or bool(self.model_name)),
                    ),
                    ConditionalContainer(
                        Window(
                            FormattedTextControl(cast(Any, hints)),
                            dont_extend_height=True,
                        ),
                        Condition(lambda: visible() or mention_state() is not None),
                    ),
                ]
            ),
            bindings,
            focus=control,
        )
