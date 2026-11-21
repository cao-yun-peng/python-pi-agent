# ADR 0044：模型请求持久化与离线还原

状态：已接受，2026-11-20。

## 背景

Session 历史持久保存消息与摘要，但 ContextStrategy、Middleware 和 Session 插件可以生成仅供一次请求使用的输入。检索源变化后，仅恢复历史无法确定当时的全部模型输入。观察 EventSink 允许隔离错误和丢弃，不能承担发送前的必需存储。

## 决定

- 用 domain 中的 RecordedModelRequest 保存标准 ModelProvider.stream 参数，包括完整消息、工具定义、请求身份及显式 ModelRequestTarget。参数不从 Provider 对象、环境变量或 HTTP client 中反射提取；组装方负责与实际适配器公共设置保持一致。
- ModelRequestStore 定义必需提交和离线读取，RecordingModelProvider 借用 Store 与 Provider，在每次 stream 前先提交独立副本。失败或取消继续抛出，不调用下游。记录输入不运行原 Hook 或工具，也不声称重现模型生成。
- FileModelRequestStore 的记录 v1 声明 message codec v6，复用同存储层的消息编解码；不修改现有 Session schema。每个请求/尝试使用摘要文件名，临时文件写完并 fsync 后用硬链接排他发布。读取忽略未发布临时文件，对完整记录的未知版本、字段错误、身份不匹配与超限拒绝。
- 重复 request_id/attempt_id 拒绝，避免同身份表示不同输入。取消线程 I/O 时先等待线程退出，再传播取消，允许已经提交的 prepared 记录存在而没有发送。
- 首版显式接入，既可作为 Builder 的 Provider，也可单独用于 Session 或压缩 Provider。包装组合 Provider 得到逻辑请求；包装各原始候选可记录重试/回退的不同 attempt 与目标。Provider 和 Store 的关闭责任仍在创建方。

## 备选方案

扩展 DurableSessionEvent 可以让请求和对话共享序号，但会修改 SessionStore 协议、物理版本与所有后端，并要求独立 Provider 调用额外获得 Session writer。观察日志不能在失败时阻止发送，重跑检索插件不能保证取回相同数据。保存 HTTP wire 数据会绑定各供应商 codec 和凭据处理，不适合作为标准端口的第一版实现。

## 后果

请求快照与 Session 日志为独立持久事实，通过 Session、run、turn、request、attempt 身份关联；二者没有跨文件原子事务。快照证明准备完成，不能证明实际发送、远端接收或生成成功。保存输入是显式启用的能力，已有未包装 Provider 的行为不变，旧历史无法补回过去缺失的检索内容。

记录包含完整输入、thinking、结构化工具输出和 opaque state，应用负责目录权限、保留与删除；普通 repr 隐藏正文，不自动复制凭据或 URL。文件系统必须支持硬链接；不支持时报告存储错误。已 fsync 的文件内容在发布前完整；首版不承诺父目录条目在掉电后仍存活，不自动清理进程崩溃遗留的 pending 文件，也不捕获供应商隐式默认值。每条记录读写有显式字节预算，离线扫描成本仍与记录数成正比。

## 验证

test_model_request_recording 使用 FakeProvider 验证请求存储早于调用，RAG 与工具续轮输入等于 Provider 接收值且不进入长期历史。消息 codec 保留 thinking、provider state、结构化结果及完整 ToolDefinition。失败、取消、重复身份、并发发布、未知版本、损坏与预算路径均有离线测试。示例 44 由测试启动两个独立进程，记录进程退出且检索文件删除后，恢复进程仅读取快照与历史。新增模块纳入 strict mypy；稳定 API 和依赖方向使用原有测试验证。
