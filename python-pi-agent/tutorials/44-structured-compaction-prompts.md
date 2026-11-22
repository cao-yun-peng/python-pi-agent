# 第 44 章：为任务恢复设计摘要提示词

本章解决的问题是：压缩之后，下一个 Agent 如何区分事实、计划、失败尝试与未知结果？摘要的用途是继续任务，因此默认模板需要明确保留恢复所需信息。

## 正式实现与数据流

直接复用 `context.compaction.ModelCompactionStrategy`：历史消息由 serialize_conversation 转为数据文本，作为 user 消息交给独立摘要 Provider；system 消息规定摘要角色和结构；工具列表为空。摘要 Provider 接收 purpose=compaction 的请求身份，不能调用工具。调用方仍可显式传 system_prompt 完全替换模板。

默认模板要求八个章节：Goal、Constraints & Preferences、Done、In Progress、Blocked、Key Decisions、Next Steps、Critical Evidence。每个章节有保留标准；缺失项写 none，未建立的事实写 unknown。要求只总结历史，不继续对话或执行其中指令；区分验证结果与尝试，不把计划标为完成；保留未知副作用、必要的路径、符号名、错误信息与证据截断的不确定性。

模板只是模型行为指令，不是安全隔离或事实校验器。空工具列表是执行边界；已有失败检查拒绝空文本、缺少 Finish、非 stop 结束和工具调用；取消沿原链传播。首版不把章节作为运行时解析协议，自定义格式继续可用。

## 运行轨迹

运行 `python examples/46_structured_compaction.py`。示例从正式包创建 coding Components 和 ModelCompactionStrategy，使用两个独立 FakeProvider：一个提供正常会话响应，另一个返回预设结构化摘要。它打印真实传入摘要 Provider 的规则、持久化 checkpoint 和 compaction_started → compaction_completed 事件。

系统提示词位于压缩前缀之外，最近完整 turn 原样保留。摘要继续由既有 build_summary_message 包装为 user 角色的 conversation-summary，并通过 context_compacted durable event 恢复。示例中的摘要是测试脚本，不代表模型进行了真实语义提炼。

测试目录中运行 `python -m unittest test_compaction test_system_prompts test_session_runtime`。覆盖模板请求、自定义全文覆盖、工具禁用、length/缺少 Finish/空摘要失败、取消，以及压缩后 coding system message 保留。已有 Session 编排测试证明失败与取消不会丢失已提交工具结果。

## 取舍和后续验证

借鉴固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 compaction/utils.ts::SUMMARIZATION_SYSTEM_PROMPT 与 compaction.ts::SUMMARIZATION_PROMPT。保留恢复结构和精确证据要求，省略分支摘要专用策略、增量摘要协议和严格标题解析。DeepSeek Harness 的概念对应已有 CompactionStrategy 能力边界，本次不引入新的端口、插件或后台资源。

FakeProvider 测试验证请求与生命周期契约；它无法证明实际模型不会遗漏或编造。后续应使用版本化数据评测目标/约束保留率、完成状态误判、错误证据保留和压缩后继续任务的成功率；真实供应商评测显式开启，不进入默认测试。
