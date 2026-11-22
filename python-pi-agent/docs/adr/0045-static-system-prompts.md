# ADR 0045：静态提示词组装与工具规则贡献

状态：已接受，2026-11-22。

## 背景

现有 Profile/Components 仅携带完整字符串，coding 默认没有提示词。工具可在 Builder 插件合并和 SessionSpec 选择时改变，因此在 Profile 中直接拼接会过早确定操作规则。现有会话必须继续以 durable 历史为事实来源。

## 决定

- domain 增加不可变 SystemPromptSpec，ToolDefinition 追加默认空 prompt_guidelines，不改变既有位置参数。
- coding 的文案由 application/prompts.py 显式提供；minimal 和 read-only 默认保持无角色。
- runtime 使用共享纯函数，在最终工具选择之后渲染；独立 Agent 使用其定义快照。顺序为基础说明、Profile 规则、工具规则，稳定去重。
- 完整字符串和 spec 互斥。Builder 的显式字符串/None 同时清除 spec，保留原有完全覆盖和禁用行为。
- Session 首次运行持久化最终文本，恢复不补写、不更新旧提示词。动态工具集迁移不属于本契约。
- 请求快照写 v2 保存规则，读 v1/v2；v1 规则为空，Session message codec 不变。

## 备选方案

在主循环硬编码 coding 文案会反转依赖；在 Profile 提前渲染会遗漏插件和会话选择；运行时加载任意模板会引入当前不需要的 I/O 与生命周期；不保存新增字段会丢失请求快照的完整工具定义。

## 后果

新建 coding 会话多一条 system message。依赖消息位置的测试改为按角色定位。SessionSpec 对 spec 按值比较。已有自定义字符串及 minimal 默认不变；旧快照可读取，新 v2 快照不能由旧版读取器读取。

## 验证

test_system_prompts 覆盖确定渲染、插件/会话选择、覆盖/禁用、恢复、失败配置与取消；test_model_request_recording 覆盖 v2 完整还原、v1 兼容与非法字段。示例 45 打印模型输入及 durable 角色；API、架构、工具与会话聚焦回归继续验证既有契约。
