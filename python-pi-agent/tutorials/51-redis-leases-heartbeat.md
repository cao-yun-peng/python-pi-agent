# 第 51 章 Redis 租约与受监督心跳

本章解决运行期间“协调租约还能否信任”的问题。PG 已保证旧许可不能写入持久事实；Redis 减少 Worker 争抢，心跳在任一基础设施不可信时停止派发并取消当前工作。独立心跳作用域已经实现，AgentRuntime 自动注入和恢复扫描是后续阶段。

## 运行正式示例

在仓库根目录使用现有虚拟环境，安装两个可选驱动并提供专用开发端点：

```powershell
.\.venv\Scripts\python.exe -m pip install -e './python-pi-agent[postgres,redis]'
$env:PI_AGENT_POSTGRES_DSN='postgresql://postgres@127.0.0.1:5432/pi_agent_dev'
$env:PI_AGENT_REDIS_URL='redis://127.0.0.1:6379/0'
.\.venv\Scripts\python.exe python-pi-agent/examples/54_redis_heartbeat.py --migrate
.\.venv\Scripts\python.exe python-pi-agent/examples/54_redis_heartbeat.py --lose-lease
```

示例只请求 FakeProvider。成功轨迹为 started → renewed → prompt/dispatch/assistant 检查点 → stopped，Run 为 completed，scope 为 closed。失租模式只删除该示例自己的 Session key：lost(redis_lost) → owned_work_cancelled → stopped；Run 仍为 running、PG active Run 保留，等待后续恢复判断。两个模式均输出空 cleanup_failures，不遗留心跳。示例生成的 Session 历史留在开发数据库中供检查。

## 三种身份，两个时间体系

Redis value 的 owner/claim/nonce 必须全部相同才可续期或释放。每次重新 acquire 生成新 nonce，即使 owner/claim 被重用，旧请求也删不掉新租约。value 不包含 expires_at，因此续期后的旧句柄仍可比较释放同一租约。

PG epoch 是另一种身份：它在数据库 Session 锁内递增，Redis 不生成最终 fencing token。Redis 清空后 B 能取得新租约；只要 A 的 PG grant 仍然有效，B 的 claim_next 就会被拒绝。A 心跳发现 Redis 键丢失后停止工作并释放旧 PG grant，B 才能接管，epoch 递增，A 的新写入被拒绝。

SessionLease.expires_at 来自 Redis TIME，作为跨系统 Unix 时间包络；PG 仍独立使用数据库时间。心跳派发截止线使用本机单调时钟，保守地从 Redis 请求开始计 TTL，再扣安全余量，避免本机墙钟跳变让过期许可复活。跨服务时钟异常可能降低可用性，但最终写入仍由 PG 判断。

## 正式模块与调用边界

从 pi_agent_py.api 导入 RedisLeaseManager、RedisLeaseSettings、LeaseHeartbeat、LeaseHeartbeatSettings 和错误类型。应用构造并借出异步 Redis 客户端与 PG pool；适配器不关闭它们，也不启动后台线程。Redis SDK 是可选 extra，纯契约和离线运行无需安装它。

LeaseHeartbeat 只依赖 SessionLeaseManager 和 ExecutionGrantManager。run(work, finalize=...) 先重新验证两份许可，之后用 TaskGroup 拥有 work 与心跳。每次新模型/工具派发前，work 调用 scope.ensure_active()；每次持久写入还必须经过绑定 writer 的 PG 校验。一个 writer 捕获一个 grant 对象，operation 摘要应使用它实际捕获的对象；心跳更新后可重新 bind_writer，不能在摘要与请求之间混用两个 expires_at。

正常工作只提交到最终边界；finish_run 放进 finalize。此时心跳已退出，不会把正常清除 grant 误判为失租。finalize 在剩余有效时间内运行，超时可能意味着业务提交结果未知；保留原 operation_id 并查询 resolve_operation，禁止自动重跑模型或工具。SessionCommitUnknownError 保留其查询身份。

## 期限、取消与清理

配置约束是：heartbeat_interval + redis_timeout + grant_timeout + safety_margin 严格小于向下取整后的 TTL。TTL 支持 1ms 到 24h。适配器 command_timeout 和客户端 socket 超时还应与心跳预算配合；应用显式关闭驱动对变更命令的隐式重试，示例展示零次重试配置。

每轮先续 Redis，再续 PG。Redis 返回不匹配是 redis_lost；驱动故障和命令超时分别报告 redis_error/redis_timeout。PG 冲突是 grant_lost，其他故障和超时为 grant_error/grant_timeout。超时与连接错误不等于命令一定没执行。任一路径都保守停止，不能用旧回执继续派发。

作用域持有 lease/grant 的释放责任，客户端仍归应用。失租、业务异常或父任务取消后，它先等待受管任务退出，再有界释放 PG 和 Redis；重复取消不会跳过第二项清理。cleanup_failures 和 capability 事件记录释放错误类型，不覆盖已经完成的结果。aclose 可重复调用，也能释放未启动作用域；同一个作用域只能运行一次。

协作式取消无法撤销已经发出的工具副作用。自定义客户端和 work 必须响应取消；吞掉取消并永不返回的代码无法同时满足“强制终止”和“无任务泄漏”。本项目等待受管任务退出，外部系统仍需自己的超时、隔离和幂等策略。失租不会自行生成虚假的 Run 终态。

## 离线与真实验证

```powershell
Push-Location python-pi-agent/tests
..\..\.venv\Scripts\python.exe -m unittest test_session_lease test_lease_heartbeat -v
$env:PI_AGENT_REDIS_TEST_URL=$env:PI_AGENT_REDIS_URL
$env:PI_AGENT_POSTGRES_TEST_DSN=$env:PI_AGENT_POSTGRES_DSN
..\..\.venv\Scripts\python.exe -m unittest test_redis_lease test_redis_pg_heartbeat -v
Pop-Location
```

默认测试使用 InMemoryLeaseManager、窄 PG 替身、纯转换和 ManualLeaseClock，不连接服务、不等待真实 TTL。advance 推进超时/睡眠，await asyncio.sleep(0) 仅让出调度；断言工作/心跳/清理任务及手动时钟等待全部消失。Windows 执行环境按 AGENTS.md 处理。

真实测试显式设置端点；Redis 测试只清理随机前缀，PG 测试只清理自身 schema。以 pexpireat(key, 1) 强制服务器过期，不依赖固定等待。删除单个租约 key 对续期具有与 Redis 清空相同的可观察状态，测试不会对任意端点运行 FLUSHDB。

练习：用同 owner/claim 重新申请并比较 nonce；在 Redis 清空后让新 Worker 争取 PG grant；分别阻塞 Redis/PG 续期并推进手动时钟；让 work 吞掉取消但返回值，确认仍报告失租；在 finalizer 中注入提交未知，确认需要查询原操作。

## 设计映射与掌握标准

本章沿用项目显式端口、能力事件与资源 owner，不新增与固定 Pi commit 56b25ff4ebbd8a119ef9185447c7b2416059dbcb 的同名分布式 API 兼容承诺。DeepSeek Harness 映射为 Definition=租约/时钟/续期端口，Provider=内存与 Redis，Consumer=心跳作用域，Lifecycle=显式组装/等待退出/应用关闭客户端，Evidence=确定性和真实双存储测试。

能解释 Redis 互斥与 PG fencing 的区别，复现失租取消和零后台泄漏，说明结果未知为何不能重跑副作用，并指出自动 Runtime 接线、恢复扫描和外部副作用去重仍在范围之外，即达到本章掌握标准。决策见 [ADR 0053](../docs/adr/0053-redis-leases-and-supervised-heartbeat.md)。
