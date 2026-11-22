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
