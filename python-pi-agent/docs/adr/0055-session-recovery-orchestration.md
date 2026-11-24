# ADR 0055 受监督的跨 Worker 接管编排

## 背景

ADR 0054 已让同一 Agent loop 按持久检查点显式 resume。自动恢复还需要发现候选、重建原配置、控制并发与资源所有权。扫描到 Session 不代表获得执行权；再次 submit 或在调度器中再次 claim 都会破坏原 Run 的身份、队列和预算。

## 决定

- 新增 RecoveryDispatcher 端口与终态 RecoveryOutcome。SessionRecoveryWorker 只消费执行存储、租约、时钟和恢复端口，不依赖 PG 私有方法或重写 loop。候选查询新增可选 exclude，既有默认调用兼容，SessionStore 六方法不变。
- Worker 以 TaskGroup 监督扫描与每 Session 一个本地任务，允许不同 Session 并行；跨 Worker 的最终授权仍由 Redis 协调和 PG epoch 决定。占用/暂时失败使用有上限的指数退避，排除活跃与冷却 Session 后再做有界查询，避免最早的占用候选长期挡住后续 Session。
- Worker 获取 Redis 后调用一次 claim_next；提交未知只查询原 operation。把已领取 receipt/lease 直接交给 dispatcher，不生成新 Run、不再次领取。恢复结果必须与持久终态的 Run、epoch、attempt、错误码相符。达到 attempt 上限的无 grant 终态不调用配置工厂。
- RegisteredRecoveryDispatcher 用不可变 `(recovery_spec_ref, recovery_spec_hash)` 注册表选择应用工厂。先开启心跳，再有界重建全新、无嵌套 Worker 的 Runtime。原 Provider、工具、审批/Gate、workspace 及其版本由应用工厂重建和验证；缺失或不兼容必须明确拒绝。摘要是应用的配置身份契约，不是自动计算的安全证明。
- 工厂返回的 Runtime 由本次 dispatch 拥有；其 PG/Redis 和读取端必须借用同一组对象与 owner。Builder 负责未完成构建的回滚。拒绝复用正在工作的 Runtime，防止一个任务关闭另一任务的资源。配置拒绝提交稳定 failed 原因；瞬时存储故障、超时、父级取消和失租保留 running 供合法新持有者接管。
- 内部 prepare/work/finish/end claimed 接缝由手动 resume 和 Worker 共用。执行前用 replay 校验 checkpoint 的 prompt/assistant 引用、Run 关联、控制交付和未决工具集合。非法历史不发起推理或工具。已提交最终 assistant 直接结束；工具尾部只生成缺失的 unknown 结果，保留 ID、名称与恢复 provenance，绝不自动重跑原工具。
- 临时恢复 Runtime 可以接受新的 steer/follow-up。当前 Run 结束后关闭本地派发窗口，保留后续已接受的 durable queued Run；本地 handle 明确报告作用域已关闭，下次候选扫描继续执行。不能让临时 Runtime 的默认关闭取消这些持久任务。
- Builder.with_recovery 显式注册 dispatcher 的 owned/borrowed 与清理句柄，要求已配置 distributed execution。根 Runtime 拥有并启动 Worker，关闭时先停止扫描与接管任务，再关闭本地 Session 与基础设施。默认立即取消，有界 shutdown_grace 可允许 drain；清理任务 shield 并等待，重复取消也不能遗留心跳。
- Worker 状态、扫描故障、接管决策、配置拒绝与清理异常通过隔离 Capability 事件观察；只包含关联 ID、计数、版本和错误类别。后台不可恢复故障记入 failure 并在关闭时报告，启动失败回滚。清理失败不覆盖已提交成功，许可比较清理失败则等待自然到期。

## 备选方案

把配置或 Python 对象序列化到数据库会引入秘密与代码装载风险；选择由应用授权的显式工厂。全局串行调度会让慢 Session 阻塞全部候选；选择有界 Session 并行加存储裁决。仅凭本地锁或 Redis key 执行不能防止旧执行者写入；必须保留 PG grant 校验。另一套恢复 loop 会使工具安全和补偿语义分叉，因此复用正式执行路径。

## 后果

本步不新增 SQL migration，不改变 Session v6 或 Run journal v1 格式；新增能力通过稳定 API 导出。自定义 SessionExecutionStore 需支持 list_candidates 的可选 exclude 参数，自定义 dispatcher 必须监督心跳并消费已领取许可。运行配置注册和数据源配对由应用负责。

跨进程故障可以恢复持久状态，外部副作用仍可能已经发生；fencing 只保护数据库提交。Worker 也不能强行终止不协作取消的任意代码。首版省略自动插件升级/配置迁移、跨租户公平保证、Redis HA 拓扑、生产部署与恢复延迟 benchmark，后两项开发环境与测量工作留给阶段 8。

## Definition / Provider / Consumer / Lifecycle / Evidence

Definition 为 RecoveryDispatcher/Outcome 与有界设置；Provider 为注册工厂 dispatcher，候选端由 PG 实现；Consumer 为独立 Worker 和 Builder。Lifecycle 为显式 start/aclose、TaskGroup、许可比较清理和按所有权释放。Evidence 为 FakeProvider/手动时钟、两个独立 PG pool/Redis client，以及同步屏障后的真实子进程硬终止。

## 验证方式

见[第 13 节故障矩阵证据](../design/phase-1-recovery-fault-matrix.md)、[教程 53](../../tutorials/53-session-recovery-workers.md)与[示例 56](../../examples/56_recovery_workers.py)。默认离线测试不连接基础设施，不产生模型费用。真实集成必须显式启用环境变量，使用隔离 schema 与随机 Redis 前缀；Redis 状态丢失通过删除任务自有 key 注入，真实服务重启有额外专用容器开关，进程终止通过子进程输出屏障定位，过期通过任务自有服务端状态推进，不能据此声称生产 HA 或恢复延迟分位数。

本地验收记录（2026-11-24，基线 128c2fa 上的本次阶段 7 变更）：Windows/Python 3.12，asyncpg 0.32、redis-py 8.2，PostgreSQL 16/Redis 7 专用回环容器。受影响的 Runtime、控制、存储、插件、生命周期和 API 离线回归共 385 项：325 通过、60 项因未启用服务条件跳过；其中接管离线测试 29 项。显式服务验收分两组，接管 6 项、原 PG/Redis/Runtime 契约 57 项，合计 63 项通过。真实重启使用固定回环端口和关闭持久化的独占 Redis，进程硬终止只杀测试拥有的子进程。

Ruff、73 模块 strict mypy 与 git diff --check 通过。示例 56 的最终 assistant/部分流模式分别得到模型请求数 [1, 0]/[1, 1]，两者 prompt_count=1、attempt/epoch=2、旧 writer 拒绝。离线 wheel 验证包含新增模块和两份既有 SQL 资源；屏蔽 redis/asyncpg 导入仍能加载稳定恢复 API。文档本地链接和故障矩阵离线方法名已核对。没有付费模型调用；本地 Python 3.12 证据不代替 CI 多版本或生产部署验证。
