from pathlib import Path
from types import SimpleNamespace

from agent_app.file_mentions import (
    Mention,
    complete_mention,
    format_mention,
    match_entries,
    mention_token,
    render_matches,
    workspace_entries,
)


def test_mention_token_detects_a_word_token():
    assert mention_token("@term", 5) == Mention(0, 5, "term")


def test_mention_token_reads_a_bare_at_sign():
    assert mention_token("@", 1) == Mention(0, 1, "")


def test_mention_token_accepts_cjk_prefix_without_space():
    text = "看看@term"
    assert mention_token(text, len(text)) == Mention(2, 7, "term")


def test_mention_token_rejects_email_and_spaced_token():
    assert mention_token("user@example.com", 16) is None
    assert mention_token("x @foo bar", len("x @foo bar")) is None


def test_mention_token_ends_at_whitespace_when_cursor_is_inside():
    text = "@terminal.py is here"
    assert mention_token(text, 5) == Mention(0, 12, "term")


def test_mention_token_reads_quoted_paths():
    text = '@"a b.md"'
    assert mention_token(text, 4) == Mention(0, 9, "a ")


def test_mention_token_rejects_cursor_after_a_closed_quote():
    assert mention_token('@"a b" x', 6) is None


def test_mention_token_returns_none_without_at_sign():
    assert mention_token("hello", 5) is None
    assert mention_token("@term", 0) is None


def test_format_mention_quotes_paths_with_spaces():
    assert format_mention("a/b.py") == "@a/b.py"
    assert format_mention("a b.md") == '@"a b.md"'
    assert format_mention("a b/") == '@"a b/"'


def test_complete_mention_replaces_token_and_keeps_separator():
    text, cursor = complete_mention("@term rest", 5, "agent_app/terminal.py")
    assert text == "@agent_app/terminal.py rest"
    assert cursor == len("@agent_app/terminal.py") + 1


def test_complete_mention_quotes_space_paths_and_inserts_mid_text():
    text, cursor = complete_mention("看 @my file", 4, "docs/my file.md")
    assert text == '看 @"docs/my file.md" file'
    assert cursor == len('看 @"docs/my file.md"') + 1


def test_complete_mention_appends_separator_at_end_of_buffer():
    text, cursor = complete_mention("@term", 5, "terminal.py")
    assert text == "@terminal.py "
    assert cursor == len(text)


def test_complete_mention_keeps_directories_open_for_drilling():
    text, cursor = complete_mention("@sub", 4, "sub/")
    assert text == "@sub/"
    assert cursor == len(text)


def test_complete_mention_quotes_space_directories_without_separator():
    text, cursor = complete_mention("@my", 3, "my dir/")
    assert text == '@"my dir/"'
    assert cursor == len(text)


def test_complete_mention_without_token_is_a_noop():
    assert complete_mention("hello", 5, "a.py") == ("hello", 5)


FILES = [
    "agent_app/terminal.py",
    "tests/test_terminal.py",
    "terminal.md",
    "src/deep/terminal_helper.py",
    "agent_app/core/loop.py",
]


def test_match_entries_ranks_top_level_before_nested():
    assert match_entries("terminal", FILES) == [
        "terminal.md",
        "agent_app/terminal.py",
        "src/deep/terminal_helper.py",
        "tests/test_terminal.py",
    ]


def test_match_entries_prefers_current_directory_over_deeper_matches():
    entries = ["deep/deeper/terminal.py", "terminal_notes.md"]
    assert match_entries("term", entries) == [
        "terminal_notes.md",
        "deep/deeper/terminal.py",
    ]


def test_match_entries_prefers_exact_basename():
    assert match_entries("loop.py", FILES) == ["agent_app/core/loop.py"]


def test_match_entries_falls_back_to_path_substring():
    assert match_entries("core/loop", FILES) == ["agent_app/core/loop.py"]


def test_match_entries_is_case_insensitive():
    assert match_entries("TERM", FILES)[0] == "terminal.md"


def test_match_entries_empty_query_ranks_top_level_first():
    ranked = match_entries("", FILES)
    assert ranked[0] == "terminal.md"
    assert set(ranked) == set(FILES)


def test_match_entries_honours_limit():
    assert match_entries("terminal", FILES, limit=2) == [
        "terminal.md",
        "agent_app/terminal.py",
    ]


ENTRIES = [
    "agent_app/",
    "agent_app/core/",
    "agent_app/core/loop.py",
    "agent_app/terminal.py",
    "docs/",
    "docs/notes.md",
    "terminal.md",
]


def test_match_entries_typed_directory_lists_direct_children_first():
    assert match_entries("agent_app/", ENTRIES) == [
        "agent_app/core/",
        "agent_app/terminal.py",
        "agent_app/core/loop.py",
    ]


def test_match_entries_typed_directory_beats_other_paths():
    entries = [
        "x/agent_app/tool.py",
        "agent_app/tool.py",
        "agent_app/tools/registry.py",
    ]
    assert match_entries("agent_app/tool", entries) == [
        "agent_app/tool.py",
        "agent_app/tools/registry.py",
        "x/agent_app/tool.py",
    ]


def test_match_entries_ignores_the_typed_directory_itself():
    assert match_entries("agent_app/", ["agent_app/"]) == []


def test_workspace_entries_prefers_git_listing_and_keeps_real_files(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"keep.txt\0missing.txt\0")

    assert workspace_entries(tmp_path, run=run) == ["keep.txt"]
    assert commands[0][:2] == ["git", "-C"]
    assert Path(commands[0][2]) == tmp_path.resolve()
    assert "ls-files" in commands[0]


def test_workspace_entries_derives_directories_from_git_files(tmp_path):
    (tmp_path / "agent_app" / "core").mkdir(parents=True)
    (tmp_path / "agent_app" / "terminal.py").write_text("x")
    (tmp_path / "agent_app" / "core" / "loop.py").write_text("x")

    def run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=b"agent_app/terminal.py\0agent_app/core/loop.py\0",
        )

    assert workspace_entries(tmp_path, run=run) == [
        "agent_app/",
        "agent_app/core/",
        "agent_app/core/loop.py",
        "agent_app/terminal.py",
    ]


def test_workspace_entries_falls_back_to_walk_when_git_fails(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("x")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x")
    (tmp_path / "secret.pem").write_text("x")

    def run(command, **kwargs):
        return SimpleNamespace(returncode=128, stdout=b"")

    assert workspace_entries(tmp_path, run=run) == [
        "docs/",
        "docs/notes.md",
        "src/",
        "src/app.py",
    ]


def test_workspace_entries_falls_back_when_git_cannot_start(tmp_path):
    (tmp_path / "a.py").write_text("x")

    def run(command, **kwargs):
        raise FileNotFoundError("git")

    assert workspace_entries(tmp_path, run=run) == ["a.py"]


def test_walk_fallback_respects_limit(tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x")

    def run(command, **kwargs):
        return SimpleNamespace(returncode=128, stdout=b"")

    assert workspace_entries(tmp_path, run=run, limit=2) == ["a.py", "b.py"]


def test_render_matches_marks_selection_and_reports_truncation():
    rows = render_matches(["a.py", "b.py"], total=12, index=1)
    text = "".join(part for _, part in rows)
    assert "❯ b.py" in text
    assert " a.py" in text
    assert "匹配 12 个，显示前 2 个" in text
    assert "Enter 选中" in text


def test_render_matches_handles_no_matches():
    assert render_matches([], 0, 0) == []
