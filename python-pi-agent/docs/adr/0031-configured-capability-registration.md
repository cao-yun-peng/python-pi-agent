# ADR 0031: 通用的配置驱动 capability 注册

> 状态：已接受（Accepted）
> 关联：路线图 B4，ADR 0012、0019、0027、0030

## 背景

B3 已开放 Provider 配置解析。工具侧仍由 Builder 读取 bash 节并直接创建 DockerProcessSandbox、Executor 和 BashTool，下游配置驱动能力需要修改 Builder。

## 决定

- 新增 CapabilitySettings、CapabilityConfigContext、ConfiguredCapability、CapabilityRegistration 和不可变 CapabilityCatalog。
- YAML 使用 capabilities mapping，以注册名为键。同步 parser 接收原始字段和配置文件上下文，返回带 name 的类型化 Settings。factory 接收 Settings 和既有 PluginAPI，可同步或异步初始化能力。
- Builder 按配置顺序将初始化函数交给 PluginHost.load，再加载显式代码插件。工具、NextTurnHook 和 Session 插件进入现有组件组装流程，资源通过 register_cleanup 声明。
- factory 在取得资源后、下一次 await 前注册 cleanup。PluginHost 负责失败、取消和正常关闭时的清理与注销；Builder 继续管理 Provider、Store 和 observer 的整体回滚。
- Bash 的 `DockerBashSettings`、parser 和全部字段校验由 `application/capabilities/bash.py` 中的内置注册提供，通用配置模块不再识别任何 Bash 字段。Builder 直接消费 `AppConfigSpec.capabilities`。
- 顶层 `bash` 不再被支持（修订）。最初它作为兼容字段归一化为 ConfiguredCapability；发布 0.9.0 前改为只要根节点出现 `bash` 键就以 `ConfigurationError` 拒绝，包括 `bash: null` 和空值，并提示改用 `capabilities.bash`。按键而非按值判断，避免旧配置因取值为空被静默忽略而“看似成功却没有 Bash”。
- 默认目录仅注册 Bash。自定义目录必须显式交给配置加载器和 Builder。新增能力使用下游包中的注册对象，权限与执行仍经过已有策略端口。

## 备选方案

1. 为每个工具继续增加 Builder 分支。扩展会持续触及核心组装代码。
2. 新建另一套能力产物和资源管理器。现有 PluginAPI 和 PluginHost 已覆盖注册、事务和生命周期语义。
3. 在 YAML 中指定 Python 模块并自动导入。显式 Catalog 能让应用审查可使用的能力定义。

## 后果

配置解析产生部署值，初始化函数创建活资源，PluginHost 提供注册与生命周期。capability 当前可贡献的能力由 PluginAPI 定义，后续端口扩展仍需独立契约。

## 验证方式

先用 src 外的测试适配器验证配置解析、工具执行和事件，再验证 Hook/Session 插件、失败、取消、重名与逆序清理。`capabilities.bash` 需保持相对路径、审批、副作用和 fail-closed 语义；顶层 `bash` 的映射、`null` 与空值均需明确报错。教程和离线示例导入正式包。

证据见 tests/test_capability_catalog.py、tests/downstream_capability/echo.py、examples/33_configured_capability.py 和第 32 章教程。B3 的外部 Provider 与 B4 的外部 capability 在同一 YAML/Runtime 测试中组合运行。
