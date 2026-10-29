# ADR 0020: 分离工具执行 Gate 与工作区进程沙箱

## 背景

ADR 0014 引入的 `DeclaredSideEffectSandbox` 能消费工具声明、审批决定和工具 allowlist，但仍然直接调用当前 Python 进程中的 handler。这个对象无法阻止 handler 调用 `open()`、`subprocess` 或网络，却使用了 Sandbox 名称，容易把策略检查误解为系统隔离。

编码 Agent 还需要运行模型生成的 Shell。仅检查命令字符串或工作目录不能阻止绝对路径、符号链接及子进程越界。真正的边界必须位于命令实际执行的位置，并且在边界不可用时拒绝运行。

## 决定

1. 将通用工具策略端口命名为 `ToolExecutionGate`；默认实现为 `DeclaredSideEffectGate`，可信工具的显式宽松实现为 `TrustedToolExecutionGate`。
2. Gate 只回答“是否允许调用这个 Python Tool”，不承诺文件、进程或网络隔离。拒绝事件改为 `tool_execution_denied`。
3. 新增独立 `ProcessSandbox` 端口。它只接收包含精确 argv 和容器工作目录的 `SandboxProcessRequest`，并返回带 `full/partial` enforcement evidence 的 `SandboxProcessResult`；Shell 选择属于上层 Consumer/Executor。
4. 首个 Provider 是 `DockerProcessSandbox`：Harness 留在宿主；仅挂载规范化 workspace；容器根文件系统只读；默认关闭网络；丢弃 Linux capabilities；启用 `no-new-privileges`、内存、CPU、进程数、临时目录和每流有界输出尾部。
5. Docker 或镜像不可用、Docker 自身返回运行错误、Provider 只能提供 partial enforcement 时默认 fail closed，绝不回退到宿主 Shell。
6. `SandboxedShellTool` 是第一个兼容 Consumer，显式生成 `/bin/sh -lc` argv，不直接调用本机 Shell；ADR 0025/0026 后由 `BashTool -> SandboxedBashExecutor` 成为新应用路径。取消和超时会强制移除容器并终止 Docker 客户端进程。
7. V1 只提供 `read-only` 与 `workspace-write`。`danger-full-access` 不是沙箱模式；未来若加入，必须作为显式可信执行器并通过单次升级审批暴露，不能在 Docker Provider 内伪造其语义。

## 备选方案

- 保留 `Sandbox.execute(tool)` 并把所有 Python Tool 放入子进程：需要序列化任意插件代码、上下文和生命周期，第一阶段复杂度过高，而且仍容易遗漏宿主资源。
- 只做路径字符串检查：不能约束 Shell 子进程、符号链接、junction 或解释器代码，不构成执行边界。
- 第一阶段同时实现 Landlock、bubblewrap、Seatbelt 和 Windows restricted token：平台测试面过大，不适合当前教学规模。
- Docker 不可用时自动调用宿主 Shell：可用性更高，但违反 fail-closed，并让相同配置在不同机器拥有完全不同的安全含义。
- 直接把整个 Harness 放入容器：隔离更完整，但模型密钥、会话存储和插件控制面也进入不可信执行环境；留作应用部署 profile，而不是核心循环默认行为。

## 后果

- `Agent` 与 `AgentSession` 的组装参数从 `sandbox` 改为 `execution_gate`，这是一次有意的 pre-1.0 公共接口修正。
- 普通 Python Tool 仍然受 Gate 管理，但只有显式使用 `ProcessSandbox` 的 Tool 才获得进程隔离。
- Docker Provider 需要应用预先准备镜像；`--pull=never` 保证一次 Agent run 不会隐式联网或改变镜像版本。
- 宿主工作区在 `workspace-write` 下仍会被真实修改。沙箱限制写入范围，不等于撤销或事务。
- 当前 Provider 隔离 Shell，不自动隔离任意第三方 Python 插件。运行未知插件仍应把整个 Harness 放进容器、VM 或远端环境。
- 文件 read/search/patch 工具尚未实现；它们后续应共享同一规范化 workspace 契约，而不是各自发明路径规则。

## 验证

- 纯函数命令构造测试证明只挂载一个 workspace、默认网络关闭、容器根只读、环境变量仅来自显式配置。
- Fake process 测试覆盖成功、Docker runtime 失败、每流有界尾部、零等待超时和外部取消。
- 超时和取消测试证明会执行 `docker rm --force <container>` 并终止 Docker 客户端。
- Fake `ProcessSandbox` 证明 `SandboxedShellTool` 传递 mode/timeout，并默认拒绝 partial enforcement。
- 显式启用的 Docker 集成测试验证 workspace-write 成功、read-only 拒绝和 `/workspace/..` 逃逸拒绝；默认测试不依赖 Docker。
- `examples/20_docker_workspace_sandbox.py` 使用正式包和 `FakeProvider` 演示从 ToolCall 到真实 Docker workspace 写入的完整路径。
