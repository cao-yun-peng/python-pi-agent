# ADR 0042：Workspace 修改与查询能力

状态：已接受。关联 ADR 0002、0014、0020、0040、0041。

## 背景

read 的文件端口与隔离后端已具备。新增工具应复用同一管线，重点解决覆盖冲突、精确修改、共享写入顺序和有界搜索，不让主循环依赖文件语义。大内容通过 argv 在 Windows 上会受命令行长度限制，也不适合作为诊断文本。

## 决定

- 保留 WorkspaceFiles，只增加 WorkspaceMutations/WorkspaceQueries 可选协议与 DTO；工具位于 tools/workspace。Backend 与工具显式注册，allow_write 缺省 False，write/edit 默认要求审批，禁止自动重试或重放。
- write 缺省版本只创建，覆盖必须匹配版本；edit 单一非空唯一替换，可选版本检查，保留 BOM/未修改区域换行。有界 diff 仅作预览。
- 同一 backend 的修改锁串行化 write/edit，持有至执行/故障清理结束。Linux no-follow 目录描述符限定访问，同目录临时文件完成后排他发布或原子替换；随机临时名由应用端生成，故障后只清理该名字。取消传播；结果不确定返回 unknown，清理不等于重试或回滚。
- ProcessSandbox 请求增加隐藏 repr 的 bytes stdin；Docker 交互输入与输出捕获并发，保持输出预算、超时和取消清理。固定 helper 通过有界 stdin 收参数；共享纯匹配/查询函数，不在 import 时读文件或启动任务。
- ls 单层、find 文件 glob/路径段 **、grep 逐行字面量。扫描项/字节/深度/结果/行长均有明确预算，链接不跟随；常见生成目录默认不递归，完整 .gitignore/regex 后续独立实现。
- coding_profile 放行显式内置工具，read_only_profile 放行 read/ls/find/grep；默认 capability Catalog 不变，不新增 YAML 分支或热重载。

## 备选方案

新增每个工具时修改主循环会破坏端口边界；拼接 Shell 命令引入引用/注入与诊断歧义；先截断文件再覆盖或对全部写入自动重试会丢数据。复制 Pi 的进程级文件队列不符合本仓库规则，因此采用实例锁。给原只读端口强加修改方法会破坏已有适配器，选择能力隔离。

## 后果

核心循环/Store/Provider 未改。新建发布/替换要求 Linux 文件系统支持相应原子操作，不支持时失败，不回退直接写目标；权限、所有权和多进程事务不作超出实现的承诺。强杀或重复外部取消可能留下临时物/目录；unknown 不表示未写入。regex、.gitignore、YAML 文件能力配置与跨实例锁仍待后续。

## 验证

test_workspace_tools 验证内存与 FakeProvider 续轮、版本/匹配失败、BOM/混合换行、预算、权限、取消锁与零写入策略。test_sandbox 覆盖大 stdin、不进入 argv/repr、输入阻塞时取消、既有输出与关闭；显式 Docker 集成覆盖超过 64 KiB 的内容、原子创建/覆盖、链接、查询、丢失结果不重复提交且清理自有临时物。示例 43 和教程 40 导入正式包，版本映射写入 upstream-mapping。
