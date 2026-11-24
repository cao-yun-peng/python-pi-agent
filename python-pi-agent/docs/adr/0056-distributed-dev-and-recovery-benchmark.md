# ADR 0056 统一开发环境与可复核的恢复测量

## 背景

阶段 1–7 已提供 PG/Redis、持久检查点、心跳和自动接管，但每次验收还需要手工组装临时服务和环境变量。正确性测试也不能回答指定并发、池大小和故障条件下的吞吐与延迟。需要可运行、无真实模型费用的测量入口，并保留判断数据有效性的证据。

## 决定

- `dev/distributed/compose.yaml` 提供 PostgreSQL 16 与 Redis 7 开发服务、健康检查和固定回环端口。PG 使用项目专属 named volume，默认 down 保留数据；Redis 只保存易失协调，不启用 RDB/AOF。信任认证仅用于这个回环开发环境，不作为生产部署模板。
- `scripts/distributed_dev.py` 是仓库级跨平台入口，显式提供 up/down/status/migrate/test/demo/benchmark/verify。verify 顺序执行健康启动、显式迁移、集成、示例与小规模 benchmark；命令失败即停止，保留现场供检查。默认不重启 Redis，额外 --restart-redis 仅允许重启对应项目带专用标签且固定回环端口的 Redis。
- 环境地址在入口解析一次；PG/Redis 测试变量只传给子进程，不写 .env、不打印环境。参数始终使用 argv，脚本不构造 Shell 字符串，不 prune 其他项目、不自动删除开发数据卷。默认单元测试不启动 Docker，真实集成继续使用显式 TEST_DSN/TEST_URL。
- benchmark 位于 application：`benchmark_report.py` 定义设置、样本和纯统计，`recovery_benchmark.py` 组装正式 Worker/Dispatcher/Builder/loop，`benchmark_cli.py` 拥有临时 schema、独立 pool/client 和报告输出。每 Worker 独立资源，无全局执行锁；进程内 instrumentation 只计数，不参与授权。PG 的 bind_writer 返回标注对齐既有 ExecutionWriter 端口，以允许类型安全的故障装饰。
- 使用固定版本 FakeProvider 数据集 recovery-fake-v1，分别测量普通文本、部分模型流中断、最终 assistant 后中断、部分工具结果后中断。只在正式提交成功后注入存储中断，工具恢复仍走正式 unknown 补偿；最终消息后恢复必须零新增模型请求。每样本验证持久 Run、prompt、attempt/dispatch、模型/工具次数及 provenance。
- warmup 单独记录；吞吐计时排除 admission 和 warmup，包含扫描、领取、配置重建与恢复到持久终态观察的时间。完成延迟从 admission 开始；恢复延迟从故障注入到 Worker 确认持久终态。报告注明这是真正释放许可后的协作中断，不包括进程死亡后 TTL 自然到期，进程硬终止与 Redis 重启的正确性沿用独立故障矩阵。
- p50/p95/p99 使用 nearest-rank 并保留样本数、min/max 与原样本；冲突率定义为 busy / (busy + claimed)，包含 Redis 占用和 PG 拒绝。不能把此值解释成 SQL CAS 失败率。统计重试、扫描、清理、后台失败类型和非完成状态；异常文本、DSN、nonce、prompt、工具参数和 provider state 不进入报告。
- 报告记录 code commit/dirty、benchmark 源码摘要、Python/驱动/服务版本、主机平台、数据集与规模、池/Worker/并发、TTL/心跳/I/O 期限、故障点及测量边界。CLI 原子写 JSON；失败/超时/取消非零退出，不把不完整样本算成功。一个批次无效则停止后续批次，防止遗留候选污染后续测量。关闭错误使报告无效，重复取消仍等待所有 Worker 清理。
- 每次正式 benchmark 使用新建的随机 schema 与 Redis 前缀，显式迁移；只删除本次生成的 schema，并在 Worker/pool 退出后执行。资源通过 AsyncExitStack 逆序关闭。原始报告保存到忽略的 .tmp，避免提交运行数据；教程和方法说明长期维护。

## 备选方案

只提供 Linux Shell 会让 Windows 走另一套流程；选择标准库 Python 入口。内嵌新执行循环会测到不同语义；选择正式包与窄故障代理。把所有故障合成一个吞吐数字会隐藏阶段差异；选择独立批次、原样本和明确的计时边界。用数十个样本给生产恢复 SLO 会误导，因此报告样本分位数但不外推。

## 后果

核心 loop、状态机、SQL 迁移与日志格式不变。Compose 和开发脚本属于源码仓库工具，wheel 包含正式 benchmark 模块，可用 `python -m pi_agent_py.application.benchmark_cli` 运行。阶段 8 提供开发环境与条件化测量，不提供生产 HA、外部副作用 exactly-once、真实模型性能、网络分区拓扑或跨进程负载生成器。

## Definition / Provider / Consumer / Lifecycle / Evidence

Definition 为 BenchmarkSettings/Sample/JSON v1 和测量定义；Provider 为 FakeProvider/正式 PG/Redis；Consumer 为 app benchmark 与显式开发命令。Lifecycle 为 Compose 显式启动/停止、Worker join、逆序关闭与随机 schema 清理。Evidence 为纯统计/手动时钟测试、真实独立 pool/client、统一 verify 以及可复核报告。

## 验证方式

见[教程 54](../../tutorials/54-distributed-dev-benchmark.md)、[测量方法](../benchmarks/recovery-methodology.md)、[示例 57](../../examples/57_recovery_benchmark.py)和[故障矩阵](../design/phase-1-recovery-fault-matrix.md)。本地使用 Windows/Python 3.12、Compose 2.40、PostgreSQL 16、Redis 7；测试与 benchmark 全部 FakeProvider，无付费调用。默认离线测试不访问服务，真实服务验证通过专用 Compose 项目开启，不声称已在其他主机或 CI 多版本服务环境测量。

本地验收记录（2026-11-24）：聚焦离线回归 400 项，339 通过、61 因服务条件未启用跳过；新增 benchmark/开发入口离线测试 14 项，覆盖纯统计、手动时钟超时、重复取消、扫描失败分类与脱敏。统一 verify 的显式 PG/Redis 集成 64 项通过，包含真实 Redis 重启、独立进程 kill 和四种 benchmark 场景；正式示例两种模式与 3 样本/场景 smoke 报告通过。最终构造/清理改动后的真实 benchmark 集成再次通过。

Ruff、76 模块 strict mypy、文档本地链接和 git diff --check 通过。离线 wheel 检查包含三个 benchmark 模块和两份既有 SQL 资源，屏蔽 redis/asyncpg 导入仍能加载稳定 API 和 benchmark 设置/CLI。可复核性能数字以从最终干净提交生成的 .tmp JSON 为准，报告自行记录 commit/dirty 与运行条件，不把本地观测固化为性能承诺。
