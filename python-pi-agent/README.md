# pi-agent-py

一个教学优先、模块化、可复用的 Python Agent Harness。我们从可以完全看懂的最小循环开始，逐步加入工具、会话、上下文压缩、权限、沙箱、插件、评测与多 Agent 协作。

当前已经实现九条纵向切片：

```text
用户输入 -> 消息历史 -> ModelProvider -> 流式事件 -> Assistant 消息 -> settled
DeepSeek HTTPS/SSE -> DeepSeekProvider -> 通用模型事件 -> Agent Loop
模型 ToolCall -> schema 校验 -> 权限审批 -> 工具执行 -> ToolResult -> 模型续写
SessionStore -> 加载历史 -> Agent Loop -> 逐消息提交 -> 下一轮恢复
消息提交 -> DurableSessionEvent -> 有序事件日志 -> replay -> SessionSnapshot
JSONL -> 严格反序列化 -> durable events -> replay -> 跨进程恢复
历史前缀 -> SessionStore.fork -> 独立 durable log -> 分支继续追加
完整历史 -> ContextStrategy -> ContextPlan -> 单次模型请求投影
完整历史 -> turn 分组 -> token budget -> 最近完整上下文投影
```

它同时提供脚本化 `FakeProvider` 和真实 `DeepSeekProvider`。默认测试仍然确定、离线且不会产生 API 费用。

## 快速开始

在仓库根目录执行。首次使用可以先安装为 editable package：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\python-pi-agent
.\.venv\Scripts\python.exe -m unittest discover -s .\python-pi-agent\tests -v
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\03_tool_calling.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\04_session_resume.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\05_session_replay.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\06_jsonl_persistence.py --help
.\.venv\Scripts\python.exe .\python-pi-agent\examples\07_session_fork.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\08_context_strategy.py
.\.venv\Scripts\python.exe .\python-pi-agent\examples\09_token_budget_context.py
```

已经安装 `httpx` 依赖时，也可以直接用源码目录运行：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe -m unittest discover -s .\python-pi-agent\tests -v
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
```

连接 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe .\python-pi-agent\examples\02_deepseek_chat.py
```

默认使用当前官方模型名 `deepseek-flash`，可通过 `DEEPSEEK_MODEL` 修改。详见[第二章：DeepSeekProvider](tutorials/02-deepseek-provider.md)。

预期示例输出：

```text
你好，我是最小 Agent。

--- trace ---
live    run_started
durable message_committed
capability context_prepared
live    model_request_started
live    text_delta
live    text_delta
durable message_committed
live    run_completed
live    run_settled
```

## 从哪里开始读

1. [教程：最小 Agent Loop](tutorials/01-minimal-agent-loop.md)
2. `src/pi_agent_py/domain.py`：什么是消息和事件
3. `src/pi_agent_py/ports.py`：核心依赖哪些抽象
4. `src/pi_agent_py/runtime.py`：循环如何消费流并提交消息
5. `src/pi_agent_py/session_transitions.py`：会话事件怎样纯计算出新状态
6. `src/pi_agent_py/testing.py`：如何不用真实模型验证循环
7. `tests/test_minimal_loop.py`：公开行为契约
8. `src/pi_agent_py/providers/deepseek.py`：真实 SSE Provider 适配器
9. `src/pi_agent_py/tools.py`：工具定义、审批策略与 schema 校验
10. [教程：工具调用系统](tutorials/03-tool-calling.md)
11. [教程：会话存储与 AgentSession](tutorials/04-session-runtime.md)
12. [教程：Durable Event Log 与 Replay](tutorials/05-durable-event-log.md)
13. [教程：JSONL 持久化](tutorials/06-jsonl-persistence.md)
14. [教程：持久化会话分叉](tutorials/07-session-fork.md)
15. [教程：ContextStrategy 接缝](tutorials/08-context-strategy.md)
16. [教程：Token Budget 与完整消息组](tutorials/09-token-budget-context.md)

项目路线见 [docs/learning-path.md](docs/learning-path.md)，架构边界见 [docs/architecture.md](docs/architecture.md)。
