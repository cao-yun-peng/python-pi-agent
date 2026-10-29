# ADR 0027: 在应用边界配置并组装 Docker Bash

## 背景

ADR 0026 已提供 `BashTool -> SandboxedBashExecutor -> DockerProcessSandbox`，但正式应用仍需手工传入 workspace、镜像、超时、输出预算和权限策略。若把已构造的 Sandbox 或 Executor 放进 `AgentProfile`，Profile 就会持有环境相关 I/O 对象；若让 `BashTool` 自己读取 YAML，又会让 Consumer 反向依赖应用配置。

我们需要让部署配置、产品策略和运行对象保持分离，同时让 Docker 不可用时继续 fail closed。

## 决定

1. `AppConfigSpec` 增加可选 `bash` 节。缺省表示不注册 Bash capability；配置出现时必须显式声明 `provider: docker` 和预先存在的镜像。
2. `DockerBashSettings` 只保存非秘密值：配置文件相对的 workspace、镜像、Docker 可执行文件、访问模式、默认/最大超时、每流输出预算和网络开关。
3. 相对 `workspace_root` 以 `agent.yaml` 所在目录为基准解析成绝对路径，不搜索父目录，也不依赖启动进程的当前目录。
4. `AgentRuntimeBuilder.from_loaded_config()` 在 build 阶段创建 `DockerProcessSandbox`、`SandboxedBashExecutor` 和 `BashTool`。直接 `with_provider()` 的代码组装路径不隐式读取配置或添加工具。
5. 新增不持有活 I/O 的 `coding_profile()`：审批默认拒绝，Execution Gate 只允许名为 `bash` 的工具进入，并依据工具声明允许 read/write/external。
6. Bash 始终 `requires_approval=True`。`read-only` 声明 `read`，workspace-write 声明 `write`；显式启用容器网络时声明更强的 `external`，避免把网络副作用伪装成文件写入。
7. Docker executable、镜像或 workspace 不可用时，构建或执行失败关闭；不得回退宿主 Shell。配置诊断只报告非秘密 Bash 设置。

## 备选方案

1. 在 `coding_profile()` 的 tool factory 中捕获 `DockerProcessSandbox`：拒绝，Profile 会携带环境相关实例，破坏代码默认值与活 I/O 的边界。
2. Builder 总是注册 Bash 并使用当前目录：拒绝，会让最小应用意外获得高风险能力，并使 workspace 随启动目录漂移。
3. 让模型选择镜像、网络或输出预算：拒绝，这些是可信部署配置，不是模型参数。
4. 配置 `requires_approval: false`：拒绝，静态配置不应静默绕过受保护工具审批；预授权应用可以显式注入自己的 `ApprovalPolicy`。
5. 网络开启后仍声明 `write`：拒绝，Gate 与审计会得到错误的副作用范围。

## 后果

- 同一个 YAML 可以确定性地组装模型 Provider、resilience 和 Docker Bash capability；
- `minimal_profile()` 继续不开放 Bash，`coding_profile()` 也不会自动批准命令；
- workspace 是否存在由 Docker Provider 构造阶段验证，错误被 Builder 回滚并包装为稳定的 `RuntimeBuildError`；
- `network_enabled: true` 是部署级能力开关，但每次 Bash 调用仍需 ApprovalPolicy 批准；
- 当前配置只覆盖 Docker Provider 的必要参数，CPU、内存、PID 和 tmpfs 暂用代码安全默认值；
- 本阶段没有 CLI 交互审批，也没有完整输出 spill，二者继续作为独立能力演进。

## 验证方式

- 配置测试覆盖相对 workspace 解析、默认关闭、未知字段、类型、timeout 次序、输出预算和网络布尔值；
- Runtime 测试使用 FakeProvider 和不存在的 Docker executable，证明工具被组装、网络模式声明 `external`、审批后仍在执行边界不可用时 fail closed；
- 既有 Builder 回滚、Profile、Provider resilience 和完整离线测试继续通过；
- `examples/28_configured_bash_runtime.py` 使用 FakeProvider 驱动从 YAML 到真实 Docker Bash 的完整路径，不调用付费模型。
