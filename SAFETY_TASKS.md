# PamuCode 安全兜底执行文档

本文档用于多个 agent 接力完成 PamuCode 的安全兜底改造。**每完成一项任务，就在同一个提交里把该任务整节删掉。** 全部任务删完后，删除本文件以及 `AGENTS.md` 里指向它的那一行，再开 PR。

## 接力须知

- **分支**：`fix/safety-guardrails`（从 main `8d67ef0` 开出）。不要在 main 上直接改。
- **顺序**：按文中顺序做，一次只做一项。前 4 项优先级最高，后 3 项依赖前面的确认机制。
- **每项的流程**：
  1. 先写失败的测试，复现当前缺陷；
  2. 实现修复；
  3. `uv run pytest -q` 全部通过（偶发失败的 `test_user_signal_cleans_shell[SIGINT]` 重跑一次即可）；
  4. 同步更新 `README.md` 里对应的说明，必要时更新 `AGENTS.md`；
  5. 删除本文档中这一节；
  6. 提交，格式参考 `git log`（`fix: ...` / `feat: ...`，正文写清原因）。
- **项目约定**（详见 `AGENTS.md`）：
  - 面向用户的 CLI 文案是中文，测试会断言精确字符串，改文案要同步改断言。
  - 测试里不要读真实家目录：`tests/conftest.py` 已把 `agent_app.config.USER_HOME` 指向临时目录；构建 runtime 的测试要注入 `credentials_path`。
  - PTY 测试设置 `NO_COLOR=1`，并以 `❯` 作为输入框就绪的标记。
  - 交互确认统一走 `agent_app/cli_progress.py:terminal_confirmation`。权限钩子在 `agent_app/tools/hooks.py:make_permission_hook`，在 `agent_app/bootstrap.py:_register_default_hooks` 里注册。
  - headless 模式（`pamu run`，`agent_app/execution/__init__.py`）有自己的预算和钩子，主循环在 headless 下不触发 `PreToolUse` 权限钩子。除非任务明确要求，否则不要改动 headless 的行为。
- **已有的确认能力**（任务 1 已完成，后续任务直接复用，不要另写一套）：
  - `agent_app/cli_progress.py:terminal_confirmation` 在非主线程调用时不读 stdin，直接返回 `''`（视为拒绝），并打印 `BACKGROUND_APPROVAL_DENIED` 提示；stdin 不可读（`OSError`）时也返回 `''`。
  - Shell 命令由 `agent_app/security/command_policy.py:classify` 判定为 `allow` / `ask` / `deny`，结果里的 `reason` 用于展示原因；`make_permission_hook` 里有本会话白名单 `session_allowed`（回答 `a` 时写入）。
  - 配置 `PAMU_SHELL_APPROVAL=auto` 可让测试或用户跳过 Shell 确认；PTY 测试里在子进程脚本的 `AppConfig.from_env(environ=...)` 中传入即可。
- **路径保护**（任务 2 已完成）：默认模式下 `ExecutionPolicy.check_path` 也会拒绝 `.pamu`、`.env*`、`.settings`、`.ssh` 等路径，以及写 `.git/hooks`、`.git/config`；`state_roots`（bootstrap 里传入 `config.tool_result_dir` 和 `config.worktrees_dir`）是例外。新功能如果需要让模型读写 `.pamu` 下的某个目录，要把它加进 `state_roots`，不要放开整个 `.pamu`。

---

## 任务 3：收紧定时任务

**现状**
- `schedule_cron` 默认 `recurring=True, durable=True`（`agent_app/features/scheduler.py` 约第 52 行），也就是默认永久重复，并写入 `.pamu/scheduled_tasks.json`。
- 启动时 `load_durable_jobs` 会自动加载这些任务，没有过期时间，不需要确认，也没有斜杠命令可以查看或管理。
- 任务触发后，由 `agent_app/cli.py:_queue_processor_loop` 在后台线程里跑一整轮对话。中途需要确认时，`terminal_confirmation` 在后台线程里调用 `input()`，会和输入框抢 stdin。

**要做的**
1. 默认值改为 `recurring=False, durable=False`，同时更新 schema 描述。
2. 创建 `durable` 或 `recurring` 的任务时，要用户确认：在 `make_permission_hook` 里给 `schedule_cron` 加分支，复用任务 1 的确认能力。在后台线程里会被自动拒绝。
3. 重复任务默认 7 天过期：`CronJob` 增加 `expires_at`；`load_durable_jobs` 丢弃已过期的任务；调度循环跳过并删除过期任务。
4. 新增斜杠命令（同时更新 `agent_app/slash_commands.py` 和 `cli._handle_slash_command`）：
   - `/cron` 列出任务：ID、cron 表达式、prompt 摘要、是否重复、是否持久、过期时间；
   - `/cron cancel <ID|all>` 取消任务。
5. 启动时如果加载了持久任务，打印一行提示：「已加载 N 个持久定时任务，输入 /cron 查看」。
6. 定时任务触发时，在输入框上方打一行通知（通过 `cli_progress` 打印或 `ui.notify`），让用户知道后台正在执行哪个任务。

**验收**
- 不传参数时，创建的是一次性、不落盘的任务。
- 创建 durable 或 recurring 任务时会询问；拒绝后不创建。
- 过期任务不会被加载，也不会被触发。
- `/cron` 和 `/cron cancel` 有测试。
- 后台线程需要确认时直接拒绝，且不读 stdin，用 mock 断言不会调用 `input`。

---

## 任务 4：交互模式的轮次与资源上限

**现状**
- `agent_app/core/loop.py:run_agent_loop` 是 `while True`（约第 185 行），唯一的自动刹车是 `ToolRepeatLimit`：同工具同参数最多 3 次。
- headless 模式已经有 `max_model_calls`、`max_tool_calls`、`max_tokens`、`timeout` 四道上限（`agent_app/execution/__init__.py`），交互模式一道都没有。
- 后台任务（`agent_app/features/background.py:start_background_task`）和队友（`agent_app/features/teams/teammates.py:spawn_teammate_thread`）的数量没有上限。

**要做的**
1. 新增配置，在 `AppConfig.from_env` 里读取并校验为正整数：
   - `PAMU_MAX_TURN_MODEL_CALLS`，默认 50；
   - `PAMU_MAX_TURN_TOOL_CALLS`，默认 200；
   - `PAMU_MAX_TURN_TOKENS`，默认 2,000,000。token 数用 `record_usage` 返回的真实用量；provider 不返回用量时，退回到 `prepare_budget` 的估算值。
2. 在 `run_agent_loop` 里按轮计数。达到任一上限时：
   - 主线程交互模式：用 `terminal_confirmation` 询问「本轮已调用模型 X 次、工具 Y 次、约 Z tokens，是否继续？[y/N]」。选 y 则计数清零继续；否则结束本轮，打印提示，并保存检查点（保持会话状态为 `idle`）。
   - 非主线程（定时任务）：直接结束本轮。
   - headless 模式不受影响（它有自己的预算）。
3. 并发上限：
   - `PAMU_MAX_TEAMMATES`，默认 4，超出时 `spawn_teammate` 返回错误；
   - `PAMU_MAX_BACKGROUND_TASKS`，默认 8（指同时处于 running 的数量），超出时后台派发返回错误，提示模型等已有任务结束。

**验收**
- 用假 adapter 让模型持续返回不同参数的工具调用，测试到达上限后：回答 n 时停止，回答 y 时继续。
- 非主线程直接停止。
- 超出队友上限、后台任务上限时返回错误，有对应测试。
- 原有的循环测试保持通过（默认上限足够大）。

---

## 任务 5：自动记忆的过滤、可见性与备份

**现状**
- 每轮对话结束后，`run_agent_loop` 都会调用 `extract_memories`，并在条件满足时调用 `consolidate_memories`（`agent_app/core/loop.py` 约第 416–419 行）。
- `extract_memories`（`agent_app/features/memory.py`）会把模型从对话里提取的内容写成长期记忆，以后每次请求都会带上。
- 用户贴过的密钥、工具读到的文件里夹带的指令，都可能被存下来。
- `consolidate_memories` 按模型输出删除文件（约第 270 行 `path.unlink()`），没有备份。
- 两处都有 `except Exception` 把错误静默吞掉（约第 239、279 行）。

**要做的**
1. 写入前过滤疑似密钥：`sk-...`、`ghp_` / `gho_` / `github_pat_`、`AKIA...`、`xox[abp]-`、`-----BEGIN ... PRIVATE KEY-----`，以及 `api_key` / `token` / `secret` / `password` 后面紧跟的长字符串。命中时跳过这条记忆，并记一条日志（不要记录密钥本身）。
2. 提取时只使用用户输入（排除 `_pamu_source == "system"` 和内部提醒）和助手正文，不包含工具结果，以减少注入。
3. 可见性：
   - 新记忆写入后，在终端打印「已记住：<name>」（交互模式下可以用 `theme.success`）；
   - 新增 `/memory`（列出记忆）和 `/memory forget <name>`（删除）。
4. 合并前把整个记忆目录备份到 `.pamu/memory/.backup/<时间戳>/`，只保留最近 5 份。
5. 吞异常的地方改为 `LOGGER.warning(..., exc_info=True)`，返回值保持不变。
6. 新增配置 `PAMU_AUTO_MEMORY=on|off`，默认 `on`；`off` 时完全不提取、不合并。

**验收**
- 含密钥的对话不会产生记忆文件。
- 工具结果里的文本不会进入提取提示词。
- 合并前存在备份，超过 5 份时清理最旧的。
- `/memory` 的列出和删除有测试。
- `PAMU_AUTO_MEMORY=off` 时不调用 summarize。

---

## 任务 6：文件检查点与 /undo

**现状**：会话历史有快照，但文件没有。被 Ctrl+C 中断的轮次会保留已做的修改；不在 git 仓库里，或者有未提交改动时，改坏了没法撤销。

**要做的**
1. 在 `agent_app/bootstrap.py` 的 `run_write` / `run_edit` 包装里：每轮第一次修改某个文件之前，把原内容（文件不存在时记为「不存在」）保存到 `.pamu/checkpoints/<session_id>/<turn_id>/`，并写一个 manifest，记录相对路径和原内容的哈希。
   - 轮次 ID 可以复用 `session.context_state['current_request']['id']`。
2. 新增 `/undo`：
   - 列出上一轮改过的文件，确认后恢复；原来不存在的文件就删除；
   - 恢复前检查当前内容的哈希，如果和那一轮结束时不一致（之后又被改过），要提示并让用户确认。
   - 可以多次 `/undo`，逐轮往前撤。
3. 明确告知：Shell 造成的改动不在检查点范围内，`/undo` 的输出和 README 都要写明。
4. 检查点按会话最多保留 20 轮，删除会话时一并清理（参考 `repl_sessions.delete_current_session`）。

**验收**
- 写入、编辑、新建文件后执行 `/undo`，三种情况都能恢复。
- 连续两轮后能逐轮撤销。
- 文件被外部再次修改后，`/undo` 会提示。
- 超过 20 轮时清理最旧的检查点。

---

## 任务 7：项目自带技能需要信任确认

**现状**：`agent_app/config.py:skill_search_dirs` 会读取工作区里的 `.pamu/skills`、`.agents/skills`、`.claude/skills`。克隆一个陌生仓库，它自带的技能描述会直接进入系统提示词（`agent_app/core/context_materials.py` 的技能目录），技能正文也随时可以被加载，没有任何信任确认。

**要做的**
1. 在 `~/.pamu/trusted_workspaces.json` 里记录信任过的工作区：路径，加上当时项目技能清单的哈希（目录名 + SKILL.md 内容哈希）。文件权限设为 600，写法参考 `agent_app/features/auth.py` 的原子写入。路径基于 `agent_app.config.USER_HOME`，测试里会被隔离。
2. 工作区级别的技能目录，在工作区未被信任、或技能清单哈希变了时：
   - 不进入注册表（`SkillState` 增加一个 roots 级别的「待信任」状态）；
   - 交互模式下第一次扫描到时，列出技能名和来源，询问是否信任。信任后写入记录，刷新注册表。
   - 非交互模式或后台线程下保持排除。可以用 `PAMU_TRUST_PROJECT_SKILLS=1` 显式放行。
3. 用户级目录（`~/.pamu|.agents|.claude/skills`）默认信任。
4. `list_skills` 在有待信任技能时说明原因，例如「项目技能等待信任：输入 /skill trust」。新增 `/skill trust` 子命令。

**验收**
- 未信任的工作区里，项目技能不出现在 `list_skills` 和系统提示词里。
- 信任后出现；技能内容变化后需要重新信任。
- 用户级技能不受影响。
- 设置 `PAMU_TRUST_PROJECT_SKILLS=1` 时直接可用。

---

## 收尾

1. 本文档里的 7 节任务都删完后，删除本文件，并删掉 `AGENTS.md` 里指向本文件的那一行。
2. 跑一遍 `uv run pytest -q`。检查暂存区：不能包含密钥、`.settings`、`.env` 或 `.pamu/` 下的状态文件。
3. 推送 `fix/safety-guardrails`，对 main 开 PR，在描述里逐项列出改动和新增的配置项。
