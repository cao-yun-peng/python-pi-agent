# 第 43 章：静态系统提示词与工具规则

本章解决的问题是：如何让 coding 助手的操作规则与最终注册的工具一致，同时保留通用 Harness 的无业务人设默认值？

## 从配置到模型请求

`SystemPromptSpec` 是 domain 中的不可变值，包含 preamble、guidelines 和 include_tool_guidelines。`coding_profile()` 引用应用层的短默认说明；`minimal_profile()` 不提供角色或提示词。工具在 `ToolDefinition.prompt_guidelines` 声明操作规则，完整参数说明仍放在 description/schema。旧工具默认贡献空元组。

组装顺序是 Profile → Builder 覆盖 → 插件工具合并 → SessionSpec 选择 → 首次运行渲染。不能提前在 Profile 内拼接，否则会遗漏插件工具或混入其他 Session 的规则。独立 Agent 使用已冻结的工具定义渲染；Session 首次运行使用选定 Components 的工具定义渲染。纯函数 `runtime.prompts.render_system_prompt` 保留 preamble，按 Profile 规则、工具顺序收集规则，去除空项并稳定去重。它不读取文件或执行 I/O。

## 覆盖和持久化契约

`AgentProfile(prompt_spec=SystemPromptSpec(...))` 可配置自定义结构化说明。`with_system_prompt("exact")` 清除结构化配置并完整使用原字符串；`with_system_prompt(None)` 清除默认角色和规则。直接同时提供字符串与 spec 会抛出 ConfigurationError。Components.replace 是显式的字段替换：更换模式时同时清除另一字段。

Session 在空历史时将最终文本提交为普通 durable system message，后续 run 不重复插入。恢复使用已有历史；包括没有 system message 的旧历史，也不自动补入新默认值。配置更新不会迁移旧会话。独立 Agent 的 initial_messages 与任何新提示词配置互斥，避免不明确的覆盖。

## 运行和观察

运行 `python examples/45_static_system_prompt.py`。示例从正式包导入 Builder、Profile、ReadTool 和存储，用 FakeProvider 完成一次无费用请求，打印模型收到的提示词及持久化角色：system → user → assistant。read 的规则会出现，未注册 bash 的规则不会出现。

运行测试目录内的 `python -m unittest test_system_prompts test_app_runtime test_session_spec test_model_request_recording`。测试覆盖插件工具、Session 隔离、完整替换、显式禁用、恢复复用、配置错误和取消。取消不会撤销已存在的系统消息。操作权限仍由 ApprovalPolicy 和 Gate 执行，规则不会增加权限。

请求快照升级为 v2，保存工具的 prompt_guidelines；读取器继续接受 v1，旧工具规则恢复为空。Session 消息格式没有变化。快照负责保留标准端口参数，不声称复现供应商生成。

## 取舍与边界

借鉴固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 buildRules/buildSystemPromptSections 和工具贡献，但不复制工具参数语义。Python edit 仍只做一次唯一文本替换。第一版省略工具概览副本、动态章节补丁、提示词热更新、项目文件发现、Skills 加载和模板引擎。DeepSeek Harness 的能力边界在此体现为 Definition（spec/规则）、Provider（工具贡献）、Consumer（Agent/Session）、Lifecycle（实例级不可变值）和 Evidence（离线测试/持久化文本）。

FakeProvider 能证明请求构造和恢复契约，不能证明真实模型遵循规则。后续应独立评测分页、版本冲突和未知结果恢复。
