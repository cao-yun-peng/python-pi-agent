# pi-agent-py

一个教学优先、模块化、可复用的 Python Agent Harness。我们从可以完全看懂的最小循环开始，逐步加入工具、会话、上下文压缩、权限、沙箱、插件、评测与多 Agent 协作。

当前里程碑只实现第一条纵向切片：

```text
用户输入 -> 消息历史 -> ModelProvider -> 流式事件 -> Assistant 消息 -> settled
```

它不连接真实模型，使用脚本化 `FakeProvider` 让每次运行和测试都确定、离线且免费。

## 快速开始

在仓库根目录执行。首次使用可以先安装为 editable package：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\python-pi-agent
.\.venv\Scripts\python.exe -m unittest discover -s .\python-pi-agent\tests -v
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
```

如果当前环境没有构建工具或不能联网，可以直接用源码目录运行，测试本身没有第三方依赖：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe -m unittest discover -s .\python-pi-agent\tests -v
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
```

预期示例输出：

```text
你好，我是最小 Agent。

--- trace ---
live    run_started
durable message_committed
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
5. `src/pi_agent_py/testing.py`：如何不用真实模型验证循环
6. `tests/test_minimal_loop.py`：公开行为契约

项目路线见 [docs/learning-path.md](docs/learning-path.md)，架构边界见 [docs/architecture.md](docs/architecture.md)。
