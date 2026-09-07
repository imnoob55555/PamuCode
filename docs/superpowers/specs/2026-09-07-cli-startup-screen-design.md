# PamuCode CLI 启动欢迎屏与退出交互设计

## 目标

为 PamuCode 增加类似 Codex/Claude Code 的轻量 CLI 启动界面，同时保持现有
REPL、Provider 切换、模型切换、流式输出和非交互模式不变。

本次范围只包含：

- 交互式终端启动欢迎屏；
- `/quit` slash command；
- 双击 `Ctrl+C` 退出；
- slash command 的输入历史、会话历史和执行结果展示规则。

不包含全屏终端布局、固定状态栏、流式输出重构或新的 Provider 功能。

## 已确认的用户体验

### 启动欢迎屏

仅在 stdin/stdout 均为 TTY 时显示，非交互模式保持现有输出行为。欢迎屏只显示
一次，然后进入现有 REPL。

示意内容：

```text
  ____    _    __  __ _   _
 |  _ \\  / \\  |  \\/  | | | |
 | |_) |/ _ \\ | |\\/| | | | |
 |  __// ___ \\| |  | | |_| |
 |_|  /_/   \\_\\_|  |_|\\___/

  PamuCode · AI Coding Agent

  工作目录：/path/to/project
  Provider：deepseek
  模型：deepseek-chat
  状态：已准备就绪

  输入问题开始对话
  输入 /help 查看命令
  输入 /quit 退出
```

Logo 和欢迎文案放在 `agent_app/branding.py`，后续修改品牌内容时不需要改动
`cli.py` 或 `terminal.py`。

状态显示规则：

- 运行时已创建：显示运行时实际 Provider、模型和“已准备就绪”；
- 交互式启动但尚未配置：显示“尚未配置 API Key”，并提示 `/login`；
- 配置错误：保留原有错误信息，同时欢迎屏只提供可操作的配置提示。

启动屏不再打印旧的固定欢迎语，避免重复显示两套欢迎信息。

### 退出行为

- `/quit`：退出当前会话；不支持 `/quite` 别名；
- 空行：不退出，继续等待输入；
- `q`、`exit`：不再是退出快捷方式，作为普通文本处理；
- 第一次 `Ctrl+C`：清空当前输入并提示“再次按 Ctrl+C 退出 PamuCode”；
- 约 1 秒内第二次 `Ctrl+C`：退出 REPL，并执行已有的后台线程清理逻辑；
- 超过时间窗口后再次按 `Ctrl+C`：视为新的一次第一次中断。

### Slash command 记录规则

slash command 的输入和执行结果分开处理：

- 任何以 `/` 开头的输入都不进入 Agent 会话历史；
- 任何以 `/` 开头的输入都不进入上下键输入历史；
- 成功执行的 slash command，其执行结果正常打印在终端聊天记录中；
- 被取消、未执行或未知的 slash command 不保留命令输入，也不产生额外聊天记录；
- `/quit` 不进入任何历史，并显示简短退出提示后结束进程；
- 普通文本继续进入 Agent 会话历史、终端输入历史并显示在终端中。

示例：

```text
/status
供应商：deepseek
模型：deepseek-chat
降级模型：（无）
```

`/status` 本身不是模型消息，但它产生的状态结果仍然可见。

## 架构

新增 `agent_app/branding.py`，提供纯文本渲染能力，例如：

```python
def render_startup_screen(runtime, credentials_path: Path) -> str:
    ...
```

该模块负责：

- Logo 常量和品牌文案；
- 当前工作目录读取；
- Provider、模型和配置状态的格式化；
- ANSI 颜色文本；
- 无运行时场景下的配置状态展示。

`agent_app/cli.py` 负责在启动后台线程并完成运行时创建后调用欢迎屏渲染函数。
欢迎屏渲染失败不能阻断已有 REPL；必要时应降级为简单纯文本提示。

`agent_app/terminal.py` 继续负责输入控件，但需要调整中断状态，使第一次
`Ctrl+C` 不直接抛出退出异常，第二次才结束当前 prompt。

`agent_app/slash_commands.py` 增加 `/quit` 命令元数据，使它出现在 `/help` 和
输入补全列表中。命令处理应返回明确的退出信号，由 `cli.py` 结束主循环，避免
把退出逻辑混入 Agent 运行循环。

## 数据流

```text
启动
  -> 判断是否交互式终端
  -> 创建 runtime（若配置可用）
  -> 读取 runtime/auth 状态
  -> branding.render_startup_screen()
  -> 进入 TerminalUI.prompt()
  -> 普通文本：显示、写入历史、调用 Agent
  -> slash command：执行命令，不写入历史；成功时保留命令输出
```

启动屏读取的状态必须来自当前实际运行时，而不是只读取凭证文件，避免启动后
Provider/模型状态和欢迎屏不一致。

## 错误与边界情况

- 非交互终端不显示 ANSI 欢迎屏，避免破坏脚本输出；
- 小尺寸终端仍使用可换行的普通文本，不依赖固定宽度；
- 未配置时欢迎屏可以显示，但不能假装 Agent 已准备就绪；
- `/quit` 的退出必须经过 `finally`，确保 scheduler 和 queue processor 被停止；
- 第一次 `Ctrl+C` 的提示不能写入 Agent 上下文或输入历史；
- 未知 slash command 的错误提示可以显示为一次命令反馈，但命令本身不能进入
  Agent 或输入历史；
- 保留现有未提交改动，不覆盖与本功能无关的用户工作。

## 测试设计

新增或补充以下测试：

1. `branding.py` 在已配置、未配置和无 runtime 场景下生成正确状态文本；
2. 交互式 `main()` 启动时显示一次欢迎屏，非交互模式不显示欢迎屏；
3. 启动屏包含当前目录、Provider、模型、`/help` 和 `/quit` 提示；
4. 空行、`q`、`exit` 不触发退出；
5. `/quit` 结束主循环并执行线程清理；
6. 第一次 `Ctrl+C` 保持会话，第二次 `Ctrl+C` 退出；
7. 成功执行的 slash command 输出仍显示在终端；
8. 未知、取消或未执行的 slash command 不进入 Agent history 和输入 history；
9. 普通文本仍进入两类历史并调用 Agent；
10. 现有 Provider、模型和 slash command 测试全部保持通过。

## 可修改位置

后续想修改品牌 Logo、标题或启动提示时，优先编辑：

```text
agent_app/branding.py
```

不建议直接修改 `agent_app/cli.py` 中的启动流程，除非要改变启动时机或状态
来源。
