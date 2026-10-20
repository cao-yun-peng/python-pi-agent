# 第 11 章：Token Threshold 自动触发压缩

上一章已经能手动调用 `await session.compact()`。这一章增加的不是另一套摘要实现，而是一个新的决策能力：在 `AgentSession.run()` 进入模型循环前，以及工具结果提交后的下一次模型请求前，判断上下文是否达到 token 阈值，并决定是否复用 durable compaction。

## 1. 为什么 Policy 与 Strategy 必须分开

```text
CompactionPolicy   = 何时压缩
CompactionStrategy = 怎样生成摘要
SessionStore       = 怎样原子持久化
replay_session     = 怎样重建状态
```

如果把阈值写进 `ModelCompactionStrategy`，策略就同时知道 token 预算、会话历史和模型调用；以后想改成“阶段结束时压缩”或“成本超过预算时压缩”就必须重写摘要器。

`CompactionPolicy` 因此只接收消息并返回 `CompactionDecision`：

```python
CompactionDecision(
    should_compact=True,
    estimated_tokens=8120,
    token_threshold=8000,
    keep_recent_turns=1,
)
```

这个值对象既是控制结果，也是 trace evidence。runtime 不需要猜测策略为什么触发。

## 2. 阈值计算包含即将到来的 prompt

`AgentSession.run(prompt)` 加载 snapshot、补上首次 system message 后，用以下候选上下文评估策略：

```text
candidate = persisted messages + incoming user prompt
```

如果只估算 persisted messages，一个很长的新问题可能在检查后才让请求越界。当前 prompt 只参与估算；压缩真正替换的仍是已经持久化的旧完整 turn。压缩成功后，Agent loop 再按原有规则提交用户消息。

## 3. TokenThresholdCompactionPolicy

`TokenThresholdCompactionPolicy` 复用上一阶段的 `TokenEstimator` 端口。默认仍使用确定性的 `CharacterTokenEstimator`，也可以注入供应商 tokenizer：

```python
policy = TokenThresholdCompactionPolicy(
    token_threshold=80_000,
    keep_recent_turns=2,
    estimator=my_model_tokenizer,
)
```

当 `estimated_tokens >= token_threshold` 时触发。非法阈值、非法估算结果或估算器异常都会产生稳定的 `CompactionPolicyError`，不会被误报成 Provider 传输失败。

当前估算只覆盖 `Message`，不包含 tool definitions、供应商包装和输出 token reserve。因此真实 profile 应让阈值低于模型最大上下文，并为工具 schema、摘要调用和模型输出保留余量。

## 4. `prepare_next_turn` Hook

Hook 是主流程主动调用的扩展接缝。它和观察事件不同：`EventSink` 只能看到已经发生的事实，不返回控制结果；`NextTurnHook.prepare()` 会被 Agent `await`，并返回下一次模型请求使用的 canonical history，所以它可以暂停、变换或拒绝后续执行。

当前安全时序是：

```text
assistant tool call 已提交
  -> 所有 tool result 已提交
  -> NextTurnHook.prepare(messages)
  -> AgentSession 重新评估 CompactionPolicy
  -> 必要时生成摘要并追加 context_compacted
  -> Hook 返回 replay 后的 SessionSnapshot.messages
  -> ContextStrategy 准备请求投影
  -> 下一次 ModelProvider 请求
```

底层 `Agent` 只依赖 `NextTurnHook`，不知道 token 阈值、摘要模型或 SessionStore。`AgentSession` 通过内部适配器 `_SessionNextTurnHook` 注入现有 `_maybe_auto_compact()`，并在调用前验证 Agent history 与 Store snapshot 一致。压缩成功后，返回值不是临时拼出的消息，而是 Store 应用 durable event 并 replay 得到的 snapshot。

当前只有一个有序 hook，因此没有多 hook 顺序问题。取消直接向上传播；预期的 `AgentError` 保留原类型；其他实现错误统一包装成 `ContextPreparationError`。Hook 返回值必须全部是 `Message`。

## 5. 四条运行路径

每次配置了 policy 的 session run 都会产生 `compaction_evaluated`：

```text
未达到阈值
  -> 不调用摘要策略
  -> 正常进入 Agent.run()

达到阈值且存在可压缩旧 turn
  -> compaction_started
  -> 生成摘要并追加 context_compacted
  -> compaction_completed
  -> 用 replay 后的历史进入 Agent.run()

达到阈值但没有可压缩旧 turn
  -> compaction_skipped(reason=no_compactable_history)
  -> 正常进入 Agent.run()

工具结果使同一 run 越过阈值
  -> tool message 先持久化
  -> prepare_next_turn 重新评估
  -> context_compacted 持久化并 replay
  -> 第二次模型请求看到 summary + 保留的完整当前 turn
```

最后一种情况通常说明 system message、当前 prompt 或唯一近期 turn 本身太大。重复调用摘要器无法解决问题，所以 runtime 显式跳过，让后续 ContextStrategy 或 Provider 给出真实的预算错误。

## 6. 配置约束与失败语义

自动 policy 必须与 `compaction_strategy` 一起配置；只有 strategy 时仍可手动压缩。

摘要失败、取消、版本冲突的语义与手动压缩相同：不会追加 `context_compacted`。如果 run 前压缩失败，本轮 user prompt 尚未提交；如果工具循环内压缩失败，已经完成的 user、assistant tool call 和 tool result 保持 durable，但不会发起下一次模型请求。这样不会假装工具没有执行，也不会留下半个压缩结果。

## 7. 运行示例

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
.\.venv\Scripts\python.exe .\python-pi-agent\examples\11_automatic_compaction.py
```

示例用消息数量作为教学估算器，让 run 前的第一次决策为 `False`、工具结果后的第二次决策为 `True`。输出会显示两个 decision payload、第二次 Provider context，以及完整 capability/live trace。

## 8. 与 OpenAI 和 Pi 的映射

OpenAI Responses API 的 server-side compaction 同样在 rendered token count 跨过 `compact_threshold` 后触发，并把 compaction item 带入后续窗口。它返回的是面向模型延续状态的 opaque item，而本项目为了教学、审计和跨 Provider 复用保存可读 summary message。参考：[OpenAI Compaction](https://developers.openai.com/api/docs/guides/compaction)。

固定 Pi commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 在底层 `runLoop()` 暴露 `prepareNextTurn`，由上层 `AgentSession` 注入阈值检查和 compaction。本项目采用同样的“触发时机深入循环、压缩职责留在 session”结构，但仍使用更保守的完整 turn 切点。

## 9. 当前主动限制

- 只在 run 前和工具续轮前检查；最终回答越过阈值后，要等下一次 run 才压缩。
- 没有 hysteresis 或最小压缩收益判断。
- 没有自动重试摘要，也不会在版本冲突后静默重算。
- 没有 Pi 的 overflow compact-and-retry、steering/follow-up queue 和 split-turn summary。
- 默认字符估算不是供应商精确 token 数。

下一步不应立即增加更多触发条件，而应先用 `docs/compaction-evaluation.md` 中的评测验证摘要真的保留了任务状态。
