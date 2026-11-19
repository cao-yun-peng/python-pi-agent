# 第 31 章：从 YAML 注册一个外部 Provider

Provider 构造工厂解决了如何创建实例。通过 YAML 启动应用时，还需要把配置字段转成 Settings，再解析凭据，最后构造运行配置与 Provider。本章把这些步骤交给同一个显式 ProviderRegistration。

在安装项目后，从 `python-pi-agent` 运行 `python examples/32_registered_provider.py`。示例用临时 YAML 选择外部 `demo` Provider，以演示用凭据驱动 FakeProvider。输出包含脱敏诊断、完成运行、取消运行和 `provider closed`。

## 四个实现与一份注册

示例中的 DemoSettings 保存 model、prefix 和 api_key_env。parse_demo 检查 YAML 字段，并返回此类型。DemoSettings.build 收到已解析的 key 后创建 DemoConfig，后者使用 `field(repr=False)` 保存凭据。create_demo 先检查运行配置的具体类型，再创建模型端口实现。

正式包提供两个结构协议。ProviderSettings 描述 name、model、api_key_env 和 build，ProviderRuntimeConfig 描述运行配置的 model。适配器自己的 dataclass 可以增加 URL、部署名、请求参数等字段，工厂在具体类型上读取它们。解析和 build 应当只处理配置值，资源在 factory 中创建。解析器使用同步函数，异步初始化放在 factory 中。

示例通过 `ProviderRegistration("demo", create_demo, parse_settings=parse_demo)` 注册，再调用 `default_provider_catalog().with_registration(...)` 得到扩展 Catalog。with_registration 返回新的值，重名会失败，因此不同应用的注册集合可以独立配置。

## 配置如何到达运行时

load_app_config 接受 `provider_catalog=catalog`。加载器先检查 YAML 的根结构、版本、重复键和 Provider 字符串键，再按 provider.name 找到解析器。传给 parser 的 mapping 保留完整 Provider 节，顶层只读且内容经过复制。parser 应校验其字段与组合，并返回 Settings。

Catalog 随后核对返回的注册名称、model、环境变量名和 build 方法。缺少解析器、未知名称、内联 api_key、非法返回值或字段类型错误会在构造 Provider 前失败。回调抛出的原生异常转为 ConfigurationError 并保留 cause。适配器主动抛出的 ConfigurationError 应使用安全消息。

凭据优先级为显式覆盖、进程环境、指定 dotenv。load_app_config 调用 Settings.build 获得运行配置后，应用把 LoadedAppConfig 和 Catalog 交给 `AgentRuntimeBuilder.from_loaded_config(...)`。Builder 使用名称选择工厂，随后按既有模型组合层发布 request/attempt 事件。正常的加载与构造过程调用 parser 一次。

单独调用 parse_app_config 可以检查非秘密配置。内置解析器还通过构造配置值验证已有模型参数组合。外部 parser 自行验证非秘密字段，build 在凭据解析后创建配置值。所有适配器都应让 Settings 的 repr 只包含可公开字段。

## 生命周期与失败

解析器、Settings 和注册项属于配置定义。Provider 实例由 Builder 创建并拥有，在 Runtime 关闭时释放，后续组装失败也会触发回滚。异步 factory 可以执行初始化，取消信号会进入该 factory。实例返回前的资源仍由 factory 管理，适配器可用 try/finally 释放初始化中申请的资源。

原有的 `ProviderRegistration(name, factory)` 可继续配合手工提供的运行配置使用。让这份注册支持 YAML 时，需要补上 parse_settings。旧的 assembly 和根包 Catalog 导入路径继续指向相同定义，新的下游项目可以从 pi_agent_py.api 导入全部配置接入契约。

## 验证与练习

查看 `tests/downstream_provider/echo.py`，它位于正式包之外，仅依赖稳定 API。运行 `python -m unittest discover -s tests -p test_provider_catalog.py -v`，跟踪自定义字段、凭据来源、运行事件、取消和回滚。

在示例中增加一个供应商专用字段，并给非法取值写一个 ConfigurationError。观察这个字段如何从 YAML 到 Settings、Config、factory。再取消异步初始化，确认 factory 自身的清理与 Builder 已接管资源的清理顺序。

设计对照固定的 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb`。对应的 `ModelRegistry.registerProvider()` 和本项目的注册契约见[上游映射](../docs/upstream-mapping.md)。当前接缝覆盖单个凭据引用与显式 Catalog，OAuth、动态发现和多凭据选择留给后续设计。
