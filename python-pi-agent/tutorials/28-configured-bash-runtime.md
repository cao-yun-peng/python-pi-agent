# 第二十八章：从 agent.yaml 组装 Docker Bash Runtime

## 1. 这章只解决一个问题

第 27 章已经证明 Bash 可以安全进入 Docker，但示例仍手工创建每个对象。本章把可信部署配置、产品权限策略和运行对象接起来，使正式 Runtime 能从显式 YAML 获得 Bash capability。

## 2. 三类信息不能混在一起

```text
agent.yaml                    coding_profile()              build()
------------------------      -------------------------     -------------------
workspace / image             默认拒绝审批                  DockerProcessSandbox
timeout / output budget       bash-only Execution Gate      SandboxedBashExecutor
network / access mode                                       BashTool
```

- YAML 描述部署事实，不保存密钥，也不包含模型可以控制的参数；
- Profile 保存代码拥有的产品默认策略，不持有 Docker client 或 workspace 对象；
- Builder 在单次构建事务中创建运行对象，失败时不发布半成品 Runtime。

## 3. 配置格式

```yaml
version: 1
provider:
  name: deepseek
  api_key_env: DEEPSEEK_API_KEY

bash:
  provider: docker
  workspace_root: .
  image: local/pi-agent-python:locked
  docker_executable: docker
  mode: workspace-write
  default_timeout_seconds: 120
  max_timeout_seconds: 600
  max_output_bytes: 1000000
  network_enabled: false
```

`bash` 整节可省略；省略后 Builder 不注册该工具。相对 `workspace_root` 按配置文件目录解析，而不是按当前工作目录解析，因此从不同目录启动应用不会悄悄改变挂载范围。

镜像必须预先存在并包含 `/bin/bash`。运行时仍使用 `--pull never`，不会在一次 Agent run 中隐式下载镜像。

## 4. 为什么 Profile 不创建 Docker

`AgentProfile` 的 factory 应只创建轻量策略或工具定义默认值。Docker workspace 和镜像属于每次部署，若通过 closure 塞进 Profile，会让 Profile 难以测试、复用和比较。

`coding_profile()` 只做两件事：

- 使用 `DenyByDefaultApprovalPolicy`，没有审批适配器时所有 Bash 调用都拒绝；
- 使用只允许工具名 `bash` 的 `DeclaredSideEffectGate`。

应用若已获得可信的单次批准，可以显式覆盖 ApprovalPolicy：

```python
runtime = await (
    AgentRuntimeBuilder.from_loaded_config(
        loaded,
        provider_catalog=default_provider_catalog(),
    )
    .with_profile(coding_profile())
    .with_approval_policy(my_interactive_policy)
    .build()
)
```

Profile 允许 read/write/external 三种声明，不等于自动批准它们；工具名 allowlist 和 ApprovalPolicy 仍同时生效。

## 5. 网络为何改变副作用声明

配置为 read-only 时，Bash 声明 `side_effects="read"`；workspace-write 声明 `write`。若 `network_enabled: true`，无论文件模式是什么，都声明更强的 `external`。

这让审批 UI、Gate 和事件日志看到真实风险。网络开关只说明容器具备网络能力，每一次模型命令仍必须通过审批。

## 6. 失败路径

- workspace 不存在：Builder 构造 Docker Provider 时失败并回滚；
- Docker executable 或 daemon 不可用：执行时抛出 `SandboxUnavailableError`，不会回退宿主；
- 配置字段未知、类型错误或默认 timeout 大于上限：加载阶段 `ConfigurationError`；
- 没有 ApprovalPolicy：命令在启动 Docker 前被拒绝；
- Docker 只能报告 partial enforcement：Executor fail closed。

模型会得到稳定、无敏感细节的 tool error，从而可以解释失败或选择不再执行。

## 7. 可运行轨迹

复制并修改顶层 `agent.yaml.example`，确保镜像已预拉取，然后运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\28_configured_bash_runtime.py `
  --config .\agent.yaml
```

示例使用 `FakeProvider` 和固定的只读 Bash 命令，因此不调用付费模型；Bash 仍真实进入配置指定的 Docker 容器。

聚焦验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p "test_configuration.py"
.\.venv\Scripts\python.exe -m unittest discover -s python-pi-agent/tests -p "test_app_runtime.py"
```

## 8. 主动省略

本阶段不实现 CLI 人工审批、镜像发现或拉取、任意环境变量透传、完整输出 spill、CPU/内存配置覆盖和单次网络权限升级。这些能力必须各自定义权限、生命周期与测试，不能通过增加几个松散 YAML 字段完成。

掌握标准：能解释为什么 YAML、Profile 和 Builder 分别拥有不同信息，并能证明配置缺失、审批缺失或 Docker 缺失时都不会执行宿主 Shell。
