# PamuCode

PamuCode is a minimal‑baseline Agent implementation similar to Codex and Claude Code.

## Setup

PamuCode requires [Git](https://git-scm.com/) and
[UV](https://docs.astral.sh/uv/getting-started/installation/). Choose the
instructions for your operating system.

### macOS / Linux

Install UV if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal if the installer asks you to refresh `PATH`, then run:

```bash
git clone https://github.com/imnoob55555/PamuCode.git
cd PamuCode
uv sync
mkdir -p ~/.pamu
if [ ! -e ~/.pamu/.settings ]; then
  cp .settings.example ~/.pamu/.settings
fi
uv tool install --editable .
uv tool update-shell
```

Run `pamu` and enter `/login` to configure your provider interactively.
You can also configure `ANTHROPIC_API_KEY` and `MODEL_ID` in `~/.pamu/.settings`.
Restart the terminal after `uv tool update-shell` before running `pamu`.

### Windows (PowerShell)

Install UV if it is not already available:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new PowerShell window if the installer asks you to refresh `PATH`, then
run:

```powershell
git clone https://github.com/imnoob55555/PamuCode.git
Set-Location PamuCode
uv sync
New-Item -ItemType Directory -Force -Path "$HOME\.pamu" | Out-Null
if (!(Test-Path "$HOME\.pamu\.settings")) {
    Copy-Item -Path .settings.example -Destination "$HOME\.pamu\.settings"
}
uv tool install --editable .
uv tool update-shell
```

Run `pamu` and enter `/login` to configure your provider interactively.
You can also configure `ANTHROPIC_API_KEY` and `MODEL_ID` in `$HOME\.pamu\.settings`.
Restart PowerShell after `uv tool update-shell` before running `pamu`.

The editable install means later changes to this checkout are used directly by
the global `pamu` command.

Configuration is loaded in this priority order:

1. Environment variables already set in the process.
2. `<project>/.pamu/.settings`, for project-specific overrides.
3. `~/.pamu/.settings`, for user-wide defaults.

The first value found wins, so a project configuration can override the global
default while an explicitly exported environment variable overrides both.

### Migrating from `.env`

PamuCode no longer reads `~/.config/pamucode/.env` or
`<project>/.pamu/.env`. Move an existing configuration explicitly before
upgrading. Do not overwrite a `.settings` file that already exists.

On macOS or Linux:

```bash
mkdir -p ~/.pamu
if [ -e ~/.pamu/.settings ]; then
  echo "Refusing to overwrite ~/.pamu/.settings" >&2
elif [ -e ~/.config/pamucode/.env ]; then
  mv ~/.config/pamucode/.env ~/.pamu/.settings
else
  echo "No legacy config found at ~/.config/pamucode/.env" >&2
fi
# Run inside a project only when it has a project-specific configuration:
if [ -e .pamu/.settings ]; then
  echo "Refusing to overwrite .pamu/.settings" >&2
elif [ -e .pamu/.env ]; then
  mv .pamu/.env .pamu/.settings
else
  echo "No legacy config found at .pamu/.env" >&2
fi
```

On Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.pamu" | Out-Null
if (Test-Path "$HOME\.pamu\.settings") {
    throw "Refusing to overwrite $HOME\.pamu\.settings"
} elseif (Test-Path "$HOME\.config\pamucode\.env") {
    Move-Item "$HOME\.config\pamucode\.env" "$HOME\.pamu\.settings"
} else {
    Write-Output "No legacy config found at $HOME\.config\pamucode\.env"
}
# Run inside a project only when it has a project-specific configuration:
if (Test-Path ".pamu\.settings") {
    throw "Refusing to overwrite .pamu\.settings"
} elseif (Test-Path ".pamu\.env") {
    Move-Item ".pamu\.env" ".pamu\.settings"
} else {
    Write-Output "No legacy config found at .pamu\.env"
}
```

## Run

```bash
cd /path/to/project
pamu
```

While waiting for a model response, interactive terminals show an animated
`Working` status. Redirected and other non-interactive output stays clean.

### 日志（Logging）

每次运行会自动在 `<项目>/.pamu/logs/pamu.log` 写入旋转日志（单文件 2MB，
保留 5 份），记录轮次起止、工具调用、压缩与恢复事件、供应商/模型切换等。
在 `.settings` 中设置 `LOG_LEVEL=DEBUG/INFO/WARNING/ERROR` 可调整级别。
终端输出不变，日志仅为事后排查通道，且被 `.pamu/.gitignore` 屏蔽，不会入库。

### 模型供应商登录与切换

除环境变量外，PamuCode 支持多供应商账户登录，凭证保存在
`~/.pamu/credentials.json`（仅属主可读，0600）：

启动只检查本地保存的 API Key 记录及已有配置，不发送验证请求。
首次启动没有配置时，输入区会提示输入 `/login`：

1. 输入 `/login`，用 ↑↓ 选择 provider，按 Enter 确认。
2. 输入 API Key（遮罩显示），按 **Ctrl+S** 保存到本地。
3. 用 ↑↓ 选择模型，按 Enter 保存并进入会话。也可选择“自定义模型 ID”。

模型菜单使用内置列表，不联网查询账户权限。保存 Key 后取消模型选择会保留
Key；可重新 `/login` 配置，未启动模型会话时也可用 `/model` 继续选择。
Esc 返回或取消当前步骤，Ctrl+C 取消 Key 输入并回到对话输入区。
API Key 不进入输入历史或日志；凭证通过原子替换写入，写入失败可以重试。

```bash
pamu login [provider]    # provider → API Key → 模型，交互终端离线保存
pamu logout [provider]   # 登出一个或全部供应商
pamu status               # 查看当前供应商与已登录账户
pamu use <provider>       # 设定下次启动使用的默认供应商
```

内置供应商（均提供 Anthropic 兼容端点）：`anthropic`、`deepseek`、`moonshot`、
`zhipu`、`openrouter`。登录后无需再配置 `MODEL_ID`/`ANTHROPIC_API_KEY`。

REPL 会话内支持热切换，无需重启：

```
/login             列出 provider，配置 API Key 并选择模型
/status            查看当前供应商、模型与登录状态
/model             列出当前 provider 的模型，选择后切换
/provider          列出 provider，选择后切换（需先配置 Key）
/help              查看命令列表
```

输入 `/` 后，输入区下方即时展示命令及简短说明；继续输入命令名称或说明关键词
会实时过滤。↑↓ 选择建议、Enter 直接执行高亮命令；Tab 只补全，Esc 收起建议。
`/login` 无需参数，执行后先列出支持 Anthropic 格式 API 的 provider，
↑↓ 高亮并按 Enter 选择，再输入 Key、Ctrl+S 保存，最后选择模型。
`/model` 和 `/provider` 同样无需输入参数：执行命令后显示选项菜单，
↑↓ 选择、Enter 确认，Esc 取消。provider 菜单标出当前供应商与 Key 配置状态；
选择尚未配置 Key 的供应商时会提示先执行 `/login`。
普通文本输入隐藏建议，并保留上下键历史导航。

非交互终端保留普通行输入；`pamu login` 的行输入模式保留原有在线验证行为，
可加 `--no-verify` 离线保存。键盘菜单和实时提示需要交互终端。

同一 provider 的 Key 优先级：进程环境变量 > 项目 `.settings` > 全局
`.settings` > 已保存凭证；启动和会话内切换均遵循此顺序。登录时选择的模型
保存为该 provider 的默认模型；无登录模型记录时保留原有 `MODEL_ID` 配置方式。
验收清单见 `docs/ACCEPTANCE.md`。

PamuCode uses the current directory as the project workspace. Its local runtime
state is stored beneath `.pamu/`, including `memory/`, `transcripts/`,
`task_outputs/`, `tasks/`, `mailboxes/`, `worktrees/`, and
`scheduled_tasks.json`. The state directory contains its own protective
`.gitignore`; it should remain untracked by the project repository.

Static symlink and path validation prevents accidental state escape, but
PamuCode is not a sandbox against the workspace owner concurrently mutating
paths while it runs. This boundary does not weaken file-tool workspace
containment.

Legacy top-level state directories and files are not migrated automatically.
They are left in place so you can review or remove them deliberately.

## Test

```bash
uv run pytest
```

The implementation is organized under `agent_app/adapters`, `core`, `features`,
and `tools`. `agent_app/bootstrap.py` is the composition root and
`agent_app/cli.py` owns the command-line lifecycle.
