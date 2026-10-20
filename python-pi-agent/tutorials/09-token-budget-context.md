# 第 9 章：Token Budget 与完整消息组

上一章建立了 `ContextStrategy` 接缝。这一章第一次真正改变模型输入：当历史估算值超过预算时，只保留 system 消息和最近的完整 turn，但 durable history 仍保持不变。

本章仍不生成摘要，也不修改 session event log。

## 1. 为什么不能逐条从后往前拿消息

一次工具调用在历史中至少包含：

```text
user
  -> assistant(tool_call id=call-1)
  -> tool(tool_call_id=call-1)
  -> assistant(final answer)
```

如果只留下 tool result，模型找不到产生它的 tool call；如果留下 tool call 却删掉结果，模型可能以为工具仍未完成。因此本项目先定义更保守的原子单位：从一个 user message 开始，到下一个 user message 之前结束，整个 turn 不可拆分。

`group_context_messages()` 是纯函数，负责把历史分成：

- 每条 system message：独立且固定保留的 group。
- 每个 user turn：user、assistant tool call、tool result 和最终 assistant 回答组成一个 group。
- 异常历史中出现在首个 user 之前的 assistant/tool 消息：组成一个普通 group，不静默丢失。

## 2. TokenEstimator 是估算端口

```python
class TokenEstimator(Protocol):
    def estimate(self, messages: Sequence[Message]) -> int:
        ...
```

不同模型的 tokenizer、消息包装开销和多模态计价可能不同，因此预算策略不能硬编码供应商算法。当前默认 `CharacterTokenEstimator` 会计算正文、thinking、tool call 和工具关联字段的字符数，按 `characters_per_token=4` 换算，并增加每条消息的固定开销。`structured_content` 是程序侧数据，不会发给模型，因此不计入预算。

它是确定性的近似值，不是 DeepSeek 或其他供应商 tokenizer 的精确结果。将来可以显式注入精确实现，而不改 `TokenBudgetContextStrategy`。

当前预算只覆盖 `Message`。工具 schema、供应商请求包装以及为模型输出预留的 token 尚未计算，因此它不能被当作“整个请求绝不会超过 context window”的保证。

## 3. 预算选择算法

```text
1. 分组
2. 选中所有 system groups
3. 强制选中最新非 system turn
4. 若强制内容已超预算，失败
5. 从倒数第二个 turn 向前逐组尝试
6. 第一个放不下时停止
7. 返回 ContextPlan，不改原历史
```

“遇到第一个放不下就停止”保证保留下来的普通历史是连续的最近后缀。不能跳过一个很大的较新 turn，再偷偷塞入更老的小 turn，否则时间线会出现难以解释的空洞。

## 4. 为什么超预算要失败

system 指令和当前 turn 是最低限度的模型上下文。如果它们本身就超过预算，策略抛出 `ContextBudgetExceededError`，其中包含 `required_tokens` 和 `token_budget`。

可选方案还有截断当前用户输入、删除 system 指令或拆开工具链，但这些都会改变语义。当前阶段选择显式失败，让上层决定提高预算、拒绝输入，或者在下一阶段触发摘要压缩。

估算器抛出异常或返回负数、布尔值、非整数时，会转换为 `ContextPreparationError`。错误发生在 Provider 调用前，因此不会误记为模型传输失败。

## 5. 与上一步代码怎样连接

`Agent` 没有新增预算判断：

```text
Agent._prepare_context()
        |
        v
TokenBudgetContextStrategy.prepare()
        |
        +-- group_context_messages()
        +-- TokenEstimator.estimate()
        |
        v
ContextPlan(
    messages=选中的投影,
    token_budget=预算,
    estimated_tokens=估算值,
)
```

原有 `context_prepared` event 会自动带出预算、估算值和省略消息数。这个变化不需要修改 Agent loop，说明上一章建立的能力接缝确实可以复用。

## 6. 运行示例

在仓库根目录执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\09_token_budget_context.py
```

示例为了让数字直观，把 `characters_per_token` 设置为 1。Provider 会看到 system、较新的完整问答和当前问题；更老的问答只从本次请求投影中省略，最终 durable history 仍包含它。

## 7. 与 Pi 的映射和差异

固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 中，`packages/coding-agent/src/core/compaction/compaction.ts` 的 `estimateTokens()` 也采用 chars/4 启发式；`findCutPoint()` 从最新 entry 向前累计，并避开非法工具结果切点。

Pi 允许在一个 turn 内从 user 或 assistant 位置切开，并用 `turnStartIndex` / `isSplitTurn` 为后续摘要保存前缀。本项目尚未实现摘要，所以采用更保守规则：整个 user turn 都不可拆分。代价是可能更早报告预算不足，收益是当前模型投影永远不会出现半条工具链。

## 8. 掌握标准

完成本章后，应能独立解释和实现：

1. 为什么 TokenEstimator 是端口，而不是 ContextStrategy 内的固定函数。
2. 为什么 tool call 与 tool result 不能独立裁剪。
3. 为什么普通历史必须保留连续的最近后缀。
4. 为什么 mandatory context 超预算应该产生稳定错误。
5. 为什么预算策略不需要修改 Agent、SessionStore 或 durable event。
