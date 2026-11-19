# 第二十章：精简的 Workspace Process Sandbox

## 1. 这章只解决一个问题

目标是让模型生成的 Shell 命令可以处理项目文件，但不能因为 Docker 不可用就静默跑到宿主机。

本章不实现文件搜索、patch 冲突、审计日志、快照或跨进程人工审批。它们是其他能力，不应让第一版沙箱失去可解释性。

## 2. Gate 和 Sandbox 是两条边界

```text
ToolCall
  -> ApprovalPolicy             是否获得用户许可
  -> ToolExecutionGate         该 Tool/effect 是否允许进入
  -> BashTool / legacy SandboxedShellTool
  -> ShellExecutor              Bash 语义、工作目录和超时策略
  -> ProcessSandbox             exact argv 实际在哪里运行
  -> Docker container
```

`DeclaredSideEffectGate` 信任 Tool 的 `side_effects` 声明，所以只能管理已经信任的 Python Tool。`ProcessSandbox` 不执行任意 Python handler；它只负责一个更窄、可以被系统边界约束的对象：子进程命令。

这种窄接口避免了“所有 Tool 看起来都经过 Sandbox，实际插件仍在宿主进程中”的虚假安全感。

## 3. ProcessSandbox 契约

```python
class ProcessSandbox(Protocol):
    async def run(
        self,
        request: SandboxProcessRequest,
    ) -> SandboxProcessResult: ...
```

请求有四个核心字段：

- `argv`：在隔离环境中执行的精确参数元组，`ProcessSandbox` 不隐式选择 Shell；
- `workdir`：必须位于容器内 `/workspace` 下的绝对 POSIX 路径；
- `mode`：`read-only` 或 `workspace-write`；
- `timeout_seconds`：进程沙箱自己的终止期限。

结果除了退出码和输出，还必须报告 `enforcement`。Consumer 默认只接受 `full`，避免把 Windows ACL 等 partial backend 当成完整隔离。

V1 没有 `danger-full-access`。该模式的真实含义是“不要隔离”，应由独立可信执行器和明确审批表达，不应伪装成 Docker sandbox 的一种安全模式。

## 4. Docker Provider 具体限制什么

`DockerProcessSandbox` 生成的容器具有以下边界：

- 只挂载一个经过 `Path.resolve(strict=True)` 规范化的 workspace；
- `read-only` 将 workspace 只读挂载，`workspace-write` 只让该挂载可写；
- 容器根文件系统始终只读；
- `/tmp` 是有大小限制的临时文件系统；
- 默认 `--network none`；
- `--cap-drop ALL` 与 `no-new-privileges`；
- 显式 CPU、内存和进程数上限；
- 不把宿主环境传进容器，只有构造时显式给出的键值进入；值通过 Docker client 环境转交，不拼进命令行参数；
- `--pull never`，运行过程中不下载镜像；
- stdout/stderr 分别保留有界尾部；超过上限时丢弃该流头部并设置独立 truncation fact，不因为日志量改变命令退出语义。

Docker 可执行文件不存在或 Docker 返回 runtime exit code 125 时，Provider 抛出 `SandboxUnavailableError`。没有宿主执行 fallback。

## 5. 为什么超时不能只包在 Agent 外层

如果 Agent 只对 `tool.execute()` 使用 `asyncio.timeout()`，取消 Docker 客户端不一定删除仍在后台运行的容器。

所以 Provider 自己拥有清理协议：

```text
timeout / cancellation
  -> docker rm --force <container>
  -> kill local docker client if still running
  -> propagate SandboxTimeoutError / CancelledError
```

Agent 外层取消仍然传播，但资源释放发生在真正拥有容器生命周期的 Provider 中。

## 6. 组装方式

```python
process_sandbox = DockerProcessSandbox(
    workspace_root=".",
    image="my-agent-image:locked",
    network_enabled=False,
)
executor = SandboxedBashExecutor(
    process_sandbox,
    mode="workspace-write",
)
bash = BashTool(executor, requires_approval=True)
agent = Agent(
    AgentComponents(
        provider,
        tools=(bash,),
        approval_policy=my_policy,
        execution_gate=DeclaredSideEffectGate(
            allowed_side_effects=("none", "write"),
            allowed_tools=("bash",),
        ),
    )
)
```

新路径的镜像必须预先存在并包含 `/bin/bash`。兼容期 `SandboxedShellTool` 会显式使用 `/bin/sh -lc`。建议使用固定 tag 或 digest，并只安装任务真正需要的编译器和工具链。

可运行示例：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\20_docker_workspace_sandbox.py `
  --image your-prepulled-image:tag `
  --workspace .\python-pi-agent
```

示例使用 `FakeProvider`，因此不调用付费模型；Shell 命令仍真实进入 Docker，并在 workspace 中创建 `sandbox-demo.txt`。

## 7. 当前保证与明确不保证

当前保证：

- Shell 不会在 Docker 不可用时回退到宿主执行；
- 容器只能看到显式挂载的 workspace；
- 超时和取消拥有清理路径；
- 网络、环境和基本资源默认最小化；
- 调用方可以从结果验证 enforcement level。

当前不保证：

- Docker daemon 本身被攻破后的宿主安全；
- 任意 `FunctionTool` 或第三方 Python 插件受到隔离；
- workspace 写入可撤销；
- 文件 read/search/patch 工具已经统一走 workspace port；
- `danger-full-access` 的单次升级审批；
- Docker 镜像供应链安全。

对未知 Python 插件或高风险仓库，应把整个 Harness 放入容器、MicroVM 或远端 worker，而不是只隔离 Bash 子进程。

## 8. 与 Codex、DeepSeek Harness、Pi 的映射

- Codex 把 Harness 控制面与 sandbox execution plane 分开；这里同样让模型、审批和会话留在宿主，让 Shell 进入隔离环境。
- DeepSeek Harness 的本地 sandbox 强调 per-call policy、fail closed 和 enforcement completeness；这里保留这些语义，但首个 Provider 只实现 Docker，避免一开始维护多个平台 runner。
- 固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 没有内建通用 Sandbox；其安全设计同样警告进程内局部拦截容易形成虚假安全感。本项目因此把 Gate 与 OS/容器执行边界显式拆开。

## 9. 验证与掌握标准

运行聚焦测试：

```powershell
.\.venv\Scripts\python.exe -m unittest python-pi-agent/tests/test_sandbox.py -v
```

本机已预拉镜像且 Docker daemon 可用时，显式运行真实边界测试：

```powershell
$env:PI_AGENT_DOCKER_IMAGE = "python:3.12.8-slim-bookworm"
.\.venv\Scripts\python.exe -m unittest python-pi-agent/tests/test_sandbox_integration.py -v
```

该测试默认跳过，不会在普通离线测试中拉取镜像。

掌握标准：

- 能解释为什么 `ToolExecutionGate` 不是沙箱；
- 能指出 Shell 的取消清理由哪个组件负责；
- 能从 Docker argv 判断 workspace、网络、环境和资源边界；
- 能解释为什么 Docker 不可用时必须 fail closed；
- 能判断何时只隔离 Shell 不够、必须隔离整个 Harness。
