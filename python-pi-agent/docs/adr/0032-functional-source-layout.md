# ADR 0032 按功能归组存储、上下文和可观测性

## 状态

已接受，2026-11-19。

## 背景

根目录同时存在存储、上下文策略、事件、遥测和应用装配模块。Session 存储与 runtime Session 同名，writer 和 Session 事件工具位于 runtime 内，阅读功能需要跨多个位置。用户要求第一批迁移三个功能目录并彻底删除旧模块。教程和下游示范代码在后续任务调整。

## 决定

- `storage/` 保存内存、JSONL、纯状态转换和 writer。
- `context/` 保存请求投影和摘要机制。
- `observability/` 保存事件、订阅、遥测、用量及 Session 事件工具。
- 子包初始化文件仅提供目录说明。具体能力由定义模块显式导入，OTel 适配单独加载。
- 包内引用和测试迁移到新路径，原具体模块被删除。稳定 API 和根包公开导出指向迁移后的实现。
- 应用级观察接线由 Runtime 组装，压缩执行时机由 runtime 编排。

## 备选方案

保持根目录平铺可以减少导入修改，但功能定位仍需跨目录检索。旧模块转导出可以保留具体路径兼容，但用户明确要求删除旧模块。按功能迁移符合此次要求。

## 后果

具体模块路径变更记录在 CHANGELOG，类的 Python 定义模块路径随迁移变化。教程、示例和下游示范包保留现有内容，后续调整。durable JSONL 契约沿用已有编解码和回放机制。

## 验证方式

对比迁移前后除导入外的语法树。验证稳定 API 对象身份和原模块移除。执行聚焦回归、包级离线测试、Ruff 和 mypy，并从干净源构建 wheel 验证安装后的模块。

## 第二批：工具与执行边界

2026-11-19 继续沿用按功能归组、删除原具体模块的决定：

- `tools.py` 迁入 `tools/core.py`，保存函数工具、参数校验、审批与声明式 Gate。
- `shell.py` 迁入 `tools/shell.py`，保存 Bash Consumer 和 SandboxedBashExecutor。
- `sandbox.py` 迁入 `tools/sandbox.py`，保存 DockerProcessSandbox 与输出捕获、清理机制。
- `tools/__init__.py` 仅说明目录职责，不转导出原 `tools.py` 的对象。
- `configured_bash.py` 继续属于应用组装边界。工具实现依赖 domain/ports，组装边界依赖工具实现，运行时保留执行编排。

此次调整定义位置与导入，并修正迁移模块的类型描述：进程协议使用只读属性，读取方法声明按位置传参；JSON 校验避免在不同类型的循环中复用变量名。稳定 API 和根包公开对象直接引用新定义；既有审批拒绝、超时、取消与 fail-closed 测试继续验证执行契约。教程、示例和下游示范代码仍在后续任务调整。

## 第三批：请求组合、run 句柄与工具职责

2026-11-19 继续在当前分支迁移：

- `model_runtime.py` 迁入 `providers/runtime.py`。ProviderCandidate、重试和路由策略、direct/resilient 请求组合及 attempt 观察保留原有语义。
- `run_handle.py` 迁入 `runtime/run_handle.py`。事件订阅类型继续由 observability 提供；句柄只组合 run 身份、结果等待和定向取消。
- 删除 `tools/core.py`，将 FunctionTool/ToolHandler、审批策略、执行 Gate、JSON 参数校验分别放入 `tools/function.py`、`tools/approval.py`、`tools/gates.py`、`tools/validation.py`。
- SandboxedShellTool 归入已有的 `tools/shell.py`，与 BashTool 共享 Shell 功能位置，保持各自消费的端口和结果语义。
- 包内消费、TYPE_CHECKING 引用、稳定 API 和根包导出都直接指向新定义；原路径不留兼容模块。教程、示例和下游代码仍由后续任务更新。

验证包括拆分前后每个定义的语法树对比、公开对象身份和原路径移除，以及模型重试/回退/取消、run 等待和定向取消、工具失败与取消的离线回归。工具子包保持仅含说明的初始化文件。

## 第四批：应用组装与共同消费的规格

2026-11-19 按用户确认的边界归组：

- assembly.py、app_runtime.py、lifecycle.py 分别迁入 `application/builder.py`、`application/runtime.py`、`application/lifecycle.py`。
- configuration.py、profiles.py 和两个 Catalog 迁入 application 下的同名模块；应用级观察接线迁入 `application/_observation.py`。
- configured_bash.py 迁入 `application/capabilities/bash.py`，默认 Catalog 继续通过显式工厂获取注册。两级新包的初始化文件仅提供说明，不注册插件或转导出实现。
- components.py、session_spec.py 迁入 `runtime/components.py`、`runtime/session_spec.py`，让运行时与应用层共同消费。
- 稳定 API 和根包直接引用新的定义位置。包内相对导入、类型检查导入和延迟导入同步迁移，11 个旧根模块全部删除。

本批只调整定义位置与导入。旧构造参数适配、内部接线参数、follow_up_id 和顶层 bash 配置仍按既有接口契约运行，由后续变更独立移除。教程、示例和下游示范代码继续留待后续调整。

通过语法树对比确认全部 17 个受影响源码模块的执行逻辑不变；验证新定义与公开导出的对象身份、旧模块移除，以及配置驱动能力、SessionSpec、资源关闭和构造回滚等离线行为，并从干净源构建 wheel 验证模块打包。

现有 CI 范围的 Ruff 和 mypy 检查通过。额外对整个 application 和两个 runtime 规格模块运行 strict 检查时有 11 项错误；在同一环境对迁移前 HEAD 的独立源码副本检查，也得到相同的 11 项错误，涉及动态 cleanup 类型、配置局部变量和泛型推断，以及缺少 PyYAML 类型桩。本批不扩大为类型标注修正任务。

## 后续：移除兼容接口

上文第四批留下的兼容接口已在后续独立变更中移除，不属于本 ADR 的目录调整：

- 旧构造参数、`_resolve_components`、`Agent` 公开接线参数和 `RuntimeSession.follow_up_id()`，见 ADR 0028 第 5 节；
- 顶层 `bash` 配置，以及从通用配置模块迁入 `application/capabilities/bash.py` 的 Bash Settings、parser 和校验函数，见 ADR 0031。

仓内测试、示例与教程同步改为 `AgentComponents` 和 `capabilities.bash`。
