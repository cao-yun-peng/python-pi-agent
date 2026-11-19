# 第 32 章：配置驱动的 capability 注册

应用可能从 YAML 启用工具、Hook 或 Session 插件。每种能力都需要参数校验、初始化、资源清理和运行证据。本章通过 CapabilityCatalog 选择配置定义，再交给已有 PluginHost 执行事务注册。

安装项目后，从 `python-pi-agent` 运行 `python examples/33_configured_capability.py`。示例使用 FakeProvider，输出资源打开、插件注册、工具结果和关闭轨迹。第二次构造故意失败，已注册的 cleanup 会在回滚中执行。

## 从配置到能力

YAML 的 capabilities 是按注册名索引的 mapping。示例使用以下配置。

```yaml
version: 1
provider:
  name: deepseek
capabilities:
  label:
    prefix: 'Configured '
```

应用实现同步 parser，签名为 `(raw, context) -> CapabilitySettings`。raw 是经过复制且顶层只读的 mapping，context.config_path 是配置文件绝对路径。相对资源路径应在此阶段按配置文件目录解析。parser 校验字段与默认值，返回应用自己的冻结 dataclass，其中 name 与注册名一致。

CapabilityRegistration 把 parser 与 factory 配对。factory 签名为 `(settings, api) -> None | Awaitable[None]`，api 是已有 PluginAPI。示例的 factory 创建 LabelResource 后立即注册 cleanup，再注册 FunctionTool。相同接口也可以注册 NextTurnHook 和 SessionPluginFactory。

扩展默认目录使用 `default_capability_catalog().with_registration(registration)`。应用把目录同时传给 `load_app_config(..., capability_catalog=catalog)` 和 `AgentRuntimeBuilder.from_loaded_config(..., capability_catalog=catalog)`。目录返回新实例，重名注册会失败。解析期间只生成 Settings，初始化发生在 build 事务中。

## 为什么复用 PluginHost

Builder 先检查配置中的能力是否在目录里，然后创建 Provider 与默认组件。配置能力按 YAML 顺序交给 PluginHost.load，随后加载代码插件。Host 的 snapshot 提供工具、顺序 Hook 与 Session 插件，Builder 把它们交给正式运行时。

每个 PluginAPI 注册操作都返回 Registration 句柄。资源应在取得后、下一次 await 前通过 register_cleanup 声明清理。初始化失败或取消时，Host 清理当前尚未发布的注册；后续步骤失败时，Builder 关闭 Host 并逆序释放已经加载的能力和 Provider。正常关闭先停止 Session，再释放能力资源，最后关闭 Provider 和观察者。

异步 factory 可以等待初始化，并会接收取消信号。factory 自行负责 cleanup 注册之前的部分初始化资源。能力工厂中的 ConfigurationError 应使用安全消息，原生异常会保留在异常链中，调用方收到 RuntimeBuildError。

工具定义继续声明输入输出、副作用、审批和超时，调用时进入已有 ApprovalPolicy 与 ToolExecutionGate。能力配置只选择可信应用代码中的实现，具体权限策略由 Profile 和显式组装决定。

## Bash 如何迁移

内置 Bash 和下游能力走同一个目录：`application/capabilities/bash.py` 同时拥有 `DockerBashSettings`、全部字段校验、parser 与初始化函数，通用配置模块不再识别任何 Bash 字段。配置只能写在 `capabilities.bash`。早期的顶层 `bash:` 已移除，配置根出现该键即报错，包括 `bash: null`。这里按键判断而不是按值判断：如果把 `null` 当作“未配置”，旧配置会解析成功却没有 Bash，问题要到运行时才暴露。

Docker workspace 仍以配置文件目录为基准。BashTool 保持 requires_approval，网络开启时声明 external，执行继续使用 SandboxedBashExecutor 和 DockerProcessSandbox。Builder 只消费 `AppConfigSpec.capabilities` 这一条配置序列，配置诊断也从它列出启用的能力。

## 验证与练习

运行 `python -m unittest discover -s tests -p test_capability_catalog.py -v`。src 外的 downstream_capability 包只导入稳定 API，其工具、Hook 和 Session 插件可以通过配置进入 Runtime。测试还把 B3 的外部 Provider 解析器与 B4 的外部能力放在同一个配置里运行。

把示例中的 prefix 改成其他字符串，观察部署参数如何影响工具结果。再在 factory 里增加一个异步初始化等待，在测试中取消构建，检查 cleanup 次数和顺序。可用 plugin_loaded、tool_execution_finished 和 run_settled 串起初始化与调用轨迹。

上游概念对照固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 ExtensionAPI.registerTool，本项目的映射见[上游文档](../docs/upstream-mapping.md)。当前能力贡献范围由 PluginAPI 定义，动态发现、任意端口注入与热卸载需要后续契约。
