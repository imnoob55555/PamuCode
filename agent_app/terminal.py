"""Inline terminal prompts, local setup menus and slash suggestions."""

from __future__ import annotations

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window, ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import BeforeInput, PasswordProcessor
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from .slash_commands import match_commands


STYLE = Style.from_dict(
    {
        "selected": "bg:#164e63 #ffffff bold",
        "muted": "#888888",
        "prompt": "#00aaaa",
        "error": "#ff6666",
    }
)


class TerminalUI:
    def __init__(self, *, input=None, output=None):
        self.input = input
        self.output = output
        self.history = InMemoryHistory()

    def _run(self, root, bindings, *, focus=None):
        app = Application(
            layout=Layout(root, focused_element=focus),
            key_bindings=bindings,
            input=self.input,
            output=self.output,
            style=STYLE,
            full_screen=False,
            erase_when_done=True,
        )
        # Bare Escape remains usable without slowing down arrow sequences.
        app.ttimeoutlen = 0.05
        with patch_stdout(raw=True):
            return app.run()

    def choose(self, title: str, choices: list[tuple[str, str]]) -> str | None:
        if not choices:
            return None
        index = 0
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
        error = ""
        bindings = KeyBindings()

        @bindings.add("c-s" if password else "enter")
        def accept(event):
            nonlocal error
            value = buffer.text.strip()
            if not value:
                error = "请输入 API Key。" if password else "请输入模型 ID。"
                return
            try:
                if on_save:
                    on_save(value)
            except OSError:
                error = "保存失败，请检查凭证文件的写入权限或磁盘空间后重试。"
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
            event.app.exit(exception=KeyboardInterrupt())

        processors = [PasswordProcessor()] if password else []
        processors.append(BeforeInput("API Key: " if password else "Model ID: "))
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
                ConditionalContainer(
                    Window(
                        FormattedTextControl(lambda: error),
                        height=2,
                        style="class:error",
                    ),
                    Condition(lambda: bool(error)),
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
        bindings = KeyBindings()

        def changed(_):
            nonlocal index, hidden
            index, hidden = 0, False

        buffer.on_text_changed += changed
        visible = Condition(lambda: buffer.text.lstrip().startswith("/") and not hidden)

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
            nonlocal hidden
            hidden = True

        @bindings.add("enter")
        def accept(event):
            if visible():
                complete_selection(submit=True)
            buffer.append_to_history()
            event.app.exit(result=buffer.text)

        @bindings.add("c-c")
        def interrupt(event):
            event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("c-d")
        def eof(event):
            event.app.exit(exception=EOFError())

        def hints():
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

        control = BufferControl(
            buffer, input_processors=[BeforeInput([("class:prompt", ">> ")])]
        )
        return self._run(
            HSplit(
                [
                    Window(control, height=1),
                    ConditionalContainer(
                        Window(FormattedTextControl(hints), dont_extend_height=True),
                        visible,
                    ),
                ]
            ),
            bindings,
            focus=control,
        )
