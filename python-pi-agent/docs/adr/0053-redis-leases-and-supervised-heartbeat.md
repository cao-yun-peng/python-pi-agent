# ADR 0053 Redis 协调租约与受监督心跳

## 背景

ADR 0052 已由 PG 裁决执行 epoch，但需要避免多个 Worker 持续争抢数据库，并在 Redis 清空、连接失联或 PG 续期失败时停止当前工作。只启动一个无人等待的续租任务会隐藏错误；只调用 task.cancel() 也不能证明工作和清理已经退出。

## 决定

- SessionLeaseManager 保留 acquire/renew/release。新增 LeaseClock 和窄接口 ExecutionGrantManager，心跳只依赖端口与领域对象。SessionExecutionStore 继承后者，既有 PG 实现签名不变。
- InMemoryLeaseManager 是单事件循环、实例内的替身。它惰性检查单调时钟，不创建过期任务；clear 模拟 Redis 状态丢失。ManualLeaseClock 控制睡眠与超时，默认测试没有真实 TTL 等待。
- RedisLeaseManager 借用 redis.asyncio 兼容客户端，构造和导入不连接网络、不导入可选 SDK、不注册全局对象。应用负责客户端、连接池、socket 超时、重试策略与 aclose。增加独立 redis extra。
- 一个 Session 一个 Redis key，以 Session ID 的 SHA-256 作为 hash tag。value 是版本化规范 JSON，包含 session/owner/claim/随机 nonce，排除 expires_at。nonce 防止同一 owner/claim 再次取得租约后的 ABA；调用方不能复用测试 nonce_factory 的值。
- acquire 用单 key Lua SET NX PX；renew/release 在 Lua 中比较完整 value 后 PEXPIRE/DEL。Redis TIME 返回服务器时间用于 SessionLease 的 Unix 时间元数据。TTL 统一向下取整到毫秒，范围为 1ms 至 24h。错误和畸形回复不伪装成占用或正常失租。
- Redis 只作协调；最终执行权仍由 PG epoch/当前许可/数据库时间裁决。Redis 重启、键清空或旧 primary 失联均不产生可信新 epoch。Redis 键丢失后另一个 Worker 即使取得协调租约，也不能越过仍有效的 PG grant。
- LeaseHeartbeat 是一次性作用域。接收已有 lease/grant 即接管二者的释放责任，但借用客户端。run 开始先重新验证 Redis，再续 PG，之后才派发 work；不信任历史领取回执仍然有效。
- 每轮顺序固定 Redis → PG；各 I/O 有独立可取消时限。心跳间隔、两个 I/O 时限和安全余量之和必须严格小于实际毫秒 TTL。客户端派发截止线用单调时钟和 Redis 请求开始时间保守估计；新 PG 续期必须在旧截止线内成功，过期作用域不得复活。
- TaskGroup 拥有工作与心跳；失租、异常或超时关闭派发门并取消工作，然后等待所有任务退出。work 在每次新派发前调用 ensure_active，并响应取消。被取消的工作即使返回值，也不能覆盖已确定的失租错误。重复 aclose/取消不会遗漏清理任务。
- 可选 finalize 在工作成功、心跳取消并等待退出后运行，且受剩余许可时间与 grant_timeout 约束。它用于 finish_run，避免终态清除 PG grant 与最后一次心跳竞争。finalize 失败、超时或 SessionCommitUnknownError 原样传播，不自动重试业务操作。
- 清理先比较释放 PG，再比较释放 Redis。屏蔽父任务的重复取消，并等待清理退出；两个释放各有期限。清理失败通过 cleanup_failures 和事件报告，不覆盖已有业务结果或失租原因。过期/不再拥有的释放返回 false 是正常结果。
- 发出 started/renewed/lost/cleanup_failed/stopped capability 事件，包含 epoch、稳定原因、错误类型，不含 nonce、完整驱动错误或连接串。观察者失败沿用既有隔离语义；慢观察者应使用 BufferedEventSink。

## 备选方案

Redis INCR 作为最终 epoch 会在 Redis 数据丢失后倒退；无条件 DEL 可能删除新持有者；先续 PG 后发现 Redis 失租会无谓延长旧执行权；fire-and-forget 心跳无法传播故障并证明退出。以上均不采用。

把所有租约检查直接塞进 Agent loop 会扩大核心职责。本步交付独立心跳消费者，Runtime 注入和恢复扫描仍按步骤 6–7 实现。

## 后果

客户端超时可能发生在 Redis 命令已经执行后。申请结果未知时不偷偷重试，不拥有可靠句柄的租约等待 TTL 回收；续期结果未知时保守停止。释放失败可能延迟下一次领取，但 PG 始终拒绝旧许可写入。

取消是协作式的，无法强制杀死吞掉取消且永不返回的外部 SDK/工具；移交许可前必须等待受管任务退出，外部系统仍需自己的幂等与隔离。本步不承诺撤销已发出的副作用，不自动将失租 Run 标成 completed/cancelled，也不自动接管。日志与检查点保留到后续恢复流程。

## Definition / Provider / Consumer / Lifecycle / Evidence

Definition 是 SessionLease/SessionLeaseManager、LeaseClock 与 ExecutionGrantManager；Provider 是内存/Redis 租约、系统/手动时钟与既有 PG grant；Consumer 是独立 LeaseHeartbeat 和示例 54。所有资源由应用显式组装，作用域等待工作、心跳和清理结束，客户端由应用关闭。

## 验证方式

离线测试覆盖互斥、过期、nonce ABA、全部身份字段比较、清空、超时、PG 拒绝、启动取消、父任务重复取消、吞掉取消的假成功、结束提交不确定/超时、清理故障及零剩余任务/时钟等待。

显式 Redis 7 测试使用两个独立客户端竞争，服务器命令强制过期而不 sleep。PG + Redis 测试验证键丢失后的双重许可与旧 epoch 拒绝，并验证先停止心跳再提交终态。测试只删除自身前缀或 Session key，不对任意配置端点执行 FLUSHDB。FakeProvider 示例覆盖成功和键丢失取消，不调用付费模型。操作命令与掌握标准见[教程 51](../../tutorials/51-redis-leases-heartbeat.md)。

本次本地验证：2026-11-23，Windows/Python 3.12，redis-py 8.2.0、Redis 7.4.10、asyncpg 0.32.0、PostgreSQL 16.16。租约/心跳离线核心 30 项通过，显式双存储及既有 PG/共享 Store 回归 62 项通过；示例成功/失租两条路径通过，Ruff 与 57 模块 strict mypy 通过。离线 wheel 验证新模块、redis extra、既有 SQL 资源，以及在禁用可选驱动导入时仍可导入稳定 API。常规 CI 的 Python 版本矩阵仍按 pyproject 与既有 workflow 执行，本地结果不代替多版本证据。

参考：[Redis EVAL](https://redis.io/docs/latest/commands/eval/)、[Redis TIME](https://redis.io/docs/latest/commands/time/)、[redis-py 异步客户端生命周期](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html)。
