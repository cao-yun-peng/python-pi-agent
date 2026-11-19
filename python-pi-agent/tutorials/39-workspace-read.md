# 第 39 章：统一 workspace 文件端口与 read

本章解决“模型如何读取配置工作区内的一页文本”。位置是模型 ToolCall → 通用校验/审批/Gate → ReadTool → WorkspaceFiles → 有界结果 → durable tool message → 模型续轮。主循环不依赖文件系统或 Docker。

运行不需要模型、Docker 或真实文件的例子：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\42_workspace_read.py
```

例子导入正式包，通过 PluginAPI 注册 ReadTool，使用 read_only_profile 和内存文件目录。第一轮读取第 1 行，第二轮读取第 2–3 行并请求缺失文件；打印既有 tool_execution_*、message_committed 等事件、正文与 metadata。两页版本一致，缺失文件是模型可见错误，Runtime 关闭撤销注册。

## 契约与语义

- Definition：WorkspaceReadRequest(path, offset=1, limit=200)、WorkspaceReadLimits 和 WorkspaceReadResult。
- Provider：testing.InMemoryWorkspaceFiles 和 tools.workspace.sandbox_backend.SandboxedWorkspaceFiles。
- Consumer：ReadTool，只依赖 ports.WorkspaceFiles。read 声明 side_effects=read、retry_safe=True、replay_safe=False；默认不单独询问审批，但必须由 Gate 明确允许。需要审批时设置 requires_approval=True。
- Lifecycle：文件服务与沙箱由应用显式传入。服务不持有空闲文件句柄/后台任务；每次沙箱请求拥有并释放进程，超时和取消交给既有 ProcessSandbox；PluginHost 拥有工具注册，不关闭借用依赖。
- Evidence：test_workspace_read、示例 42、ADR 0041。

文件工具源码集中在 tools/workspace/：read.py 提供 ReadTool，sandbox_backend.py 提供文件沙箱后端和共用文本投影。包的 __init__.py 仅作目录说明；应用仍通过 pi_agent_py.api 导入工具，通用 tools/sandbox.py 继续负责进程隔离。

路径只接受 workspace 相对 POSIX 文件名。拒绝绝对路径、Windows 盘符/反斜杠/ADS、`..`、`~`、NUL 和超过 4096 UTF-8 字节的路径。`./a//b` 规范为 `a/b`。schema 先检查参数形状；正整数与路径语义在 ReadTool 构造请求时检查，发生于审批/Gate 之后、任何文件 I/O 之前。错误默认不回显原始路径、参数和底层异常。

首版读取严格 UTF-8 文本，去除首个 UTF-8 BOM，保留原始换行；NUL 和非法 UTF-8 拒绝。行划分使用 Python splitlines(keepends=True)。offset 从 1 起，允许 EOF 后一行返回空页，更大的 offset 报错。默认请求 200 行，服务上限 2000 行、文本页 50 KiB、整个文件 1 MiB；模型只能请求行数，不能提高服务预算。完整行不能放入页预算时留到下一页；第一行本身超预算报 line_too_long，避免跳行或拆坏 UTF-8。提示后缀和固定 metadata 不计入文本页预算。

metadata 包含 path、offset、returned_lines、total_lines、total_bytes、version、next_offset。version 是完整原始文件字节（包括 BOM/CRLF）的 SHA-256，截断页不能当作完整文件。重试读取没有写入副作用，但不能保证外部文件未变化；replay 使用已提交正文，不重新读取文件。版本不是跨进程锁或文件系统事务。

## 受约束的真实后端

应用已有 DockerProcessSandbox 时可共享相同 workspace 配置，并确保输出捕获预算足以容纳 base64 的完整有界文件。例子真实模式：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\42_workspace_read.py --workspace E:\my-workspace --image python:3.12-slim --path notes/demo.txt
```

镜像必须已在本机，包含 Linux Python 3；不自动拉取、不回退宿主路径。服务以固定 argv 运行隔离 Python 程序，路径作为参数，未使用 Shell。每次读操作要求 full enforcement，workspace 挂载只读、默认网络关闭。Linux dir_fd + O_NOFOLLOW 逐组件打开目录与最终文件，拒绝全部符号链接；O_NONBLOCK + regular-file 检查避免 FIFO 阻塞。先检查大小，再最多读取预算加 1 字节；读取期间检测到大小或时间戳变化返回 changed。

固定 helper 只返回安全错误码或有界 base64。宿主再次检查 framing、截断和实际大小，之后统一分页。默认 30 秒沙箱期限、35 秒工具保险期限；外部取消不变成成功结果。沙箱启动失败/超时沿用既有错误分类；原始 OS 诊断只留异常链。硬链接、恶意外部并发修改和真实一致性快照不由路径规范化保证，工作区应由应用明确选择。

## 测试与后续

默认命令：`python -m unittest discover -s tests -p test_workspace_read.py -v`。内存/脚本化沙箱测试跨平台离线；POSIX 的真实 helper 测试在 Windows 明确跳过。设置 PI_AGENT_DOCKER_IMAGE 为本地 Python 镜像后，同一测试文件会额外验证真实只读挂载、symlink、FIFO、目录与分页，无网络或真实模型。

read_only_profile 仅放行注册的 read；coding_profile 放行显式注册的 bash/read，仍不自动创建工具或读取配置。下一阶段通过 WorkspaceFiles 的明确写入契约扩展 write/edit，再接 YAML capability；当前没有写入、二进制/图片读取、宿主直接文件后端或自动热重载。

Pi 固定 commit 与 ReadOperations/createReadToolDefinition/truncateHead 映射见 upstream-mapping。本实现选择严格 workspace 边界、双预算和明确续页，主动省略 Pi 的绝对路径、图片缩放、平台文件名模糊修复及 TUI renderer。
