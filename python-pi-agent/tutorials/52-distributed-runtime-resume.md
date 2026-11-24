# 第 52 章 持久 Run 的 Runtime 接线与恢复

本章把前五步的端口接入 Builder、AgentSession 和原有 loop。要学习的是：一次请求已经被持久接受，不代表它已经完成；恢复必须继续同一 Run，而不是重新提交相同文本。

## 运行正式示例

在仓库根目录使用现有虚拟环境，准备专用开发 PG/Redis：

```powershell
.\.venv\Scripts\python.exe -m pip install -e './python-pi-agent[postgres,redis]'
$env:PI_AGENT_POSTGRES_DSN='postgresql://postgres@127.0.0.1:5432/pi_agent_dev'
$env:PI_AGENT_REDIS_URL='redis://127.0.0.1:6379/0'
.\.venv\Scripts\python.exe python-pi-agent/examples/55_distributed_runtime.py --migrate
.\.venv\Scripts\python.exe python-pi-agent/examples/55_distributed_runtime.py --interrupt-after-assistant
```

两次均使用 FakeProvider。正常模式输出 prompt_count=1、completed、request_number=1、dispatches=1、attempt=1。故障模式在 PG 真实提交 assistant 后故意中断，输出 awaiting_boundary；新 Runtime 使用空脚本 FakeProvider 恢复，输出 resume_model_requests=0、prompt_count=1、completed、attempt=2。示例生成的 Session 历史留在开发库中供检查；本进程 Runtime 都关闭后才关闭客户端和 pool。

示例的 InterruptAfterAssistant 是故障代理，只包装提交结果，不包含另一套执行循环。机制代码全部来自正式包。

## 显式装配

从 pi_agent_py.api 导入 DistributedExecutionSettings、PostgresSessionStore、PostgresExecutionStore 和 RedisLeaseManager。Builder 的 with_session_store 提供读取端，with_distributed_execution 提供执行端、租约和配置；运行路径由该显式选项启用。默认 Memory/JSONL 不声明 fencing 能力，配置错误会在 build 时拒绝。

两个 Store 必须读取同一数据库/schema。能力 marker 不是数据源身份验证；这属于应用的装配责任。离线测试中的 ExecutionMemory 只是重用纯转换的端口替身，不会被 Builder 自动替代到生产配置中。

recovery_spec_ref/hash 是应用维护的恢复配置身份，必须覆盖 profile、Provider、工具、权限、workspace 等会影响执行的注册配置。此阶段比较已注册的引用/摘要，不序列化 Python 对象、不保存秘密、不自动重建配置。后续 Worker 将使用同一身份解析原组件。

使用 SessionSpecResolver 选择不同角色组件时，在 SessionSpec 中提供成对 recovery_spec_ref/hash；它们覆盖默认身份并保存到该 Run。缺少身份的组件切换会在打开 Session 时拒绝。恢复时选择错角色或退回默认配置，会在领取/模型派发前因身份不符被拒绝；观察 metadata 本身不承担恢复授权。

默认借用资源；with_distributed_execution 的 store_ownership/store_cleanup、lease_ownership/lease_cleanup 可登记应用拥有的释放句柄。例如只有一个资源注册 pool.close，其他共用该 pool 的适配器保持 borrowed，避免重复所有权。Runtime 先停止工作并等待心跳/清理退出，再按注册顺序的逆序释放资源。

## 持久接收与三种恢复位置

RuntimeSession.submit/follow_up 在返回 RunHandle 前调用 submit_run；普通 prompt 也能在进程中断后被发现。RunHandle 的 future 和流只属于当前进程，run_id 可用于读取持久 RunRecord。后续请求若遇到 Redis 占用或 FIFO 优先 Run，可能收到本地执行错误，但已接受任务仍在数据库中。

分布式 open_session 不启动旧 follow-up 恢复队列；调用 session.resume(run_id) 才恢复指定任务。该入口不新建 Run、不重新 submit。claim_run 在 PG 锁内检查优先顺序，目标不符不会扣减其他 Run 的 attempt；先恢复优先 Run，再显式恢复后面的任务。

| 持久位置 | 本次动作 | 不能做的事 |
| --- | --- | --- |
| before_prompt | 准备历史，再原子提交 system/user 或 follow-up delivery | 提前把输入只存在内存里当作已接受 |
| awaiting_model | 使用原 run_id、request_number 和历史派发 | 再次追加 prompt、重置预算 |
| awaiting_tools | 只补齐未提交结果，标为 unknown | 重跑工具或覆盖已提交结果 |
| awaiting_boundary | 使用原 assistant 和 Finish 执行边界判断 | 再次请求模型、再次执行工具列表 |
| terminal | 返回原 assistant 及当时历史前缀 | 再次领取、发起推理或重发完成事件 |

内部恢复函数接收已经领取并绑定的 writer，重用同一 Agent loop。最终消息后的恢复无工具续轮/steer 时直接完成；有待交付 steer 时，控制 delivery 与 ContinueRun 同事务，下一请求编号递增。无可交付控制时先关闭本地接受窗口，finish_run 再检查 PG 版本，防止过时观察静默丢输入。

## 两种预算与派发门

request_number 表示逻辑续轮；dispatches 表示实际 agent-turn 网络派发次数。模型流在第一轮中断，再次恢复仍可为 request_number=1，但 dispatches 必须从 1 递增到 2。ContinueRun 已保存而下一次网络尚未发生时中断，恢复从 request_number=2 继续，不从 1 开始。

每次 DispatchModel 都在网络前持久保存。Direct/Resilient/Recording provider 通过 ModelDispatchHook 与可选 bind_dispatch 接缝按 Run 绑定；绑定返回独立对象。重试和 fallback 都要重新预留，预算拒绝放在重试决策之外。自定义包装/重试 Provider 应实现 DispatchBindableModelProvider；原始 provider 的一次 stream 只允许一次物理派发，SDK 的隐式重试应关闭。

工具在逐调用入口检查许可，在异步审批返回后、实际执行前再检查。手动和自动压缩在 summarize 前检查许可，ModelCompactionStrategy 的内部模型重试也传递派发门。压缩模型检查租约，使用独立 compaction purpose，不扣 agent-turn 的两个预算。

运行完成事件在持久 finish 之后发出。逻辑序号、总派发数和 attempt 来自 journal；实时 token usage 和 AgentResult.usage 只描述当前 attempt，缓存结果不补造旧调用的 token 统计。流中断的远端计费可能未知，不能用本地计数伪装成精确账单。

## 工具尾部、取消与压缩许可

恢复复用正式 interrupted_tool_result：缺失工具结果包含 tool_call_id、名称、is_error 和 structured_content 的 interrupted/unknown/recovered 标记。已提交的结果不重复生成。若前一个 failed/cancelled Run 留下尾部，新 Run 用 RepairToolTail 在 before_prompt 阶段关闭整个尾部；其 journal 命令与实际恢复消息同事务，随后才能追加新用户输入。

用户明确取消本地受管工作时，等待工作退出，在许可有效时提交 cancelled。失租或存储结果未知时保留 running 检查点，不把中断伪装成业务终态。取消无法撤销已发出的外部副作用，unknown 补偿也不等于保证模型以后不会建议再调用；权限和 Gate 仍然有效。

自动压缩沿用当前 Run grant。手动 compact 需要独立 maintenance grant、epoch 和心跳；存在有效 Run 或待恢复 active Run 时会拒绝，即使 Redis key 已经丢失。摘要失租时取消、等待退出，原历史保持不变。

## 验证与练习

```powershell
Push-Location python-pi-agent/tests
..\..\.venv\Scripts\python.exe -m unittest test_distributed_runtime test_minimal_loop test_tool_runtime test_session_runtime test_session_control test_compaction test_app_runtime test_model_runtime -v
$env:PI_AGENT_POSTGRES_TEST_DSN=$env:PI_AGENT_POSTGRES_DSN
$env:PI_AGENT_REDIS_TEST_URL=$env:PI_AGENT_REDIS_URL
..\..\.venv\Scripts\python.exe -m unittest test_postgres_runtime -v
Pop-Location
```

离线测试使用手动时钟与正式纯状态计划；真实测试只操作自己的 schema/Redis 前缀。Windows 异步与 Temp 执行约束按 AGENTS.md 处理。

练习：分别在 prompt、assistant、第一条工具结果和 ContinueRun 提交后中断；观察 user 消息只出现一次、已完成工具结果保留、缺失工具被标 unknown、请求序号和 dispatches 连续。给重试层一个额外响应，验证预算拒绝后它从未被请求。最后让摘要跨越租约期限，确认下一次实际重试被派发门拒绝，且 Runtime 关闭后没有剩余 worker/heartbeat/cleanup 任务。

本章映射到既有端口、事件与资源 owner，不新增与固定 Pi commit 56b25ff4ebbd8a119ef9185447c7b2416059dbcb 的同名分布式 API 兼容承诺。DeepSeek Harness 对应配置定义、适配器、Runtime 消费、受监督生命周期和确定性证据。候选扫描、跨 Worker 配置重建和自动接管仍由阶段 7 交付；本阶段以显式 resume 为可运行边界。见 [ADR 0054](../docs/adr/0054-distributed-runtime-resume.md)。
