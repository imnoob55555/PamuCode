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
from prompt_toolkit.layout.processors import AfterInput, PasswordProcessor, Processor
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
from .theme import (
    ACCENT, ACCENT_SOFT, BORDER, DIM, ERROR, INFO, MUTED, PLAN, PROMPT_MARK,
    SUCCESS, TEXT, WARNING,
)
from .thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_COLORS,
    normalize_level,
    style_class,
)

STYLE = Style.from_dict(
    {
        "selected": f"{ACCENT} bold",
        "muted": MUTED,
        "dim": DIM,
        "error": ERROR,
        "notice": WARNING,
        "notice.icon": f"{WARNING} bold",
        "border": BORDER,
        "title": f"{ACCENT} bold",
        "prompt": f"{ACCENT} bold",
        "placeholder": f"{DIM} italic",
        "running": ACCENT_SOFT,
        "menu.name": TEXT,
        "menu.description": MUTED,
        "menu.directory": INFO,
        "status.branch": ACCENT_SOFT,
        "status.context.ok": SUCCESS,
        "status.context.warn": WARNING,
        "status.context.high": ERROR,
        "status.model": TEXT,
        "status.plan": f"{PLAN} bold",
        **{f"thinking-{level}": color for level, color in THINKING_COLORS.items()},
    }
)
NOTICE_SECONDS = 3.0
PLACEHOLDER = "输入消息 · / 命令 · @ 引用文件"
SEPARATOR = " · "


def git_branch(path: Path | None) -> str:
    """Current branch (or short commit) of the repository containing ``path``."""
    if path is None:
        return ""
    for directory in (path, *path.parents):
        marker = directory / ".git"
        try:
            if marker.is_file():
                gitdir = marker.read_text().strip().removeprefix("gitdir:").strip()
                marker = (directory / gitdir).resolve()
            if not marker.is_dir():
                continue
            head = (marker / "HEAD").read_text().strip()
        except (OSError, UnicodeDecodeError):
            return ""
        return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]
    return ""


def display_path(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix() if path != Path.home() else "~"
    except ValueError:
        return str(path)


def _context_class(percent: int) -> str:
    if percent >= 80:
        return "class:status.context.high"
    return "class:status.context.warn" if percent >= 50 else "class:status.context.ok"


def rounded_box(body, *, style="class:border"):
    """Surround ``body`` with a rounded border, one column of inner padding."""

    def edge(left, right):
        return VSplit(
            [
                Window(char=left, width=1, height=1, style=style),
                Window(char="─", height=1, style=style),
                Window(char=right, width=1, height=1, style=style),
            ],
            height=1,
        )

    return HSplit(
        [
            edge("╭", "╮"),
            VSplit(
                [
                    Window(char="│", width=1, style=style),
                    Window(width=1),
                    body,
                    Window(width=1),
                    Window(char="│", width=1, style=style),
                ]
            ),
            edge("╰", "╯"),
        ]
    )


def _hint(text):
    return Window(FormattedTextControl(text), height=1, style="class:muted")


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
        self.branch = ""
        self._entry_index = None
        self.notice = ""
        self._notice_deadline = 0.0
        self._notice_timer = None
        self._active_app = None
        # Standalone `pamu login` keeps its historical one-shot Ctrl+C.
        # The REPL enables the shared two-press gesture before prompting.
        self.repl_mode = False

    def _mode_label(self) -> str:
        return "⏸ plan · 只读" if self.working_mode == "plan" else "⏵ execute"

    def status_fragments(self) -> list[tuple[str, str]]:
        """Right-aligned context, model, thinking level and mode under the prompt box."""
        parts: list[list[tuple[str, str]]] = []
        if self.context_used is not None:
            parts.append([("class:muted", "Context "),
                          (_context_class(self.context_used), f"{self.context_used}%")])
        if self.model_name:
            level = normalize_level(self.thinking_level) or DEFAULT_THINKING_LEVEL
            parts.append([("class:status.model", self.model_name)])
            parts.append([(style_class(level), "thinking off" if level == "off" else level)])
            parts.append([("class:status.plan" if self.working_mode == "plan" else "class:muted",
                           self._mode_label())])
        result: list[tuple[str, str]] = []
        for index, part in enumerate(parts):
            if index:
                result.append(("class:dim", SEPARATOR))
            result.extend(part)
        return result

    def status_text(self) -> str:
        return "".join(text for _, text in self.status_fragments())

    def _status_width(self) -> Dimension:
        width = get_cwidth(self.status_text())
        if self._active_app is not None:
            width = min(width, max(0, self._active_app.output.get_size().columns - 6))
        return Dimension(min=width, preferred=width, max=width)

    def location_fragments(self) -> list[tuple[str, str]]:
        if self.workdir is None:
            return []
        result = [("class:muted", display_path(self.workdir))]
        if self.branch:
            result += [("class:dim", " git:("), ("class:status.branch", self.branch), ("class:dim", ")")]
        return result

    def location_text(self) -> str:
        return "".join(text for _, text in self.location_fragments())

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
            VSplit(
                [
                    Window(width=1),
                    Window(FormattedTextControl("● "), width=2, style="class:notice.icon"),
                    Window(
                        FormattedTextControl(lambda: self.notice),
                        dont_extend_height=True,
                        style="class:notice",
                        wrap_lines=True,
                    ),
                ]
            ),
            Condition(lambda: bool(self.notice)),
        )

    def _run(self, root, bindings, *, focus=None):
        running_shell = ConditionalContainer(
            VSplit(
                [
                    Window(width=1),
                    Window(FormattedTextControl("● "), width=2, style="class:running"),
                    Window(FormattedTextControl(background_shell_status), style="class:muted", wrap_lines=False),
                ],
                height=1,
            ),
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

        for number in range(1, min(len(choices), 9) + 1):

            @bindings.add(str(number))
            def pick(event, _position=number - 1):
                event.app.exit(result=choices[_position][0])

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

        width = len(str(len(choices)))

        def rows():
            result = []
            for position, (_, label) in enumerate(choices):
                selected = position == index
                number = f"{position + 1:>{width}}. "
                if selected:
                    result += [("class:selected", f"{PROMPT_MARK} "), ("class:selected", number),
                               ("class:selected", label)]
                else:
                    result += [("", "  "), ("class:muted", number), ("", label)]
                if position < len(choices) - 1:
                    result.append(("", "\n"))
            return result

        control = FormattedTextControl(
            rows,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=index),
        )
        shortcuts = "↑↓ 选择 · Enter 确认 · Esc 取消"
        if len(choices) > 1:
            shortcuts = f"↑↓ 选择 · Enter 确认 · 1-{min(len(choices), 9)} 快选 · Esc 取消"
        return self._run(
            HSplit(
                [
                    rounded_box(
                        HSplit(
                            [
                                Window(FormattedTextControl(title), height=1, style="class:title"),
                                Window(height=1),
                                Window(control, height=Dimension(max=10), dont_extend_height=True),
                            ]
                        )
                    ),
                    _hint("  " + shortcuts),
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

        processors: list[Processor] = [PasswordProcessor(char="•")] if password else []
        control = BufferControl(buffer, input_processors=processors)
        label = "API Key " if password else ""
        root = HSplit(
            [
                rounded_box(
                    HSplit(
                        [
                            Window(FormattedTextControl(title), height=1, style="class:title"),
                            Window(height=1),
                            VSplit(
                                [
                                    Window(
                                        FormattedTextControl([("class:muted", label), ("class:prompt", f"{PROMPT_MARK} ")]),
                                        dont_extend_width=True,
                                    ),
                                    Window(control),
                                ],
                                height=1,
                            ),
                        ]
                    )
                ),
                _hint("  Ctrl+S 保存 · Esc 返回" if password else "  Enter 确认 · Esc 返回"),
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
            column = max(get_cwidth(item.name) for item in items) + 3
            result = []
            for position, item in enumerate(items):
                selected = position == index % len(items)
                padding = " " * (column - get_cwidth(item.name))
                if selected:
                    result += [("class:selected", f"{PROMPT_MARK} {item.name}{padding}{item.description}")]
                else:
                    result += [("class:menu.name", f"  {item.name}{padding}"),
                               ("class:menu.description", item.description)]
                if item.arguments:
                    result.append(("class:dim", f"  {item.arguments}"))
                result.append(("", "\n"))
            result.append(("class:muted", "  ↑↓ 选择 · Enter 执行选中命令 · Tab 补全 · Esc 关闭"))
            return result

        menu_visible = Condition(lambda: visible() or mention_state() is not None)
        self.branch = git_branch(self.workdir)
        control = BufferControl(
            buffer,
            input_processors=[
                AfterInput(lambda: "" if buffer.text else PLACEHOLDER, style="class:placeholder"),
            ],
        )
        border_style = style_class(self.thinking_level)
        input_row = VSplit(
            [
                Window(
                    FormattedTextControl(f"{PROMPT_MARK} "),
                    width=2,
                    style="class:prompt",
                    dont_extend_height=True,
                ),
                Window(control, wrap_lines=True, dont_extend_height=True),
            ]
        )
        return self._run(
            HSplit(
                [
                    rounded_box(input_row, style=border_style),
                    ConditionalContainer(
                        VSplit(
                            [
                                Window(width=1),
                                Window(
                                    FormattedTextControl(self.location_fragments),
                                    wrap_lines=False,
                                    style="class:muted",
                                ),
                                Window(
                                    FormattedTextControl(self.status_fragments),
                                    width=self._status_width,
                                    align=WindowAlign.RIGHT,
                                    wrap_lines=False,
                                    style="class:muted",
                                ),
                                Window(width=1),
                            ],
                            height=1,
                            padding=1,
                        ),
                        Condition(lambda: self.workdir is not None or bool(self.model_name)) & ~menu_visible,
                    ),
                    ConditionalContainer(
                        VSplit(
                            [
                                Window(width=1),
                                Window(
                                    FormattedTextControl(cast(Any, hints)),
                                    dont_extend_height=True,
                                ),
                            ]
                        ),
                        menu_visible,
                    ),
                ]
            ),
            bindings,
            focus=control,
        )
