# PamuCode

中文 | [English](README.en.md)

PamuCode 是一个用 Python 写的终端编程智能体。它把模型驱动的智能体循环、文件和 Shell 工具、会话、上下文管理、技能和任务委派放在一个分层运行时里，每一层都能单独读懂和修改。

在哪个目录启动 `pamu`，哪个目录就是工作区，运行时状态保存在该目录的 `.pamu/` 下。界面提示是中文。

当前版本 0.1.0，仍处于实验阶段，需要 Python 3.13 及以上。

## 功能

- 流式输出，支持重试、降级模型、续写和上下文恢复。
- 接入 Anthropic Messages、OpenAI Responses、OpenAI Chat Completions 和 Ollama 原生协议。
- 用 `/login` 在本地保存多个服务的 API，用 `/model` 在所有已接入服务的模型之间切换。
- 圆角输入框，支持斜杠命令提示、`@` 文件补全和输入历史；底栏显示工作区、Git 分支、上下文占用、模型、思考程度和工作模式。
- 工具调用以 `● Read(path)` 加 `⎿` 摘要行显示，编辑会给出 `+新增 −删除` 行数；流式 Markdown 逐行渲染，代码块带语法高亮。
- 规划模式和执行模式、`/undo` 撤销文件修改、可恢复的会话、上下文压缩和固定约束。
- 跨会话记忆与离线整理、待办、共享任务、定时任务、后台命令、子智能体、队友协作和 Git worktree。
- 工具钩子、MCP 发现框架，以及有预算限制的无界面执行和评测工具。

## 快速开始

先装好 Python 3.13+、Git 和 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/imnoob55555/PamuCode.git
cd PamuCode
uv sync
uv tool install --editable .
uv tool update-shell
```

如果 `pamu` 还不在 PATH 里，重开一个终端。PowerShell 下命令相同。也可以在源码目录里用 `uv run python -m agent_app` 启动。

然后进入你要处理的项目再启动：

```bash
cd /path/to/your/project
pamu
```

第一次启动会提示输入 `/login`。选择供应商，按需填写服务地址和 API Key（输入时打码），再选模型就能开始对话。

## 接入模型

### 支持的供应商

| 名称 | 服务 | 协议 |
| --- | --- | --- |
| `anthropic` | Anthropic 官方 | Messages |
| `deepseek` | DeepSeek | Messages |
| `moonshot` | Moonshot Kimi | Messages |
| `zhipu` | 智谱 GLM | Messages |
| `openrouter` | OpenRouter | Messages |
| `openai` | OpenAI 官方 | Responses |
| `openai-compatible` | 任意 OpenAI 兼容服务 | 登录时选 Chat Completions 或 Responses |
| `ollama` | Ollama 本地模型 | Ollama 原生 `/api/chat` |

模型必须支持原生工具调用才能完成编程任务。请求失败时不会自动换协议。`/thinking` 的五档思考程度目前只对 Messages 协议生效，其他协议只能设为 `off`。

### `/login`、`/model` 和 `/provider`

`/login` 把一个服务的地址、协议、API Key 和默认模型保存到本机的 `~/.pamu/credentials.json`。可以登录多个供应商，它们会同时保存。交互式登录不联网，不会验证 Key 和模型是否可用，第一次真正发请求时才知道。命令行的 `pamu login` 默认会发一次验证请求，加 `--no-verify` 跳过。

`/model` 列出所有已经在本地接入 API 的服务的模型，每行显示「服务 · 模型」。哪些服务算已接入：用 `/login` 登录过的，或者在 `.settings`、环境变量里配了 Key 的。每个服务列出它保存的模型和内置的常用模型，最后一项可以为当前服务输入自定义模型 ID。

选了另一个服务的模型，PamuCode 会用那个服务保存的 Key 和地址重建连接，当前会话立即生效，并把它设为下次启动的默认服务。也可以直接输入 `/model <模型 ID>`：当前服务有这个模型或者谁都没有时留在当前服务，只有别的已接入服务提供这个 ID 时才切过去。

`/provider` 按服务切换，使用该服务保存的模型。

同一个供应商只保存一条记录，所以没法同时保存两个不同地址的 OpenAI 兼容服务，后登录的会覆盖前一个。

### 本地模型服务

PamuCode 不负责安装或运行模型，只连接已经在跑的服务：

```bash
PAMU_PROVIDER=ollama MODEL_ID=your-installed-model pamu

PAMU_PROVIDER=openai-compatible MODEL_ID=your-served-model \
  OPENAI_COMPATIBLE_BASE_URL=http://localhost:8000/v1 \
  OPENAI_COMPATIBLE_PROTOCOL=openai_chat pamu
```

Ollama 不需要 Key，兼容服务可以选配 `OPENAI_COMPATIBLE_API_KEY`。服务地址里不能带用户名、密码或查询参数。

## 命令

| 命令 | 作用 |
| --- | --- |
| `pamu` | 启动交互会话 |
| `pamu continue` / `pamu -c` | 恢复当前工作区最近的会话 |
| `pamu resume [ID]` / `pamu -r [ID]` | 选择会话恢复，或按 ID 直接恢复 |
| `pamu login [供应商] [--no-verify]` | 保存供应商凭证和模型 |
| `pamu logout [供应商]` | 删除已保存的凭证 |
| `pamu status` | 查看实际生效的配置和登录状态 |
| `pamu use <供应商>` | 设置以后启动使用的供应商 |
| `pamu run ...` | 执行一次有预算限制的无界面任务 |

| 斜杠命令 | 作用 |
| --- | --- |
| `/login` | 配置模型服务与认证 |
| `/model` | 在所有已接入的服务间选择或切换模型 |
| `/provider` | 切换供应商 |
| `/thinking` | 选择思考程度 |
| `/status` | 查看当前会话、供应商和模型 |
| `/plan` / `/execute` | 进入规划模式 / 执行模式 |
| `/skill [名称] [任务]` | 选择或调用技能；`/skill trust` 信任项目自带技能 |
| `/context` | 查看上下文预算；子命令 `window <tokens>`、`pin <文本>`、`pins`、`unpin <编号>` |
| `/compact [重点]` | 压缩旧历史，保留近期交互 |
| `/resume [--last \| ID]` | 切换到其他已保存会话 |
| `/delete` | 确认后删除当前会话并退出 |
| `/undo` | 撤销上一轮通过文件工具做的修改 |
| `/memory [forget <路径>]` | 查看或删除记忆笔记 |
| `/dream` | 回顾近期会话，整理和修正记忆 |
| `/background` / `/stop <ID \| all>` | 查看或停止后台命令 |
| `/cron [cancel <ID \| all>]` | 查看或取消定时任务 |
| `/help` / `/h` | 显示命令列表 |
| `/quit` | 退出 |

方向键选择提示，Tab 补全，Enter 提交，Esc 关闭菜单。菜单也可以按数字快选。输入 `@` 浏览工作区文件，带空格的路径会自动加引号。退出用 `/quit`，或在一秒内按两次 Ctrl+C。

## 使用说明

### 审批与中断

只读命令直接运行，比如 `ls`、`cat`、`rg`、不带 `-delete`/`-exec` 的 `find`、`git status`/`diff`/`log`/`show`。其他命令会先问你，并说明原因，包括未知程序、`>` 重定向写入、命令替换、子 Shell 和无法解析的命令。回答 `a` 后，本会话内同一程序加子命令（比如 `uv run pytest`）不再询问。

删除、移动或外发数据的命令（`rm`、`mv`、`cp`、`curl`、`ssh`、`git push`、`git reset` 等）以及涉及敏感路径的命令每次都问。`sudo`、`su`、`shutdown`、`mkfs`、写设备的 `dd`、递归删除 `/` 或家目录会被直接拒绝，写成 `s\udo` 或 `/usr/bin/sudo` 也一样。文件修改在确认前会显示 diff。

模型工作时按 Ctrl+C 会中断当前这一轮，回到同一个会话。已完成的修改和已经输出的内容会保留，被中断的工具调用会收到明确的错误结果，继续对话时不会自动重放。中断的操作可能已经产生了副作用，重试前先看一下实际状态。

单轮默认最多 50 次模型调用、200 次工具调用、200 万 token，达到上限时会问你是否继续。

### 规划与执行

新会话默认是执行模式。`/plan` 进入规划模式后只能读文件、搜索、加载技能、检索历史和维护待办，改代码、跑 Shell、调用 MCP、定时和委派都会被拦下。要回到执行模式必须明确输入 `/execute`，在对话里说"可以开始了"不会切换。

执行模式也不等于允许做无关修改、推送或部署。你在对话里提的限制靠模型自觉遵守，需要强制只读时用 `/plan`。

### 撤销

每一轮第一次通过 `write_file` 或 `edit_file` 改某个文件前，PamuCode 会把原内容存到 `.pamu/checkpoints/` 下。`/undo` 列出最近一轮改过的文件，确认后恢复（这一轮新建的文件会被删掉），再执行一次就继续往前撤。如果文件之后又被改过，会先警告。Shell 命令做的修改撤销不了。每个会话保留最近 20 轮。

### 项目指令

每轮请求都会带上项目说明文件。PamuCode 从 git 仓库根目录开始，一层层走到当前目录，每层读一个 `AGENTS.md`；某一层没有 `AGENTS.md`（或者文件是空的、是符号链接）时，改读同层的 `CLAUDE.md`。读到的内容按从外到内的顺序拼接，并标出每份文件的相对路径，越靠近当前目录的越靠后。不在 git 仓库里时只读当前目录。比当前目录更深的子目录里的说明文件不会读取。

这些文件全文放进提示词，不会截断。合计超过 32 KB 时，启动时会提示一次。改了文件，下一轮就生效。

启动时还会记一份 git 快照，内容是当前分支、默认分支、`git status --short --branch`（超过约 2000 字符截断）和最近 5 条提交。快照每次启动只取一次，不写进会话存档，恢复旧会话时会重新获取。取快照时会禁用 git hooks、fsmonitor 和签名校验，超时 5 秒。confined 模式只读当前目录的说明文件，也不取快照。

### 会话与上下文

会话按工作区保存在 `.pamu/sessions/`。恢复时会按顺序重放历史消息，按 Ctrl+C 可以跳过重放。历史命令不会重新执行，后台进程和队友也不会自动恢复。

底栏的 `Context xx%` 是保留的对话、系统指令和工具定义大约占模型上下文窗口的比例，估算值可能超过 100%。`/context` 查看窗口大小和来源，未知模型的窗口可以用 `/context window <tokens>` 手动设定。`/compact` 把旧对话压缩成摘要，保留当前请求和最近的工具调用。`/context pin` 固定的约束会原文保留，压缩和恢复会话后都还在。

### 记忆

记忆分三层，设计参考了 Anthropic 的文章 [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)：

- **会话记录**：`.pamu/sessions/` 里的历史会话。模型可以用 `transcript_search` 和 `transcript_read` 查以前的会话，只能看到你说的话、模型的回答和它调用了哪些工具，看不到工具输出。
- **记忆笔记**：`.pamu/memory/` 下的 Markdown 文件，可以按主题分子目录（最多 3 层）。模型通过 `memory` 工具读写，什么值得记由它自己判断：你要求它记住的，或者明显以后用得上的，比如偏好、做过的决定和理由、验证过的规则、踩过的坑。代码和 git 里能直接查到的东西不该记。看起来像凭证的内容会被拒绝。每次写入都会显示在工具调用记录里。
- **`MEMORY.md`**：笔记的索引，每个会话开始时加载前 200 行（最多 25 KB），其余笔记按需读取。还没有 `MEMORY.md` 时，加载的是笔记列表。

在会话里随手写下的笔记难免有错，或者过一段时间就过时了。`/dream` 会启动一个离线整理过程：对照最近的会话记录检查笔记，改掉错的，补上漏记的，合并重复的，把 `MEMORY.md` 压回一个简短的索引。整理前会把笔记备份到 `.pamu/memory/.backup/`（保留最近 5 份）；中途失败或按 Ctrl+C，笔记会恢复原样。退出 REPL 时，如果自上次整理以来积累了 3 个以上有实际对话的会话，或者上次整理已超过 24 小时且有新会话，会自动整理一次，按 Ctrl+C 可以跳过。

`/memory` 列出笔记，`/memory forget <路径>` 删除一条。设 `PAMU_AUTO_MEMORY=off` 后，模型只能读记忆，退出时也不再自动整理，`/dream` 仍可手动使用。规划模式下记忆只读。沙箱 `confined` 模式下不提供记忆工具。

### 技能

技能是一个包含 `SKILL.md` 的目录。按优先级从高到低搜索：

| 目录 | 范围 |
| --- | --- |
| `<工作区>/.pamu/skills/` | 本项目，私有 |
| `<工作区>/.agents/skills/` | 本项目，可随仓库共享 |
| `<工作区>/.claude/skills/` | 本项目，和 Claude Code 共用 |
| `~/.pamu/skills/` | 当前用户的所有项目 |
| `~/.agents/skills/` | 所有项目，和其他智能体工具共用 |
| `~/.claude/skills/` | 所有项目，和 Claude Code 共用 |

```markdown
---
name: Review
description: Review the current changes
---
Read the diff, identify defects, and report findings before editing.
```

目录名就是技能标识，frontmatter 里的 `name` 可作为别名。新增、修改、删除技能都不用重启。用 `/skill review 检查认证相关的改动` 调用。

项目自带的技能（工作区里那三个目录）会进入系统提示，所以要你先信任才加载。第一次发现时 PamuCode 会列出来问你，之后也可以用 `/skill trust`。信任记录按工作区保存在 `~/.pamu/trusted_workspaces.json`，项目技能有增改时会重新询问。用户级技能始终信任。

### 后台与定时

后台命令和队友独立于当前对话运行。`/background` 查看，`/stop <ID>` 或 `/stop all` 停止。

模型可以用 cron 表达式安排任务。默认是一次性的，只在本会话有效；周期任务和持久任务需要你批准，周期任务七天后过期，持久任务保存在 `.pamu/scheduled_tasks.json`。定时触发的轮次不能发起审批，需要审批的操作会被拒绝。`/cron` 查看，`/cron cancel <ID>` 取消。

## 配置

优先级从高到低：

1. 进程环境变量
2. `<工作区>/.pamu/.settings`
3. `~/.pamu/.settings`
4. `~/.pamu/credentials.json` 里保存的供应商配置

[.settings.example](.settings.example) 是模板，需要时复制到上面的某个 `.settings` 位置再填。旧的 `.env` 不会自动迁移。设置了 `MODEL_ID` 会覆盖登录时保存的模型，想用保存的模型就把它清空。

| 配置项 | 作用 |
| --- | --- |
| `PAMU_PROVIDER` | 选择供应商 |
| `MODEL_ID` | 模型 ID，已保存的供应商配置里有模型时可不填 |
| `FALLBACK_MODEL_ID` | 同一供应商内的降级模型 |
| `<供应商>_API_KEY` / `<供应商>_BASE_URL` | 该服务的 Key 和可选地址 |
| `OPENAI_COMPATIBLE_PROTOCOL` | `openai_chat` 或 `openai_responses` |
| `PAMU_CONTEXT_WINDOW` | 启动模型的上下文窗口 |
| `PAMU_SANDBOX` | `off`（默认）或 `confined` |
| `PAMU_MAX_TURN_MODEL_CALLS` / `_TOOL_CALLS` / `_TOKENS` | 单轮上限，默认 50 / 200 / 2,000,000 |
| `PAMU_MAX_TEAMMATES` / `PAMU_MAX_BACKGROUND_TASKS` | 并发队友数和后台命令数，默认 4 / 8 |
| `PAMU_TRUST_PROJECT_SKILLS` | 设为 `1` 时不询问直接加载项目技能 |
| `PAMU_AUTO_MEMORY` | `on`（默认）或 `off`：关闭后模型只读记忆，退出时不自动整理 |
| `PAMU_SHELL_APPROVAL` | `ask`（默认）或 `auto`。`auto` 会不经询问运行除被拒命令以外的所有 Shell 命令，有风险 |
| `LOG_LEVEL` | 日志级别 |

## 无界面执行与评测

`pamu run` 用和交互模式相同的循环执行一个任务，带明确的预算，工具只有 `bash`、`read_file`、`write_file`、`edit_file` 和 `glob`，可以用 `--allow-tool` 进一步缩小。它只从进程环境变量读供应商配置，不读保存的凭证和 `.settings`。

```bash
pamu run --task-file /tmp/task.txt --output /tmp/pamu-run \
  --model your-model --protocol anthropic_messages \
  --max-model-calls 30 --max-tool-calls 100 --max-tokens 100000 --timeout 300
```

结果写到输出目录的 `events.jsonl` 和 `result.json`。跑完不代表任务做对了，token 统计也不是精确的费用上限。

源码里的 `pamu_eval` 是评测工具，不随运行时一起打包：

```bash
uv run python -m pamu_eval selftest --output /tmp/pamu-selftest
uv run python -m pamu_eval run evals/regression.json \
  --output /tmp/pamu-regression --repetitions 3 --jobs 2
uv run python -m pamu_eval report /tmp/pamu-regression
uv run python -m pamu_eval compare /tmp/pamu-baseline /tmp/pamu-candidate
```

`evals/regression.json` 离线回放，测的是运行时机制。`evals/live.json` 和 `evals/intent-live.json` 需要真实模型，用 `--model` 和 `--protocol` 指定。另外还有回放导出、性能采样、SWE-bench 预测导出和 Harbor/Terminal-Bench 桥接，这些只是适配器，不代表跑过官方评测。

## 安全

- 凭证保存在仓库外的 `~/.pamu/credentials.json`，权限只对当前用户开放。输入 Key 时打码，不进输入历史和日志。
- 默认 `PAMU_SANDBOX=off`，没有操作系统级的文件和网络隔离。Shell 子进程拿到的是白名单环境变量、临时 HOME 和固定的 PATH，不会继承供应商凭证。
- 即使不开沙箱，文件工具也会拒绝访问工作区里的 `.pamu`、`.env`、`.settings`、`.ssh`、`.aws`、`.gnupg`、`.codex`，并拒绝写 `.git/hooks` 和 `.git/config`。Shell 命令提到这些路径时一定会询问，但这只是文本检查，真正拦住 Shell 读取要靠 `PAMU_SANDBOX=confined`。
- `confined` 模式在 macOS 上用 Seatbelt 限制 Shell 的文件和网络访问。平台不支持或沙箱后端不可用时直接报错，不会退回无隔离运行。
- 本地边界不是多租户隔离，也没有完整的 CPU、内存、进程数和磁盘配额。不可信的任务请放到容器或虚拟机里跑。
- `.env`、`.settings`、凭证文件、`.pamu/`、日志和评测输出都不要提交。会话记录和工具输出里可能有源码或私有数据，分享前先检查。

## 架构

```text
CLI 与终端界面
  → 智能体循环与上下文管理
    → 模型适配器与工具执行
      → 工作区功能与执行策略
```

| 路径 | 职责 |
| --- | --- |
| `agent_app/cli.py`、`terminal.py` | 输入、命令、状态和生命周期 |
| `agent_app/bootstrap.py` | 组装运行时 |
| `agent_app/core/` | 循环、提示词、预算、压缩和恢复 |
| `agent_app/adapters/` | 供应商协议和流式输出 |
| `agent_app/tools/` | 工具定义、注册、分发和钩子 |
| `agent_app/security/` | 执行策略、文件访问和 Shell 边界 |
| `agent_app/features/` | 会话、技能、记忆、任务和协作 |
| `pamu_eval/`、`evals/` | 评测工具和用例，仅在源码中 |

Logo、标题和欢迎提示在 `agent_app/branding.py` 里改。

## 开发

```bash
uv sync
uv run pytest -q
uv run pytest tests/test_cli_pty.py -q
uv tool install --editable .
```

测试用假的模型客户端和临时目录，不需要真实 Key，也不联网。PTY 测试需要 Unix 终端环境。目前没有配置 linter、类型检查或 CI。

## 许可

[MIT](LICENSE)。基于 [prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit)、[Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)、[OpenAI Python SDK](https://github.com/openai/openai-python) 和 [uv](https://github.com/astral-sh/uv) 构建。界面和架构参考了 Codex、Claude Code 和 pi。
