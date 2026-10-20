# ADR 0006：保存并回传 Provider 可见的 Thinking 状态

## 状态

Accepted

## 背景

DeepSeek 的 thinking 模式与工具调用组合使用时，后续模型请求必须把上一条 assistant 消息的 `reasoning_content` 原样回传。此前运行时只把 `ThinkingDelta` 当作实时事件发布，assistant 消息完成后没有保存完整 thinking 文本，因此工具结果进入下一轮后，DeepSeek 无法恢复对应的推理上下文。

这不仅是供应商请求格式问题。只要某段状态会影响后续模型行为，它就属于可重建会话状态，不能只存在于瞬时回调中。固定版本的 Pi 也用独立的 thinking 内容与供应商签名承载类似状态，但本项目当前只需要支持 DeepSeek 的文本 reasoning 回传。

## 决定

1. 在领域层的 assistant `Message` 上增加 provider-neutral 的 `thinking: str` 字段；非 assistant 消息不得携带非空 thinking。
2. 运行时继续实时发布 `ThinkingDelta`，并在收到 `Finish`、确认 assistant 消息完成时，把本轮 thinking 增量聚合到 `Message.thinking`。
3. durable session event 和 JSONL session store 保存该字段，使恢复后的会话可以重建模型可见状态。
4. JSONL 写入 schema v2；读取端同时接受 v1 和 v2，v1 消息的 thinking 默认为空字符串。
5. DeepSeek adapter 在启用 thinking 且请求包含工具定义时，把历史 assistant 消息的 `Message.thinking` 映射为 `reasoning_content`。即使该 assistant 消息没有 thinking，也显式发送空字符串，以符合接口约束。
6. 当前不在通用领域对象中保存 provider-specific thinking signature，也不定义跨供应商迁移或转换规则；需要这些能力时另行决策。

## 备选方案

### 只在 DeepSeek provider 内维护临时 reasoning 缓存

拒绝。该状态会影响后续模型请求，却无法从 durable log 恢复；进程重启、会话重放和 provider 重建都会丢失它，还会让 provider 适配器承担会话职责。

### 把 thinking 拼进 assistant 的普通文本

拒绝。thinking 与面向用户的最终文本语义不同，混合后会改变上下文、UI 展示和持久化行为，也无法准确映射回 `reasoning_content`。

### 立即引入通用多段内容和 provider signature 模型

暂缓。该方案更接近 Pi 的生产级表示，但会显著扩大当前第二章的领域模型和序列化范围。先用独立文本字段形成最小、可测试的纵向切片。

## 后果

- DeepSeek thinking/tool-call 多轮请求可以确定性回传 reasoning，并在会话恢复后继续工作。
- thinking 仍可作为 live event 观察，同时拥有明确的 durable 表示；观察事件不再承担隐式事实来源职责。
- JSONL schema 升级到 v2，但旧 v1 日志仍可读取。
- thinking 可能包含敏感推理或输入派生信息。session log 的访问控制、保留和脱敏策略必须与普通消息内容同等严格。
- 当前表示只覆盖文本 reasoning；provider-specific signature、加密 thinking、跨模型兼容规则仍是后续工作。

## 验证方式

- 使用两轮 `httpx.MockTransport`：首轮流式返回 thinking 和 tool call，第二轮断言请求中的 assistant 消息包含完全一致的 `reasoning_content`，随后返回最终文本。
- 断言运行结果保留两轮 assistant thinking。
- JSONL v2 round-trip 测试验证 thinking 写入和恢复。
- JSONL v1 fixture 测试验证旧日志仍可读取，并把 thinking 默认为空字符串。
