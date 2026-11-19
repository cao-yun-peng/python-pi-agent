# ADR 0041：WorkspaceFiles 与有界文本 read

状态：已接受。关联 ADR 0014、0020、0027、0035、0040。

## 背景

Bash 已隔离到 workspace，新增文件工具需要共享路径/权限与预算边界。直接用宿主 Path.read_text 会绕开现有隔离，也难以确定性验证取消、分页和字节限制。

## 决定

1. domain 定义读取请求、预算和结果，ports.WorkspaceFiles 首先仅提供 read_text；未来写入使用独立明确的契约，不加入占位方法。ReadTool 消费该端口，经既有 schema/审批/Gate/timeout/事件/Store 管线，不改 loop。
2. 只接受相对 POSIX 路径、正 offset/limit；服务限制文件字节、页字节和行数。请求默认 200 行，服务默认 1 MiB/50 KiB/2000 行。UTF-8-sig 解码、NUL 拒绝、完整行分页保留换行；超长单行明确失败。完整原字节版本与下一 offset 随结果持久化。
3. InMemoryWorkspaceFiles 位于 testing，快照初始字节目录，复用正式投影函数。SandboxedWorkspaceFiles 借用 ProcessSandbox，以固定隔离 Python argv、只读挂载、full enforcement 读取 /workspace；没有宿主回退。Linux 逐组件 no-follow 打开、非阻塞打开和 regular-file 检查；有限读、变更检测、安全错误码和响应限额。
4. ReadTool 默认无需独立审批，声明 read、可重试但不可重放；Gate 默认仍拒绝 read。read_only_profile 仅允许 read，coding_profile 扩充为 bash/read；工具和 backend 均需显式组装。借用服务无空闲资源，每次进程由 sandbox 取消/关闭；PluginAPI 注册句柄由 Host 撤销。
5. 正文预算不包括固定提示和 metadata。版本用于识别内容变化，不承诺跨进程事务；完整文件超预算即失败，不边截断边伪造完整版本。schema 支持检查与完整输出 schema 的通用演进不在此阶段。
6. 文件能力源码归入 tools/workspace/：read.py 为模型工具，sandbox_backend.py 为文件访问后端与共用投影。__init__.py 无自动注册或重导出，稳定 api/根包导入不变。通用 tools/sandbox.py 和 shell.py 保持原职责与路径。

## 备选方案

宿主 resolve 后 open 存在链接检查竞态，尤其跨平台难以给出统一强保证；asyncio.to_thread 取消不能停止底层读。调用 Bash 拼接 cat/sed 会引入 Shell 引用和错误语义问题。选择现有沙箱 + 小型固定 helper，代价是要求本地 Linux Python 镜像及足够的捕获预算（base64 约 4/3 文件预算）。读完任意大文件再截断不符合内存预算，故在 helper 内限制原始读取。

## 后果

稳定 API 增加文件端口、DTO、错误、ReadTool、沙箱适配器和 read_only_profile；Fake 从 testing 导入。没有新增依赖、全局注册、YAML 分支或后台任务。大文件、图片、write/edit、完整版本快照和 spill 后续独立设计。只读挂载不阻止其他进程修改同一 workspace；检测到变化时失败，版本不替代并发控制。

后续状态：ADR 0042 已实现可选修改/查询契约和五个工具，并扩充 profile allowlist；本 ADR 的 read 接口和默认只读 backend 保持兼容。

## 验证

test_workspace_read 覆盖路径、编码、BOM/CRLF、EOF、行/字节预算、超长行、版本、Gate/审批、缺失文件、raw 响应错误、full enforcement、超时及取消；FakeProvider 证明错误回填与模型续轮。POSIX helper 与显式 Docker 集成验证真实链接/FIFO/目录/只读挂载。示例 42、教程 39 和知识地图跟踪交付。
