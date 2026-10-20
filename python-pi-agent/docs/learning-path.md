# Learning Path

每一阶段都必须形成可运行的纵向切片，而不是先造一批空接口。

1. **消息、流和最小循环**：理解一次 Agent run 如何开始、提交和 settled。
2. **真实 Provider 适配**：DeepSeek SSE、认证、错误映射、usage、thinking replay 和生命周期。
3. **工具调用**：schema、调度、tool result、失败和权限。
4. **会话与事件日志**：追加式存储、回放、恢复和分叉。
5. **上下文策略**：资源加载、token budget、裁剪与压缩。
6. **Provider 工程化**：重试、退避、限流、多供应商和模型能力。
7. **插件与 Hook**：观察、拦截、生命周期和释放。
8. **安全执行**：审批策略、工作区边界与沙箱。
9. **可观测性与评测**：trace、成本、延迟、回归数据集和评分器。
10. **产品适配与多 Agent**：CLI、Web/SSE、垂直 profile、委派和预算。

## 当前进度

- 阶段 1、2、3 已完成：最小循环、DeepSeek Provider 和顺序工具调用已经形成可运行纵向切片。
- 阶段 4 已完成内存与 JSONL `SessionStore`、乐观并发、跨 run/进程恢复、逐消息提交、durable event log、replay、持久化 fork 和 replayable compaction event。分支元数据、多进程并发和数据库事务留给后续存储 Provider。
- 阶段 5 已完成 `ContextPlan`、异步 `ContextStrategy`、可替换 token 估算、完整 turn 分组、预算投影、手动 durable compaction，以及 run 前和工具续轮前的 token threshold 自动触发。
- 阶段 7 已提前完成一个最小基础：单个、有序、会被主循环 `await` 的 `NextTurnHook`。完整插件注册、多个 Hook 的排序、超时和卸载仍未开始。

## 当前学习节点：工具续轮前的 `NextTurnHook`

这个 Hook 服务于阶段 5 的自动压缩，但同时是进入阶段 7 前的第一份拦截型扩展样本。它刻意保持很小：Agent Loop 只提供安全时机，`AgentSession` 负责注入具体 compaction 行为。

```text
assistant tool call 已提交
  -> 所有 tool result 已提交
  -> NextTurnHook.prepare()
  -> CompactionPolicy 重新评估
  -> 可选的 durable compaction + replay
  -> ContextStrategy 生成请求投影
  -> 下一次模型请求
```

学习时按以下调用链阅读：

1. `ports.py::NextTurnHook`：先看输入、返回值和 awaited 拦截语义。
2. `runtime.py::Agent.run`：确认 Hook 只在工具结果全部提交后、且确实还有下一次请求额度时调用。
3. `runtime.py::Agent._prepare_next_turn`：观察取消传播、错误分类、返回值校验和内存历史替换。
4. `runtime.py::_SessionNextTurnHook`：理解通用 Hook 如何适配 session-specific compaction。
5. `runtime.py::AgentSession._maybe_auto_compact`：确认 run 前和工具续轮前复用同一 policy、strategy、store 与 replay 路径。
6. `tests/test_tool_runtime.py` 和 `tests/test_session_runtime.py`：用确定性证据核对正常、失败和取消路径。

这一节点的掌握标准：

- 能解释为什么 `EventSink` 是观察者，而 `NextTurnHook` 是可改变控制流的拦截器。
- 能说明 Hook 为什么必须位于完整 tool call/result 之后，而不能插在两者中间。
- 能画出 `Agent -> NextTurnHook -> AgentSession -> SessionStore -> replay -> Agent` 的状态回流。
- 能证明摘要失败或取消时，已提交工具结果仍然存在、没有 `context_compacted`，也不会发起第二次模型请求。
- 能解释为什么 Agent Loop 依赖 `NextTurnHook` 抽象，而不直接依赖 `CompactionPolicy` 或 `SessionStore`。

建议动手练习：

1. 给 `RecordingNextTurnHook` 记录 `next_request_number` 和收到的 message roles，手工预测测试结果后再运行。
2. 修改示例的消息计数阈值，让第一次评估为 `False`、工具结果后的评估为 `True`，观察 trace 顺序。
3. 让自定义 Hook 返回一个非 `Message` 值，确认 runtime 抛出稳定的 `ContextPreparationError`。
4. 在 compaction strategy 阻塞时取消 run，检查 durable log 中保留 user、assistant tool call 和 tool result，但没有 compaction event。

## 下一步

下一小步优先实现 compaction eval 的最小离线切片，用版本化 case 和 deterministic grader 验证摘要保留目标、约束、工具发现和下一步。随后再处理精确 tokenizer 与资源加载；不要在质量证据建立前继续增加更多自动压缩触发条件。阶段 7 的完整插件系统应在这些稳定核心之上单独设计注册、排序、超时、错误隔离和释放语义。
