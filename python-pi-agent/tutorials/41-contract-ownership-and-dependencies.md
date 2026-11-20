# 第 41 章：契约应该属于谁

本章解决维护依赖问题：策略的输入/输出不能依赖执行它的上层模块。它发生在能力定义和组装阶段，不增加 Agent loop 阶段或第二套实现。

## 从接口的消费者倒推定义位置

RetryPolicy、BackoffStrategy、RoutePolicy 都需要理解失败证据和候选；因此 ProviderCandidate、AttemptFailure、RetryDecision、RetryAction 与协议同在 ports。Candidate 保存真实 ModelProvider 引用，不属于纯 domain。数据结构冻结不等于它是领域对象，也不等于其中活资源不可变。

providers/runtime 现在通过私有 ports 模块引用这些值，承担重试、回退与流式输出后的 fail-closed，不再拥有契约定义。新的具体导入来自 pi_agent_py.ports；根包既有公开导出仍指向相同对象。旧的 providers.runtime 契约属性直接移除，无 shim。

SessionSpecResolver 选择含 AgentComponents 的完整规格，因此与 SessionSpec、StaticSessionSpecResolver 在 runtime/session_spec 同层。应用消费这组契约；基础 ports 不反向引用运行规格。api.SessionSpecResolver 继续是稳定入口。

## 验证“只依赖”时也检查类型分支

test_architecture 只解析正式源码，不导入它来推导依赖。它覆盖 TYPE_CHECKING 和函数内静态导入，并规范化相对路径；测试字符串故意加入违规引用，证明失败路径有效。components 的四个安全默认实现与压缩编排的 context.compaction 属于精确例外，不允许整个 tools/context 自动放行。动态 importlib 不在该检查范围内。

在 python-pi-agent 目录依次运行：

```sh
python -m unittest discover -s tests -p test_architecture.py -v
python -m unittest discover -s tests -p test_port_contracts.py -v
python -m unittest discover -s tests -p test_model_runtime.py -v
python -m unittest discover -s tests -p test_session_spec.py -v
python examples/23_provider_resilience.py
python examples/31_session_spec.py
python -m ruff check src tests
python -m mypy
```

Windows 受限沙箱中，asyncio 测试按根目录 AGENTS.md 在沙箱外离线运行。test_port_contracts 验证原字段校验与反射，不需要网络；模型和会话测试使用既有 FakeProvider。

## 观察轨迹与失败路径

示例 23 使用 FakeProvider：primary 两次超时 → retry/backoff → fallback → 文本完成。观察 provider_attempt_*，测试同时检查 run settled，类型搬迁不应改变事件顺序。示例 31 按角色选择工具并完成续写，随后为同一 Session 请求另一规格，观察 RuntimeStateError 冲突拒绝。取消期间不得多发请求或扩大权限；test_model_runtime 的退避取消、流输出后不重试，以及 test_session_spec 的 resolver 取消与关闭覆盖这些边界。

契约 DTO 不初始化连接，不注册插件，也没有独立 cleanup；Candidate 借用 Provider，所有权仍由 Builder/LifecycleScope 管理。没有新增 durable 状态、观察后台任务或付费模型依赖。

## 取舍与掌握标准

本次保持单包与 ports.py，省略动态依赖图、热重载和独立契约包。Pi/DeepSeek 概念沿用既有模型组合与能力接缝映射，不声称新增上游行为对齐，见 upstream-mapping 和 ADR 0043。

完成练习时应能：解释为什么 Candidate 不进 domain；从调用链找到契约、实现与组装者；在合成源码中验证违规导入失败；通过唯一对象身份和已有取消测试证明迁移没有复制类型或改变执行语义。
