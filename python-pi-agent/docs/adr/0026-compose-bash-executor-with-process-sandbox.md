# ADR 0026: 用精确 argv 组合 Bash Executor 与 ProcessSandbox

## 背景

ADR 0025 已把模型侧 `BashTool` 与 `ShellExecutor` 分开，但第一批只有确定性的 Fake Provider。原有 `DockerProcessSandbox` 接收 Shell 文本并在内部固定 `/bin/sh -lc`，因此底层隔离端口仍偷偷拥有 Shell 语义；它也在 stdout/stderr 总量超过上限时终止容器，把“日志很多”变成了命令控制流失败。

我们需要一个真实 Provider，同时保持 Bash 语义、进程隔离、输出收集和模型 schema 可以独立演进。

## 决定

1. `SandboxProcessRequest` 改为接收非空 `tuple[str, ...]` 精确 argv，以及容器内 `/workspace` 下的绝对 `workdir`。`ProcessSandbox` 不再选择或拼接 Shell。
2. 新增 `SandboxedBashExecutor`，作为 `ShellExecutor` Provider 消费 `ProcessSandbox`。它把模型工作目录解释为 workspace 相对 POSIX 路径，拒绝绝对路径、`..` 和反斜杠语法。
3. Executor 负责默认超时和部署上限，并固定生成 `("/bin/bash", "-lc", command)`；镜像、网络、环境、资源预算和输出预算仍属于可信应用配置。
4. `DockerProcessSandbox` 把 exact argv 原样放在镜像名之后，并把已校验的 `workdir` 传给 Docker `--workdir`。
5. stdout 与 stderr 各自最多保留 `max_output_bytes` 字节的尾部，并分别报告 `stdout_truncated` / `stderr_truncated`。输出截断不终止命令。
6. Docker runtime 不可用、exit 125 或 partial enforcement 仍然 fail closed。`SandboxTimeoutError` 由 Executor 转成正交的 `timed_out=True` 命令结果；外部取消原样传播，并由 Docker Provider 清理容器。
7. 兼容期 `SandboxedShellTool` 保留，但必须显式生成 `/bin/sh -lc` argv。删除不再可达的 `SandboxOutputLimitError` 公共导出。

## 备选方案

1. 继续让 `ProcessSandbox` 接收 Shell 文本：拒绝，Provider 会隐式决定 `/bin/sh`、PowerShell 或其他解释器，无法证明调用者请求的真实进程。
2. 直接在宿主机使用 `asyncio.create_subprocess_exec`：拒绝，这会绕过已有 fail-closed workspace 边界。
3. 输出达到上限就杀进程：拒绝，日志量不应改变命令副作用是否完成；调用方需要的是有界内存和明确的截断事实。
4. 把完整输出自动写到宿主临时目录：暂缓，spill 需要定义路径权限、生命周期、清理和模型可见性，不能作为隐藏副作用加入本阶段。
5. 接受绝对容器路径作为模型 workdir：拒绝，模型侧契约只允许 workspace 相对路径，避免把容器布局变成模型输入面。

## 后果

- `BashTool` 不变即可从 Fake Provider 切换到真实 Docker 执行；
- `ProcessSandbox` 可承载未来的非 Shell 工具或远端 worker，而不必伪装成命令字符串接口；
- `SandboxProcessRequest` 是一次 pre-1.0 契约修正：原 `command` 字段调用方必须改为显式 argv；
- 有界尾部能防止无限输出占满内存，但截断头部不可恢复；完整输出 spill 留给独立生命周期设计；
- timeout 结果目前不携带被终止前的部分输出，因为底层异常尚未定义安全的 partial-result 契约；
- Docker 镜像必须预先存在并包含 `/bin/bash`。兼容工具仍只要求 `/bin/sh`；
- Shell 子进程被隔离不代表未知 Python 插件或整个 Harness 已被隔离。

## 验证方式

- `test_shell.py` 证明相对工作目录映射、逃逸拒绝、超时封顶、固定 Bash argv、partial enforcement 拒绝、结果投影和取消传播；
- `test_sandbox.py` 证明 exact argv 不被改写、`--workdir` 映射、每流有界尾部、runtime failure、timeout 与取消清理；
- `test_sandbox_integration.py` 继续显式验证 Docker workspace 写入、只读拒绝和逃逸边界，默认离线测试不拉取镜像；
- `examples/27_docker_bash_executor.py` 使用 `FakeProvider` 驱动正式 Bash 工具进入真实 Docker，不调用付费模型。
