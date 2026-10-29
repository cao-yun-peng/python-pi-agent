# 第二十六章：把 Bash 工具与执行环境分开

## 1. 这章只解决一个问题

目标是给模型一个稳定的 `bash` 工具，同时不让工具 schema、Bash 默认值和 Docker 细节绑定在同一个类里。

本章建立 Definition、Consumer 和 Fake Provider。真实 Docker Provider 在下一阶段接入，因此本章示例不会启动进程、访问网络或调用付费模型。

## 2. 能力位置

```text
ToolCall
  -> ApprovalPolicy
  -> ToolExecutionGate
  -> BashTool                 Consumer：模型参数与结果文本
  -> ShellExecutor            Definition：resolve + execute
  -> DemoShellExecutor        Provider：本章的确定性替身
```

旧的 `SandboxedShellTool -> ProcessSandbox` 仍然可用。本章先建立新接缝，避免在同一个提交中同时改变 Docker argv、输出收集和既有示例。

## 3. Request 和 Spec 为什么分开

模型产生的是请求：

```python
ShellExecRequest(
    command="python -m unittest -q",
    workdir="python-pi-agent",
    timeout_seconds=30,
)
```

Provider 的 `resolve()` 负责填充默认工作目录、默认超时并施加上限，返回字段完整的 `ShellExecSpec`。`execute()` 只接受 spec，不在真正启动命令时重新猜默认值。

这个边界还限制了模型输入面。`BashTool` 只暴露 `command`、`workdir` 和 `timeout_seconds`，不会把未知参数、环境变量、stdin、镜像名或输出预算透传给 Provider。

## 4. 结果语义

`ShellRunResult` 分开报告：

- `exit_code` 与 `signal`；
- `timed_out` 与 `cancelled`，两者按第一原因互斥；
- stdout/stderr 文本、截断和可选恢复路径；
- 可选的沙箱 mode、enforcement、denied 和 runner failure。

命令退出 1 仍然表示 Shell Provider 成功执行并观察到了命令结果，所以 `BashTool` 返回普通 `ToolOutput`，并在模型文本中加入 `[exit code: 1]`。无法启动执行环境等基础设施故障由 Provider 抛出，交给 runtime 的稳定错误边界处理。

如果 Executor 返回 `cancelled=True`，`BashTool` 重新抛出 `CancelledError`，确保取消继续终止整个 run，而不是伪装成普通工具结果。

## 5. 可观察轨迹

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\26_bash_tool_contract.py
```

示例依次打印原始 request、解析后的 spec、执行参数和最终 assistant 文本。它使用 `FakeProvider` 和 `DemoShellExecutor`，因此输出完全确定。

## 6. 失败路径与测试

聚焦测试：

```powershell
.\.venv\Scripts\python.exe -m unittest python-pi-agent/tests/test_shell.py -v
```

测试覆盖：

- schema 只暴露模型安全参数；
- request 必须先 resolve，再 execute；
- 非零退出码保留为命令结果；
- 超时、输出截断和沙箱拒绝可以同时观察；
- 取消继续传播；
- Agent 的 approval → gate → BashTool → executor 完整路径。

## 7. 与上游设计的映射

- Pi 提供小 Agent loop、工具 schema 和执行 context，本项目继续让 runtime 只认识通用 `Tool`。
- DeepSeek Harness 把 model-facing bash tool、ShellExecutor、subprocess 和 sandbox 拆成独立 capability；本项目借鉴 request/spec 和 Definition/Provider/Consumer，但继续使用单一 Python 包。
- 本阶段主动省略后台 job、持久 PTY、host-local Bash、环境透传和单次权限升级。

掌握标准：能够替换 `ShellExecutor` 而不修改 `BashTool`，并能解释命令失败、执行环境失败和 run 取消为什么是三种不同结果。
