<div align="center">

# PamuCode

**A layered terminal coding-agent runtime — small enough to read end to end.**

分层架构的终端 AI 编码代理运行时 · 从一次按键到工具调用，整条链路都可追读

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![uv](https://img.shields.io/badge/deps-uv-de5fe9?logo=astral&logoColor=white)](https://docs.astral.sh/uv/)
[![UI](https://img.shields.io/badge/UI-prompt--toolkit-7f52ff)](https://python-prompt-toolkit.readthedocs.io/)
[![API](https://img.shields.io/badge/API-Messages%20%7C%20Responses%20%7C%20Chat%20%7C%20Ollama-d97757)](#内置供应商)
[![Tests](https://img.shields.io/badge/tests-pytest-0a7d91)](tests)
[![License](https://img.shields.io/badge/license-MIT-3da639)](LICENSE)

</div>

PamuCode gives an AI model a practical project workspace: file tools, a shell, an interactive
REPL, persistent memory, a task board, background work and provider switching. Independent
adapters support Anthropic Messages, OpenAI Responses, Chat Completions and native Ollama. The runtime stays
layered so you can follow a single prompt from keypress to streamed tokens, tool calls, state
updates and the final answer.

PamuCode 把「一个能改代码的终端助手」拆成可逐层阅读的几块：输入层（prompt-toolkit）、
agent 循环、模型适配器、工具执行与状态特性。它不追求功能堆叠，而是让每一层的职责、状态和
失败恢复路径都写在代码里 —— 因此它既是能日常使用的编码代理，也是一份可以完整读完的参考实现。

> [!NOTE]
> 当前版本 `0.1.0`，定位为实验性项目：适合自用、二次开发和架构学习。请在使用前阅读
> [安全与隐私](#安全与隐私)，并在敏感工作区中确认工具权限与供应商配置。

## 目录

- [简介](#简介)
- [主要特性](#主要特性)
- [快速开始](#快速开始)
- [首次运行](#首次运行)
- [配置](#配置)
- [命令行](#命令行)
- [REPL 交互](#repl-交互)
- [工具](#工具)
- [运行时状态](#运行时状态)
- [架构](#架构)
- [开发与测试](#开发与测试)
- [安全与隐私](#安全与隐私)
- [许可证](#许可证)

## 简介

```text
  ____    _    __  __ _   _
 |  _ \  / \  |  \/  | | | |
 | |_) |/ _ \ | |\/| | | | |
 |  __// ___ \| |  | | |_| |
 |_|  /_/   \_\_|  |_|\___/

 PamuCode · AI Coding Agent
```

工作方式与 Claude Code、Codex 一类工具相同：在项目目录里敲 `pamu`，用自然语言描述任务，
代理会自己读文件、跑命令、改代码，并在上下文变长时压缩、在请求失败时重试或续写。区别在于
它足够小：`agent_app/` 下每个包只负责一件事，`bootstrap.py` 是唯一的组装点。

## 主要特性

**模型与供应商**

- 支持 Anthropic Messages、OpenAI Responses、OpenAI Chat Completions、Ollama 原生协议，
  均支持流式文本、原生工具调用及结果回传；现有五家供应商继续使用 Messages。
- 可连接自定义网关和本地推理服务；Ollama 免密钥，兼容服务的密钥可选。
- `/login` 离线完成供应商与模型选择，密钥写入 `~/.pamu/credentials.json`（仅本人可读）。
- `/model`、`/provider` 运行时切换，无需重启；`FALLBACK_MODEL_ID` 用于当前供应商内的过载回退。
- Messages 后端保留 `/thinking` 五档设置；新增协议首版只接受 `off`。

**终端体验**

- 输入框由上下两条横线包住，没有左右边框；横线颜色随思考级别变化。
- 右下角一行灰色状态：`模型 • 级别`（`off` 时显示 `thinking off`）。
- `@` 文件补全：优先读 Git 索引（含未跟踪但未忽略的文件，遵守 `.gitignore`），目录带 `/` 可继续下钻，
  含空格的路径自动补成 `@"a b.md"`。
- `/` 命令补全：`↑↓` 选择、`Tab` 补全、`Enter` 执行、`Esc` 关闭菜单。
- 斜杠命令不进模型上下文、不进输入历史；空输入被忽略，`1` 秒内连按两次 `Ctrl+C` 退出。
- 流式输出 + 工作指示器、瞬时错误重试、超长续写、上下文压缩与错误恢复。

**Agent 能力**

- 内置工具：`bash`、`read_file`、`write_file`、`edit_file`、`glob`、`load_skill`、`compact`。
- 项目记忆写入 `.pamu/memory/MEMORY.md`，按 user / feedback / project / reference 四类归档。
- 技能放在 `skills/<name>/SKILL.md`，带 YAML front matter，按需 `load_skill` 载入。
- 任务板（支持 `blockedBy` 依赖）、会话 TODO、cron 定时任务、后台命令。
- 同步子代理 `task`、团队协作（派生队友 / 收发消息 / 计划评审）、git worktree 隔离。
- MCP 连接、Hooks（`UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop`）。

## 快速开始

前置条件：Python 3.13 及以上、Git、[uv](https://docs.astral.sh/uv/)。

### macOS / Linux

```bash
git clone https://github.com/imnoob55555/PamuCode.git
cd PamuCode
uv sync

mkdir -p ~/.pamu
[ -e ~/.pamu/.settings ] || cp .settings.example ~/.pamu/.settings

uv tool install --editable .
uv tool update-shell
pamu
```

### Windows PowerShell

```powershell
git clone https://github.com/imnoob55555/PamuCode.git
Set-Location PamuCode
uv sync

New-Item -ItemType Directory -Force -Path "$HOME\.pamu" | Out-Null
if (!(Test-Path "$HOME\.pamu\.settings")) {
    Copy-Item .settings.example "$HOME\.pamu\.settings"
}

uv tool install --editable .
uv tool update-shell
pamu
```

`pamu` 以**当前目录**为工作区，运行时状态写在 `<当前目录>/.pamu/`。缺少必需的服务配置或模型时，
启动后 REPL 会提示先 `/login`。

## 首次运行

```text
  ____    _    __  __ _   _
 |  _ \  / \  |  \/  | | | |
 | |_) |/ _ \ | |\/| | | | |
 |  __// ___ \| |  | | |_| |
 |_|  /_/   \_\_|  |_|\___/

PamuCode · AI Coding Agent

工作目录：/path/to/your/project
Provider：未配置
模型：未配置
状态：尚未配置模型服务，请输入 /login

输入问题开始对话，输入 /help 查看命令
输入 /quit 或在 1 秒内连按两次 Ctrl+C 退出
```

输入 `/login` 选择供应商并完成配置。现有供应商输入密钥后选模型；OpenAI 输入密钥和模型 ID；
兼容服务选择协议、地址、是否使用密钥及模型；Ollama 只需地址和已安装模型 ID。
终端菜单完全离线，不查询模型列表、不发验证请求。非 TTY 的命令行登录会按选定地址、协议和模型验证，
使用 `--no-verify` 可跳过校验：

```bash
pamu login openai
pamu login openai-compatible --no-verify
pamu login ollama --no-verify
```

## 配置

配置文件按优先级从高到低：

1. 进程中已有的环境变量
2. 项目设置 `<工作区>/.pamu/.settings`
3. 用户设置 `~/.pamu/.settings`
4. 已保存的供应商凭证 `~/.pamu/credentials.json`

高优先级覆盖低优先级；项目设置可以覆盖用户级默认值，显式导出的环境变量又能覆盖两者。
`PAMU_PROVIDER` 覆盖已保存的当前供应商；`MODEL_ID` 覆盖保存的模型。旧设置中若保留了
`MODEL_ID`，登录时选择的新模型会在下次启动被它覆盖；清空该项即可使用保存值。
会话内 `/model` 和 `/provider` 立即生效，下一次启动重新按优先级解析；`/status` 和 `pamu status`
显示有效模型及来源。切换时已有队友继续使用启动它们的模型与客户端。
`.settings.example` 是提交在仓库里的模板，所有值保持为空：

```dotenv
PAMU_PROVIDER=
ANTHROPIC_API_KEY=
MODEL_ID=
FALLBACK_MODEL_ID=
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_COMPATIBLE_BASE_URL=
OPENAI_COMPATIBLE_PROTOCOL=
OPENAI_COMPATIBLE_API_KEY=
OLLAMA_BASE_URL=
```

| 变量 | 说明 |
| --- | --- |
| `PAMU_PROVIDER` | 可选，明确选择供应商入口；否则使用已保存选择，初始默认为 Anthropic |
| `MODEL_ID` | 必填，主模型 ID（通过 `/login` 登录供应商时会自动提供） |
| `FALLBACK_MODEL_ID` | 可选，连续过载时回退；必须属于当前服务和协议，不做跨供应商回退 |
| `ANTHROPIC_API_KEY` | Anthropic 密钥；其他供应商用各自的 `*_API_KEY` |
| `<供应商>_BASE_URL` | 可选，覆盖服务地址，如 `ANTHROPIC_BASE_URL`、`DEEPSEEK_BASE_URL`、`OPENAI_BASE_URL` |
| `OPENAI_COMPATIBLE_PROTOCOL` | `openai_chat`（默认）或 `openai_responses`，必须与服务支持的接口一致 |
| `OPENAI_COMPATIBLE_BASE_URL` | 兼容服务必填，通常包含 `/v1`；密钥变量为可选的 `OPENAI_COMPATIBLE_API_KEY` |
| `OLLAMA_BASE_URL` | 默认 `http://localhost:11434`；使用原生 `/api/chat`，不填写 `/v1` |
| `LOG_LEVEL` | 日志级别，写入 `.pamu/logs/pamu.log`，默认 `INFO` |

### 内置供应商

| 供应商 | Base URL | 默认模型 | 密钥变量 |
| --- | --- | --- | --- |
| `anthropic` | `https://api.anthropic.com` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `deepseek` | `https://api.deepseek.com/anthropic` | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| `moonshot` | `https://api.moonshot.cn/anthropic` | `kimi-k2-0905-preview` | `MOONSHOT_API_KEY` |
| `zhipu` | `https://open.bigmodel.cn/api/anthropic` | `glm-4.6` | `ZHIPU_API_KEY` |
| `openrouter` | `https://openrouter.ai/api` | `z-ai/glm-4.6` | `OPENROUTER_API_KEY` |
| `openai` | `https://api.openai.com/v1` | 自行填写 | `OPENAI_API_KEY` |
| `openai-compatible` | 自行填写 | 自行填写 | `OPENAI_COMPATIBLE_API_KEY`（可选） |
| `ollama` | `http://localhost:11434` | 已安装的模型 ID | 无 |

前五项使用 `anthropic_messages`；`openai` 使用 `openai_responses`；`openai-compatible` 使用显式选择的
`openai_chat` 或 `openai_responses`；`ollama` 使用原生协议。失败后不会自动猜测并切换协议。

推荐用 `pamu login <provider>` 或 `/login` 保存密钥：文件权限收紧为本人可读，密钥在输入时打码，
不进入输入历史和日志。若网关对 `thinking` 字段报错，用 `/thinking off` 回退。

### 本地模型示例

已启动本地推理服务后，可在 REPL 输入 `/login` 并选择 Ollama，选择默认地址或自定义地址，再输入
已经安装的模型 ID。也可仅用环境变量启动（把占位模型名替换为实际 ID）：

```bash
PAMU_PROVIDER=ollama MODEL_ID=your-installed-model pamu

# 支持 Chat Completions 的本地服务，例如配置在 8000 端口的服务：
PAMU_PROVIDER=openai-compatible OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1 \
  OPENAI_COMPATIBLE_PROTOCOL=openai_chat MODEL_ID=your-served-model pamu
```

模型必须支持原生工具调用才能完成编码任务。协议兼容不保证模型的工具能力；不使用文本标签模拟工具调用。
PamuCode 不安装、下载模型或启动推理服务。服务不可达时检查地址和启动状态，模型不存在时检查已安装 ID。
地址不能包含用户名、密码或密钥查询参数；需要认证的服务应使用对应的 API Key 配置。

## 命令行

| 命令 | 说明 |
| --- | --- |
| `pamu` | 启动交互式会话 |
| `pamu login [provider] [--no-verify]` | 配置供应商与模型，`--no-verify` 跳过联网校验 |
| `pamu logout [provider]` | 删除一个或全部已保存的供应商 |
| `pamu status` | 查看有效供应商、协议、地址、模型来源与已配置账号 |
| `pamu use <provider>` | 指定下次启动使用的供应商 |

## REPL 交互

| 命令 | 说明 |
| --- | --- |
| `/login` | 配置模型服务、认证和模型 |
| `/status` | 查看当前供应商、协议、地址、模型来源及登录状态 |
| `/model` | 选择或切换当前模型 |
| `/provider` | 选择并切换供应商 |
| `/thinking` | 选择或切换思考程度 |
| `/help`（`/h`） | 显示命令帮助 |
| `/quit` | 退出当前会话 |

```text
────────────────────────────────────────────────────────────────────────
@agent
────────────────────────────────────────────────────────────────────────
                                                deepseek-v4-pro • medium
❯ AGENTS.md
  agent_app/
  tests/test_terminal.py
  匹配 69 个，显示前 10 个
  ↑↓ 选择 · Enter 选中 · Esc 关闭
```

- `@` 唤起文件补全，`/` 唤起命令补全；两者都在输入框下方展开，`Esc` 关闭，`↑↓` 移动。
- 选中文件只是插入路径，不会直接发送；含空格的路径写成 `@"docs/my note.md"`。
- 斜杠命令不会进入模型对话，也不会留在输入历史里；取消或失败的菜单反馈会在输入框上方显示 3 秒后自动消失。
- 空输入被忽略，`q` / `exit` 会当作普通提问发送，`1` 秒内连按两次 `Ctrl+C` 结束会话。

### 思考程度

Messages 后端的 `/thinking` 决定上下横线的颜色，并在请求里附带对应的 token 预算：

| 级别 | 预算 | 说明 |
| --- | --- | --- |
| `off` | — | 不发送 `thinking` 字段 |
| `minimal` | 1024 | 最小 |
| `low` | 2048 | 低 |
| `medium` | 8192 | 中 |
| `high` | 16384 | 高 |

Responses、Chat Completions 和 Ollama 首版只接受 `off`，选择其他档位会提示不支持且不保存变更。
这里的 `off` 表示客户端不主动请求额外推理参数，不保证模型服务完全不推理。必要的原生推理回放信息
按协议、供应商、地址和模型隔离，切换后只保留可兼容的文本及工具往返。

## 工具

模型通过统一注册表调用工具，所有路径参数都会校验并收敛到工作区内。

| 工具 | 说明 |
| --- | --- |
| `bash` | 执行 shell 命令；`run_in_background: true` 转为后台任务 |
| `read_file` | 读取文件内容，支持 `offset` / `limit` 分页 |
| `write_file` | 整体写入文件 |
| `edit_file` | 精确替换文件中的一段文本，只替换一次 |
| `glob` | 按通配符查找文件 |
| `load_skill` | 载入 `skills/` 下某个技能的正文 |
| `compact` | 主动压缩历史，按 `focus` 保留要点后继续 |
| `todo_write` | 维护会话级 TODO 列表 |
| `create_task` / `list_tasks` / `get_task` / `claim_task` / `complete_task` | 任务板，支持 `blockedBy` 依赖 |
| `schedule_cron` / `list_crons` / `cancel_cron` | 5 段式 cron 定时任务，可持久化到磁盘 |
| `task` | 同步子代理：给出完整说明，只取回最终结论 |
| `spawn_teammate` / `send_message` / `check_inbox` / `request_plan` / `review_plan` / `request_shutdown` | 团队协作：派生队友、收发消息、计划评审 |
| `create_worktree` / `keep_worktree` / `remove_worktree` | 为任务创建独立 git worktree 与分支 |
| `connect_mcp` | 连接 MCP 服务并暴露其工具 |

## 运行时状态

所有项目状态都在工作区的 `.pamu/` 下：

```text
.pamu/
├── logs/                  # 运行日志 pamu.log
├── memory/MEMORY.md       # 项目记忆索引
├── transcripts/           # 会话记录
├── tasks/                 # 任务板
├── task_outputs/          # 大输出与工具结果落盘
├── mailboxes/             # 团队消息
├── worktrees/             # 隔离工作树
└── scheduled_tasks.json   # 定时任务
```

技能目录位于工作区根部的 `skills/<name>/SKILL.md`，属于项目可见内容，需要自己纳入版本管理。

`.pamu/` 自带一层保护性 `.gitignore`，仓库层面也已忽略它；路径与符号链接会被校验，
越界访问会被拒绝。但这**不是安全沙箱**：同时修改工作区文件的其他进程不在防护范围内。

历史遗留的 `.env` 不会被自动迁移。确认需要时再手动搬到 `.settings`，且不要覆盖已有文件。

## 架构

```text
CLI（cli.py / terminal.py）
        │  用户输入、斜杠命令、菜单、状态行
        ▼
Agent 循环（core/loop.py）
        │  组装上下文 → 请求模型 → 执行工具 → 判断是否继续
        ├── 上下文与压缩（core/context.py、core/prompt.py、core/compaction.py）
        ├── 失败恢复（core/recovery.py：回退模型、重试、续写）
        ▼
适配器层（adapters/*、provider_config.py）
        │  Messages / Responses / Chat Completions / Ollama、流式事件、配置与切换
        ▼
工具与特性（tools/*、features/*）
          注册表、执行器、Hooks；记忆、任务、子代理、团队、worktree、MCP、调度
```

```text
agent_app/
├── adapters/      模型与供应商适配
├── core/          agent 循环、上下文、压缩、恢复
├── features/      记忆、任务、团队、MCP、调度、worktree、子代理、技能
├── tools/         工具 schema、注册表、执行器、Hooks
├── branding.py    启动 Logo 与欢迎界面
├── terminal.py    prompt-toolkit 输入、菜单与状态行
└── cli.py         命令入口、生命周期与斜杠命令路由
```

- 组装点在 `agent_app/bootstrap.py:build_default_runtime`：配置、适配器、工具、Hooks、特性只在这里汇合。
- 入口是 `agent_app/cli.py:main`，`python -m agent_app` 等价于执行 `pamu`。
- `config.py` 区分 `repo_root`（安装根目录）与 `workdir`（当前目录），所有 `.pamu/` 路径都由工作区派生。

## 开发与测试

```bash
uv sync                          # 安装项目与 dev 依赖
uv run pytest -q                 # 全量离线测试
uv run pytest tests/test_cli_pty.py -q   # 终端 / PTY 相关用例
```

测试用假的模型客户端、临时目录与本地凭证，不需要真实 API Key 或网络。其中两条用例通过真实
PTY 驱动终端，需要可用的 PTY 设备。
新协议测试覆盖工具往返、参数分片、断流、截断恢复、原生回放隔离、配置保存、启动与切换。
离线测试不代表已验证真实模型效果；真实 OpenAI/Ollama 连通性需要用户可用的服务另行验证。

想通过真实命令验证改动：

```bash
uv tool install --editable .
pamu
```

仓库目前没有配置 CI、linter 或类型检查工具，`pyproject.toml` 里只有 pytest 的 `testpaths`。

## 安全与隐私

- 不要提交 API Key、访问令牌、凭证文件或本地 `.pamu/` 状态；仓库已忽略 `.settings`、`.env`、`.pamu/`、
  虚拟环境与各类缓存。
- 密钥写入 `~/.pamu/credentials.json`，权限仅本人可读；输入时打码，并被排除在输入历史与日志之外。
- 提交前检查暂存区：确认所有密钥为空或已脱敏。
- 工具会读写工作区文件、执行 shell 命令。请确认当前项目与供应商配置可接受这些操作后再使用。

## 许可证

[MIT](LICENSE) © 2026 Godhao55555

## 致谢

- [prompt-toolkit](https://python-prompt-toolkit.readthedocs.io/) —— 输入框、菜单与状态行
- [anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python) —— 模型客户端
- [uv](https://docs.astral.sh/uv/) —— 依赖与工具安装
- 交互与工程分层参考了 Claude Code、Codex 与 pi 的设计取舍

### 恢复会话

`pamu` 始终开始新会话；运行 `pamu resume` 可在终端选择会话，
`pamu resume --last` 恢复最近更新的会话，`pamu resume <会话编号>`
按完整编号或至少八位的唯一前缀恢复。非交互终端执行 `pamu resume` 只列出
会话和使用提示，不读取输入，并以状态码 2 退出；使用后两种形式可直接恢复。同一会话只能由一个进程打开，
最近会话被占用时 `--last` 会报错，不会改选其他会话。取消选择或没有历史正常退出，
编号、配置或存储错误以状态码 2 退出。
REPL 内输入 `/resume` 可选择其他会话，`/resume --last` 恢复最近更新的其他会话，
`/resume <会话编号>` 支持相同编号规则。当前会话编号是无操作；取消或目标错误会保留当前会话。
非交互输入中的裸 `/resume` 只列出其他会话和参数用法，不读取下一行作为选择答案。
切换前保存当前会话，并等待原调度线程停止；代理繁忙、后台任务或队友仍在运行、
有待处理的定时任务或团队消息、存在未完成的内存定时任务时暂缓切换。持久化定时任务
沿用当前分钟的触发记录；切换不会补跑错过的任务。目标启动失败时恢复原会话，
无法确认线程已经清理或当前保存失败时停止运行，避免两个会话同时处理任务。

会话保存在当前工作目录的 `.pamu/sessions/<UUID>.json`，单个快照最大为 32 MiB。
发送第一条消息前不创建空会话；`/status` 显示当前会话编号或尚未保存。
选择界面和恢复预览仅显示经过清理的有限文本，不展示原始工具结果或隐藏推理。
对话历史、工具结果、待办事项和模型选择会从最近一次检查点恢复；压缩前的历史
保存在归档中，不会自动重新加入上下文。

恢复使用当前配置和凭证连接服务，不会把快照中的地址用作认证目标，也不会
改写全局默认供应商或模型。进程环境、工作区和全局 `.settings` 的显式配置保持
原有优先级；未指定时使用会话的供应商、模型与兼容的思考档位。保存的降级模型
仅在供应商、协议和服务地址一致且没有显式配置时恢复。配置变化会在恢复时提示。

恢复本身不会调用模型或重新执行历史工具，下一条消息才继续对话。原生推理
数据仅在协议、服务地址、模型和内容仍匹配时复用。检查点不是每个流式片段都
保存；中断可能丢失尚未保存的输出。工具可能已产生副作用，即使结果未保存，
恢复后会标记未知结果，请先检查实际状态。后台任务和子代理不会自动重新启动。
保存失败会停止继续执行；达到大小限制或存储异常时应先处理存储问题再恢复。

### 上下文预算与压缩

四种协议共享请求预算与同步摘要流程，预算包含系统提示、工具定义、历史和记忆。
输入达到可用预算的 80% 时尝试压缩到 60%；保留最新用户请求与最近两个完整工具批次。
摘要记录目标、约束、决策、完成项、剩余工作、不确定性及证据引用。失败时保留原历史。

| 命令 | 用途 |
| --- | --- |
| `/context` | 查看窗口来源、当前估算、输出预留和压缩次数 |
| `/context window 64000` | 设置当前模型的会话窗口，随会话保存 |
| `/context pin 保留公共接口兼容性` | 固定一条原文约束，压缩后继续保留 |
| `/context pins`、`/context unpin 1` | 查看或移除固定约束 |
| `/compact 保留测试证据和下一步` | 空闲时主动压缩，执行后等待新输入 |

也可通过 `PAMU_CONTEXT_WINDOW` 配置启动模型的窗口。未识别的远端模型暂按 16,384、
Ollama 暂按 4,096 tokens 规划；请按实际服务配置调整。数值为发送前保守估算，
`/context` 会标明来源，不能当成精确 tokenizer 计数。Ollama 请求使用对应 `num_ctx`。

当前目录根 `AGENTS.md`、固定约束、真实待办和有限技能/文件材料会重新装配；
过大的必要规则不会静默截断。文件变化会标记旧材料过时。压缩结果有损，原始证据存于
`.pamu/sessions/<编号>/` 的去重对象和产物中；模型可用 `history_search`、`history_read`
按引用读取，避免重跑历史工具。检索有分页和扫描上限。

会话采用 v2 快照；恢复 v1 后首次保存会保留 `migration-v1.json` 备份并迁移归档。
旧版本程序不能直接读取 v2。原始供应商推理元数据只在合法作用域内重放；压缩改写
前缀时转为通用历史。首版采用通用文本摘要，未接入供应商原生压缩或后台异步压缩。
