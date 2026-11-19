# 第 40 章：write/edit 与有界 ls/find/grep

本章沿用 read 的 workspace 边界，补齐五个工具。主循环和模型 Provider 不需要认识文件工具：仍然走 ToolCall → schema → 审批 → Gate → Tool → middleware → durable tool result → 模型续轮。

运行：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\43_workspace_file_tools.py
```

示例只使用内存文件与 FakeProvider，不访问网络或真实文件。依次打印 ls/find/grep、新建、编辑、旧版本拒绝和最终读取，并观察 tool_approval_*、tool_execution_*、message_committed。所有工具导入正式包，没有教程专属实现。

## 定义与使用

| 工具 | 参数 | 语义 |
|---|---|---|
| write | path, content, expected_version? | 缺省版本只创建；覆盖已有文本必须提供 read 返回的完整字节版本；创建父目录 |
| edit | path, old_text, new_text, expected_version? | old_text 非空且唯一（包括重叠匹配）；可选版本检查；保留 BOM 和未修改区域的换行 |
| ls | path? | 默认 .，只列一层；标记目录/符号链接，不跟随链接 |
| find | pattern, path? | 文件名 glob；含 / 时按路径段匹配，** 匹配零或多层目录；结果按确定的目录遍历顺序 |
| grep | pattern, path? | 大小写敏感、逐行字面量子串；目录或单文件；返回路径、1-based 行号和有界文本 |

write/edit 声明 write、副作用不可安全重试/重放，默认要求审批。只读工具声明 read，默认不单独询问审批，可设置 requires_approval=True；所有工具仍需 Gate 放行。coding_profile 允许显式注册的 bash 和六个文件工具；read_only_profile 只允许 read/ls/find/grep。Profile 不自动创建工具或 backend。

WorkspaceFiles 保留原 read_text 接口，另以 WorkspaceMutations/WorkspaceQueries 表达可选能力，不要求现有只读适配器实现写入。SandboxedWorkspaceFiles 和内存替身实现三者；allow_write 默认 False，需要明确开启。数据 DTO、工具类与协议通过 pi_agent_py.api 导出。

## 写入、版本、取消

write/edit 共用后端实例的异步修改锁，先校验原文件、版本和文本，再在同目录写受控临时文件并 fsync。新文件使用不覆盖的原子发布，已有文件使用原子替换；不会先清空目标文件。版本冲突、找不到旧文本或匹配多处都不修改目标。编辑时只对匹配建立 LF 视图，替换内容采用原文件的首个换行约定，未触碰字节的 BOM/混合换行保留。diff 是有界预览（各取至多 4096 字符、输出至多 8 KiB），diff_truncated 明确说明不完整。

锁只协调同一服务实例；其他实例、Bash、编辑器和外部进程不受它约束。expected_version 是冲突检测，不承诺跨进程 CAS。新建的排他发布避免并发创建覆盖；强一致共享写入需要后续独立能力。

文件内容通过新增 SandboxProcessRequest.stdin 传给固定 Python helper，不进入 argv、Shell 或 request repr。DockerProcessSandbox 并发发送 stdin、捕获有界 stdout/stderr，并继续负责超时/取消后的进程清理。文件 helper 使用同一包里的纯函数源码，内存替身复用匹配和 diff 规则，无 import 阶段 I/O。

取消会传播，不伪造成功结果。进程停止后，在释放修改锁之前尝试清理本次操作的随机临时文件；只按明确名字 unlink，不递归清理其他文件。结果丢失、传输/协议失败或超时返回 outcome_unknown，不自动重做写入；目标可能已经完整提交。清理无法确认时保留安全诊断 note。强杀 harness 或外部反复取消仍可能留下临时文件/空父目录，不宣称文件系统事务回滚。

## 查询预算与主动省略

WorkspaceQueryLimits 默认：200 条结果、10,000 个扫描项、8 MiB 总扫描字节、50 KiB 结果行 JSON 预算、匹配行 500 字符、32 层深度。原始单文件仍受 WorkspaceReadLimits.max_file_bytes 约束。大目录超过安全收集预算报 scan_limit，结果/深度/总扫描预算用 truncated 标记；长匹配行独立标记。固定 envelope 和工具提示不计入行预算。

默认不递归进入 .git/.venv/node_modules/__pycache__；显式选择这些目录作为根仍可查询。grep 跳过二进制、过大或不可读文件，skipped_files 提供证据；目录变更/不可访问的子树使结果不完整。暂不支持 regex、.gitignore、模糊匹配、批量 edit 或任意宿主访问。

真实使用时，将已有 DockerProcessSandbox 注入 SandboxedWorkspaceFiles(..., allow_write=True)，明确选择工作区、审批与 Gate；镜像需已存在且包含 Linux Python。实例同时为读/查询申请只读挂载，为已授权修改申请 workspace-write。默认测试完全离线；设置 PI_AGENT_DOCKER_IMAGE 可启用 test_workspace_tools 的真实大 stdin、版本、链接及丢失结果清理验证。

掌握标准：能够解释为什么“可替换 backend”减少核心改动，却仍必须定义原子提交、版本冲突、未知结果和查询预算；能用测试证明审批/Gate 拒绝时零写入、取消后队列仍有序。Pi 符号与差异见 upstream-mapping、ADR 0042。
