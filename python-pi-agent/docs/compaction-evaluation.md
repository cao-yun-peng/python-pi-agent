# Compaction Quality Evaluation Plan

本文只定义评测思路和数据契约，不绑定付费模型或外部 Eval 平台。目标不是判断摘要“文笔好不好”，而是验证压缩后 Agent 是否仍能完成原任务。

## 1. 从 OpenAI 官方方法中借鉴什么

截至 2026-10-20，OpenAI 官方评测建议可以浓缩为四点：

1. 先定义具体成功标准，再收集能代表真实分布的数据，并持续把生产失败加入 eval set。
2. 摘要评测不能只依赖 BLEU/ROUGE 等通用指标；应同时验证相关性、准确性和连贯性。
3. LLM 更擅长 pairwise、分类和按明确 rubric 打分，而不是无约束地评价“好不好”；模型裁判要用人工标签校准。
4. Agent 先用 trace grading 找到阈值、工具、路由或压缩阶段的错误，再把稳定标准沉淀成可重复 datasets 与 graders。

官方参考：

- [Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [Evaluate agent workflows](https://developers.openai.com/api/docs/guides/agent-evals)
- [Graders](https://developers.openai.com/api/docs/guides/graders)

OpenAI 当前文档同时标注旧 Evals 平台正在弃用，因此本项目借鉴评测方法，不让核心代码依赖某个正在迁移的平台 API。

## 2. 一个 eval case 应保存什么

建议后续在 `evals/compaction/cases.jsonl` 使用如下逻辑结构：

```json
{
  "case_id": "tool-debugging-001",
  "history": ["可序列化 Message ..."],
  "keep_recent_turns": 1,
  "required_facts": [
    {"id": "goal", "value": "修复 Windows 文件锁冲突", "critical": true},
    {"id": "decision", "value": "保持 append-only JSONL", "critical": true}
  ],
  "forbidden_claims": ["问题已经完全修复"],
  "probes": [
    {"question": "当前目标是什么？", "reference_answer": "修复 Windows 文件锁冲突"},
    {"question": "下一步是什么？", "reference_answer": "增加并发回归测试"}
  ]
}
```

不要只保存一份“标准摘要”。同一事实可以有多种正确表达；真正重要的是事实、约束、未完成状态和后续可执行性。

数据集至少覆盖：普通问答、工具调用、多次失败、用户纠正、相互冲突的信息、未完成任务、长工具输出、多语言、恶意指令藏在工具结果中，以及连续二次压缩。

## 3. 三层 grader

### 第一层：确定性结构检查

这层离线、快速，适合每次提交运行：

- durable event 是否可 replay，sequence 是否连续。
- leading system 与 recent turns 是否原样保留。
- tool call/result 是否没有被拆断。
- summary 是否非空并带明确边界标记。
- 压缩后估算 token 是否小于压缩前。
- 摘要是否包含必须精确保留的 ID、路径、错误码或用户明确约束。
- 是否出现 `forbidden_claims`。

字符串检查只适合不可改写的标识符，不能拿来判断所有语义事实。

### 第二层：下游 probe QA

分别给“完整历史”和“压缩历史”同一组问题，比较结构化答案：

```text
full history     -> answer_full
compacted history -> answer_compacted
```

优先使用可自动判断的 probes，例如项目代号、已选方案、尚未解决的错误、文件路径、数值和下一步动作。这里测的是压缩后的任务能力，而不是摘要和参考文本长得像不像。

建议核心指标：

- critical fact recall：关键事实召回率，关键项应为 100%。
- contradiction rate：与原历史冲突的断言比例，目标为 0。
- downstream task success delta：压缩上下文相对完整历史的任务成功率下降。
- compression ratio：压缩后估算 token / 压缩前估算 token。
- unsupported claim rate：摘要中无法由原历史支持的事实比例。

### 第三层：模型裁判与人工校准

对难以精确匹配的事实，用独立 judge 按 rubric 做 pass/fail 或 pairwise：

- 目标是否保留。
- 用户约束和拒绝项是否保留。
- 已完成与未完成状态有没有混淆。
- 关键工具发现和错误是否保留。
- 是否虚构结果。
- 下一步是否仍可执行。
- 是否把不可信工具文本误提升为系统指令。

pairwise 时随机交换 A/B 顺序，控制长度差，避免位置偏差和偏爱长答案。先由人类标注一小批 good/bad/borderline 样本，再检查 judge 与人工的一致率；不能用未经校准的 judge 给自己证明正确。

## 4. Trace grader 看“过程”而非只看摘要

自动压缩还要评估 runtime trace：

- 阈值以下不得调用摘要模型。
- 达到阈值后最多追加一个预期的 compaction event。
- 无可压缩 turn 时必须 skipped，而不是循环重试。
- 摘要失败或取消后 durable log 不变。
- 版本冲突不得覆盖并发消息。
- 下一轮 Provider context 必须来自 replay 后 snapshot。
- 工具结果使阈值越界时，必须先持久化完整 assistant tool call/tool result，再追加 compaction event，最后发起第二次 Provider 请求。
- 工具续轮压缩不得拆散 tool call/result；摘要失败或取消时保留已经提交的工具轮次，并且不得发起第二次 Provider 请求。
- 记录压缩耗时、摘要 token 成本、压缩前后 token 和触发频率。

这对应 OpenAI 所说的 trace-first：先定位失败发生在 policy、summary、store 还是下游回答，再决定优化哪一层。

## 5. 第一版通过门槛建议

小规模教学基线可以从 30 个手工 case 开始：10 个普通对话、10 个工具任务、5 个纠错/冲突、5 个长上下文或二次压缩。每次修改摘要 prompt、模型、turn 边界或阈值后运行同一版本化数据集。

建议门槛：

- critical fact recall = 100%。
- contradiction rate = 0%。
- deterministic trace checks = 100%。
- downstream task success delta 不低于完整历史超过 5 个百分点。
- p95 compression ratio ≤ 0.40；若未达到，只表示压缩收益不足，不应与事实正确性混成一个总分。
- 模型 judge 与人工 pass/fail 一致率达到 90% 后，才允许它承担批量初筛。

这些数字是本项目的起始假设，不是 OpenAI 官方阈值。积累真实失败后应重新校准。

## 6. 推荐实施顺序

1. 先实现纯 Python `CompactionEvalCase` 与确定性 grader，不调用模型。
2. 增加 probe runner，用 `FakeProvider` 固定验证数据管道。
3. 接入一个显式标记的真实模型集成 eval，默认测试不运行、不产生费用。
4. 保存每次报告的代码 commit、摘要模型、prompt 版本、estimator 和数据集版本。
5. 把生产 trace 中的新失败匿名化后加入数据集，形成持续评测循环。
