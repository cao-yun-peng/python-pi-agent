# 恢复 benchmark v1 测量方法

正式入口为 `pi_agent_py.application.benchmark_cli`，源码与设置、统计、执行分离；[教程 54](../../tutorials/54-distributed-dev-benchmark.md)提供完整启动命令。报告 schema_version=1，数据集 recovery-fake-v1；所有模型行为为 FakeProvider，不代表真实模型延迟、费用或质量。

## 实验条件

- 每次 invocation 创建新随机 PG schema，显式运行迁移 001/002，所有 Worker 共用这个 schema。schema 在 Worker 和连接池关闭后移除；Redis 使用随机前缀与比较释放，不 flush 服务。
- 每个 Worker 一个独立 pool/client，池 min=1、max 可配置；一个进程/event loop，不使用共享 Python 执行锁。跨 Worker 的执行权依旧由正式 Redis/PG 路径裁决。
- 每个场景 N 个独立 Session，每 Session 一个 Run；输入 5 UTF-8 字节、一个声明工具、固定 Fake 脚本。报告事件数量与预算；不混入长历史、真实模型网络或动态业务工具。
- 预热使用额外 text 样本并单独报告。所有测量批次在 admission 完成后启动 Worker；场景间关闭全部 Worker 后重新启动。前一批无效时停止，避免遗留候选影响后一批。
- code_commit/working_tree_dirty 与 benchmark_source_sha256 一起保存；记录 Python、asyncpg、redis-py、PG/Redis 服务版本、平台/CPU 数，以及 Worker/池/并发/扫描/轮询/TTL/心跳/I/O 期限。离开 Git checkout 时 commit 可能为 null，不能把此类报告当成已有可复现 commit 的证据。

## 指标定义

| 指标 | 起止/计算 | 限制 |
| --- | --- | --- |
| completed_runs_per_second | 完成 Run 数 / Worker 启动到全部持久终态观察的秒数 | 不含 admission、warmup、最终只读核验和关闭耗时；包含启动与调度开销 |
| completion_latency | 每次 submit 前到 Worker 观察该 Run 持久终态 | 包含 admission、队列等待、轮询、模型脚本、恢复；与吞吐分母口径不同 |
| recovery_latency | 故障实际注入到 Worker 观察持久终态 | 包含清理/退避/重建/续跑；不含自然 TTL 等待，text 为零样本/null |
| conflict_rate | busy / (busy + claimed) | 是协调获取冲突观测，包括 Redis 占用、PG 拒绝；不是 SQL CAS 冲突率 |
| failure categories | retry/scan/fatal/cleanup 类型计数及非完成状态 | 只保存类型/稳定码，注入故障数量单独报告，不混同失败 Run 数 |
| cross_worker_recoveries | 一个 Run 的 owner 序列出现多个不同 owner | 如实记录；任务也可能在原 Worker 新 attempt 恢复，不强制伪造迁移 |

分位数采用 nearest-rank：排序后取 ceil(p*N) 项，p 为 0.50/0.95/0.99；同时提供 count/min/max。空样本返回 null，不插值、不补零。运行中的异常、系统调度、Windows 时钟分辨率、Docker 虚拟化和同机资源竞争都会影响结果；比较实验需固定这些条件并多次运行。

## 正确性和失败

每个样本保存 Run/Session ID、终态、attempt、epoch、派发/模型/工具次数、prompt/unknown/event 数及 owner 序列；读取持久历史验证，而非只统计 EventSink 的“成功”通知。最终消息场景必须只有一次模型请求，工具场景必须只执行 done 调用并对 pending 生成带 recovery provenance 的 unknown。

CLI status=valid 需要全部指定场景、样本完成且通过核验，没有超时或关闭失败；不满足则 invalid 并非零退出。未完成样本保留在报告，不计算伪造的完成/恢复延迟。依赖/连接异常为 aborted，人工中断为 cancelled，同样非零；这些报告保存设置与安全错误类型，不输出原异常中的连接信息。写报告本身失败直接由命令退出，不会留下半份 JSON。

当前故障会由正式 LeaseHeartbeat 比较释放许可。它适合比较检查点处理、候选调度和配置重建开销，**不能推导真实进程死亡、网络分区、Redis HA failover 的恢复 SLO**。真实 kill/restart 的正确性由[第 13 节矩阵](../design/phase-1-recovery-fault-matrix.md)单独验证，PG fencing 也不能撤销外部工具副作用。

## 保存与复核

建议从干净提交执行 `python scripts/distributed_dev.py benchmark --samples 50 --output .tmp/recovery-50.json`，检查 status、metadata、settings、各组样本数和 correctness，再比较吞吐/分位数。报告和 trace 保存在被忽略的 .tmp；需要共享时显式导出该 JSON。不要提交原始运行数据或把一次小样本结果包装成稳定生产性能。
