# ADR 0025: ShellExecutor 与模型侧 Bash Consumer 分离

## 背景

现有 `SandboxedShellTool` 直接消费 `ProcessSandbox`，适合证明“模型命令不会回退到宿主执行”的最小 Docker 纵向切片，但它同时拥有模型 schema、Shell 默认值、沙箱 mode 和结果渲染。底层 `DockerProcessSandbox` 又固定使用 `/bin/sh -lc`，因此工具名称、Bash 语义与隔离 Provider 无法独立替换。

## 决定

- 新增纯领域值 `ShellExecRequest`、`ShellExecSpec`、`ShellRunResult`、`ShellCollectedOutput` 和 `ShellSandboxInfo`；
- 新增 `ShellExecutor` Port，固定使用 `resolve(request) -> spec` 和 `execute(spec) -> result` 两阶段契约；
- 新增模型侧 `BashTool` Consumer，只暴露 `command`、`workdir` 和有部署上限的 `timeout_seconds`；
- `BashTool` 从命名字段创建 request，禁止把任意模型参数透传给 Executor；
- 非零 exit code 是已观察到的命令结果，不标记为 Tool 基础设施错误；
- timeout、signal、输出截断和沙箱事实正交报告；取消继续以 `CancelledError` 传播；
- 第一批保留 `SandboxedShellTool` 和原有 Docker 行为，下一批通过独立 Provider 把 `ShellExecutor` 接到 `ProcessSandbox`。

## 备选方案

1. 直接把 `SandboxedShellTool` 改名为 `BashTool`：拒绝，会把 `/bin/sh` 行为伪装成 Bash，并在一个变更中破坏现有教程和测试。
2. 让 `BashTool` 直接使用 `asyncio.create_subprocess_shell`：拒绝，会绕过已有 fail-closed Docker 边界并把宿主执行变成隐式默认。
3. 把 Docker image、环境、stdin 和输出上限都暴露给模型：拒绝，这些是可信应用或 Provider 配置，不是模型参数。
4. 一开始复制 DeepSeek Harness 的 subprocess/jobs/terminal 多包结构：拒绝，当前规模只需要单包内清晰模块和可替换 Port。

## 后果

- `BashTool` 可以通过 FakeExecutor 离线确定性测试；
- Docker、远程 worker 或未来的可信本地实现可以替换 Executor，而不改变模型 schema；
- request/spec 分离为工作目录规范化、超时封顶和精确 argv 留出单一解析点；
- 公共 API 暂时同时存在 `SandboxedShellTool` 和 `BashTool`，迁移完成后再单独决定旧接口的弃用策略；
- 第一批尚不能真实执行 Bash，正式应用不得把 FakeExecutor 当成执行 Provider。

## 验证方式

- FakeExecutor 记录 request/spec，证明调用顺序和命名字段投影；
- schema 测试证明模型不能设置 env、stdin、镜像或输出预算；
- 结果测试覆盖非零退出、timeout、截断、沙箱拒绝和取消；
- Agent 集成测试覆盖 ApprovalPolicy、ToolExecutionGate、BashTool 与 Executor 的完整调用链；
- 旧 sandbox 测试保持通过，证明本批未改变 Docker 安全边界。
