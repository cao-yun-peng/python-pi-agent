# ADR 0030: 可注册的 Provider 配置解析

> 状态：已接受（Accepted）
> 关联：路线图 B3，ADR 0024、0028

## 背景

ProviderCatalog 原先可以注册构造工厂，但 configuration.py 通过四家供应商的名称分支和封闭配置联合解析 YAML。下游注册 Provider 后，仍需修改核心配置代码才能通过 YAML 启动。

## 决定

- ProviderRegistration 增加可选的同步纯函数 `parse_settings(Mapping[str, object]) -> ProviderSettings`，注册时拒绝异步解析器。解析器负责供应商字段白名单、类型、默认值和非秘密配置校验。已有 factory-only 注册可继续构造手工提供的配置，YAML 接入需要提供解析器。
- ProviderSettings 与 ProviderRuntimeConfig 改为开放 Protocol。Settings 提供只读 name、api_key_env、model，以及创建配置值的 build(api_key)。运行配置提供只读 model，额外字段由具体 dataclass 表达。工厂通过具体配置类型校验后消费这些字段。
- parse_app_config 和 load_app_config 接受显式 provider_catalog。省略时创建内置 Catalog。所有 Provider 按名称调用注册的解析器，默认四家的参数校验也通过此入口执行。
- Catalog 及其注册项是不可变值。with_registration 返回新的 Catalog，重名注册会失败。注册定义与工厂移动至 provider_catalog.py，assembly 保留既有导入的对象身份。
- YAML 加载器负责根结构、版本、重复键和 Provider 字符串键校验。Catalog 统一拒绝内联 api_key，核对解析结果的注册名、环境变量名、model 和 build 方法。解析器接收复制后的只读顶层 mapping。
- 凭据沿用显式覆盖、进程环境、指定 dotenv 的优先级。加载器把凭据交给 Settings.build，得到配置后交给 Builder。LoadedAppConfig 的 repr 隐藏整个运行配置，诊断继续使用模型名和脱敏凭据信息。
- parser 和 Settings.build 负责纯配置工作。factory 才创建 Provider 资源，可以返回 awaitable。Builder 接管成功返回的 Provider，继续按 owned 资源关闭和回滚。异步 factory 在返回前取消时，由 factory 释放自身尚未移交的资源。

## 备选方案

1. 只为未知名称增加回调分支，同时维护内置分支。放弃此方案，统一注册入口更易验证内置和外部适配器的契约。
2. 将任意 dict 传到 Agent 或 Provider。放弃此方案，原始数据在配置边界转为具体 Settings 与运行配置值。
3. 基于模块路径自动导入插件。放弃此方案，应用通过显式 Catalog 选择允许的解析器与工厂。
4. 扩展成通用 capability 配置容器。B4 会单独定义工具能力的配置和生命周期，本次只开放 Provider 接缝。

## 后果

下游可以在独立包中实现 Settings、Config、parser 和 factory，仅通过稳定 API 完成 YAML 到 Runtime 的组装。新增供应商时，核心供应商名称列表和联合类型无需扩展。

Settings 与运行配置的共同字段是当前应用组装需要的最小协议。凭据仍采用单个 api_key_env 引用，多凭据或 OAuth 解析需要后续契约。适配器负责排除 Settings 中的秘密，并为其 ConfigurationError 提供安全的消息。原生回调异常保留 cause，面向调用方的消息使用稳定描述。

## 验证方式

- `tests/downstream_provider/echo.py` 是位于 src 之外的适配器，全部 Harness 导入来自 pi_agent_py.api。
- `tests/test_provider_catalog.py` 验证 YAML 参数、typed config、凭据优先级、诊断、parser 调用次数、注册隔离、非法输出、运行事件、异步 factory 取消和组装回滚。
- 四家内置 Provider 的配置回归继续覆盖既有默认值、能力组合和错误分类。
- `examples/32_registered_provider.py` 使用 FakeProvider 展示完成、取消和资源关闭轨迹。
