# 第二十七章：让 Bash 工具真实进入 Docker

## 1. 这章只解决一个问题

第 26 章已经让模型只依赖稳定的 `BashTool -> ShellExecutor` 接缝。本章补上真实 Provider：把 Bash request 变成受约束的 Docker 进程，同时不把镜像、网络、环境变量或资源预算暴露给模型。

## 2. 完整调用链

```text
ToolCall(command, workdir?, timeout_seconds?)
  -> ApprovalPolicy
  -> ToolExecutionGate
  -> BashTool
  -> SandboxedBashExecutor.resolve()
       workspace 相对路径 -> /workspace/...
       默认 timeout -> 部署上限
  -> SandboxedBashExecutor.execute()
       exact argv = ("/bin/bash", "-lc", command)
  -> DockerProcessSandbox
  -> Docker container
```

Definition、Provider、Consumer 和 Evidence 分别落在 `ports.py`、`shell.py`、`sandbox.py` 与测试中。Agent loop 不需要知道 Docker 或 Bash 的存在。

## 3. 为什么 ProcessSandbox 必须接收精确 argv

`ProcessSandbox` 只负责“这个进程在哪里、以什么隔离强度运行”，不负责决定命令语言。请求如下：

```python
SandboxProcessRequest(
    argv=("/bin/bash", "-lc", "pytest -q"),
    workdir="/workspace/python-pi-agent",
    mode="workspace-write",
    timeout_seconds=120,
)
```

Docker Provider 把 argv 原样放在镜像名之后。若未来需要运行编译器、语言服务器或远端 worker，可以直接传它们的 argv，不必先包装成 Shell 字符串。兼容期 `SandboxedShellTool` 也必须明确写出 `/bin/sh -lc`，不再由 Provider 猜测。

## 4. 工作目录和超时在哪里解析

模型只允许提供 workspace 相对 POSIX 路径：

- 未提供或 `.` 映射为 `/workspace`；
- `packages/core` 映射为 `/workspace/packages/core`；
- `../outside`、`/tmp` 和 `packages\\core` 被拒绝；
- 模型请求的 timeout 可以缩短执行时间，但不能突破应用配置的 `max_timeout_seconds`。

这里只做词法边界；真正的宿主路径、符号链接和子进程访问仍由容器 mount、只读根文件系统和 capability 限制共同约束。

## 5. 输出为什么保留尾部而不是杀进程

stdout 与 stderr 分别维护固定大小的 byte tail。超限后丢弃该流最早的字节，并设置独立的 truncation fact：

```text
stdout: 12345, limit=3 -> 345, stdout_truncated=True
stderr: 67890, limit=3 -> 890, stderr_truncated=True
```

这保证内存有界，又不会因日志量改变命令是否完成及其副作用。代价是头部不可恢复；未来若加入完整输出 spill，必须同时定义 workspace 路径、清理、权限和生命周期。

## 6. 失败、超时和取消不是同一件事

- exit code 非零：命令确实运行并给出结果，`BashTool` 不把它伪装成基础设施错误；
- Docker 不可用、runtime exit 125、partial enforcement：Provider fail closed；
- timeout：Docker Provider 强制删除容器，Executor 返回 `timed_out=True`；
- 外部取消：清理后继续抛出 `CancelledError`，终止整个 Agent run；
- stdout/stderr 截断：只是输出事实，不改变 exit code。

## 7. 组装

```python
process_sandbox = DockerProcessSandbox(
    workspace_root=".",
    image="my-agent-image@sha256:...",
    network_enabled=False,
    max_output_bytes=1_000_000,
)
executor = SandboxedBashExecutor(
    process_sandbox,
    mode="workspace-write",
    default_timeout_seconds=120,
    max_timeout_seconds=600,
)
bash = BashTool(executor, requires_approval=True)
```

工具仍应经过 `ApprovalPolicy` 与 `ToolExecutionGate`。它们决定是否允许调用；Docker 负责进程和文件系统边界，二者不能互相替代。

## 8. 可运行轨迹

镜像必须已经存在且包含 `/bin/bash`。示例使用 `FakeProvider`，不会访问付费模型，但 Bash 命令会真实进入 Docker：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\27_docker_bash_executor.py `
  --image your-prepulled-image:tag `
  --workspace .\python-pi-agent
```

命令会在 workspace 中创建 `bash-executor-demo.txt`，并打印模型可见的 tool result 与最终回答。

聚焦测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p "test_shell.py"
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p "test_sandbox.py"
```

## 9. 与上游设计的映射

- Pi 的小循环和通用 Tool 契约保持不变，Bash 仍是外部 capability；
- DeepSeek Harness 的 Shell Definition/Provider/Consumer、request/spec 和 subprocess confinement 被压缩进单包内的清晰模块，而不是复制其多包规模；
- 本阶段主动省略 host-local trusted executor、后台 job、PTY、完整输出 spill、网络权限升级和多平台 native runner。

掌握标准：能从一条 `ToolCall` 追到最终 Docker argv，解释每一层拥有的默认值与失败语义，并证明 Docker 不可用时命令不会回退到宿主机。
