# 第 54 章 一套环境完成故障验收与 benchmark

本章把已有能力变成可重复操作的开发流程，并学习如何解释恢复性能。正确性决定数据能否使用，吞吐和延迟还必须绑定数据规模、并发和故障条件。

## 一条命令验收

先启动 Docker，按 README 激活仓库虚拟环境，在 python-pi-agent 目录安装可选依赖并运行：

```sh
python -m pip install -e ".[postgres,redis,dev]"
python scripts/distributed_dev.py verify --samples 20 --restart-redis
```

Windows PowerShell 和 Linux 使用同一个 Python 入口。verify 启动服务并等待健康，显式迁移，运行存储/租约/Runtime/接管/benchmark 集成，运行示例 56 两种中断，再生成 `.tmp/recovery-benchmark.json`。第一次需要本地已有或可拉取 postgres:16、redis:7-alpine 镜像；入口不安装 Python 依赖，不访问真实模型。

默认项目 pi-agent-dev，PG 127.0.0.1:55432、Redis 127.0.0.1:56379。可通过每条命令的 --project/--pg-port/--redis-port，或 PI_AGENT_DEV_PROJECT/PI_AGENT_DEV_PG_PORT/PI_AGENT_DEV_REDIS_PORT 覆盖。需要隔离第二套环境时同时改项目和端口；后续操作保持相同参数。

--restart-redis 允许集成测试停止并重启这个专用 Redis，测试会先核对 Compose 专用标签和固定回环端口。不加该参数则跳过真实重启项，仍覆盖删除自有 key 的状态丢失。不要把这个独占测试服务用于其他应用。默认 discover 不开启服务；独立集成仍要求 PI_AGENT_POSTGRES_TEST_DSN、PI_AGENT_REDIS_TEST_URL。

## 分步操作和关闭

```sh
python scripts/distributed_dev.py up
python scripts/distributed_dev.py status
python scripts/distributed_dev.py migrate
python scripts/distributed_dev.py test
python scripts/distributed_dev.py demo
python scripts/distributed_dev.py benchmark --samples 50 --output .tmp/recovery-50.json
python scripts/distributed_dev.py down
```

up 只等待健康，不隐式迁移。down 停止本项目服务并保留 PG named volume；再次 up 可以查看旧历史。需要丢弃这个开发项目的数据时，明确执行 `docker compose -f dev/distributed/compose.yaml -p pi-agent-dev down --volumes`。benchmark 的随机 schema 每次运行结束都会自行删除，默认开发 schema 与其他项目不受影响。

任一步失败返回非零，verify 停止后续步骤并保留环境供排查。阶段 7 故障矩阵仍是正确性入口；benchmark 失败报告不会宣称性能测量有效。

## 单独运行正式 benchmark

开发脚本配置了环境变量并调用正式 app 模块。使用已有专用实例时，也可以显式设置两个 TEST 端点，再运行[示例 57](../examples/57_recovery_benchmark.py)或：

```sh
python -m pi_agent_py.application.benchmark_cli --samples 50 --warmup 2 --workers 2 --pool-size 4 --concurrency 2 --output .tmp/recovery-50.json
```

可通过重复 --scenario 选择 text/partial_stream/final_assistant/tool_tail；--poll-interval、--scan-limit、--ttl、--heartbeat-interval、--timeout 调整被测参数。设置在打开连接前校验。每个 Worker 使用独立 pool/client；默认两个 Worker 共享一个 Python event loop，跨进程死亡验证由真实故障测试承担。

四个场景各有独立批次和样本数。每个样本只接受一个 Run，固定短 prompt 和一个 demo 工具。warmup 是额外 text 样本，单独记录，不进入四组样本数或吞吐。

| 场景 | 中断点 | 正确性门槛 |
| --- | --- | --- |
| text | 无 | prompt=1、attempt=1、模型派发=1 |
| partial_stream | 半条流后抛中断 | 半条消息不持久化，attempt=2、总模型派发=2 |
| final_assistant | assistant 正式提交后 | attempt=2、总模型派发=1，不再次推理 |
| tool_tail | 第一个工具结果正式提交后 | 原工具只执行一次，另一个补 unknown，ID/name/provenance 完整，总模型派发=2 |

工具场景的 unknown 只表示缺少持久结果，不证明外部执行失败；正式恢复逻辑不重跑原工具。故障注入只包装正式 writer，所有消息和终态仍由生产状态机提交。

## 阅读报告与 trace

报告的 metadata 保存代码 commit、dirty 状态、源码摘要、Python/驱动/服务版本和平台；settings 保存池、Worker、并发、轮询、TTL、心跳和预算；dataset 描述输入规模。工作区干净时运行最便于复核；源码摘要用于区分未提交的 benchmark 版本，不能替代完整源码归档。

每组同时包含 correct/completed、吞吐、完成/恢复延迟 p50/p95/p99、冲突率、尝试错误类型、非完成状态、原样本和 trace。无恢复样本时分位数是 null，不是零。nearest-rank 的 p99 在小样本中往往就是最大值；必须结合 count，不能把本地一次全通过当作生产成功率或 SLO。

示例 trace 顺序为 recovery_started → recovery_rebuilding → fault_injected → recovery_failed → recovery_started → recovery_completed。样本保存 Run/Session、epoch、attempt、模型/工具次数和 owner 序列，用于对照数据库正确性。trace 只复制安全关联字段，不含 prompt、工具参数、DSN、nonce 或 provider state。

恢复延迟从注入中断到 Worker 观察到持久终态，包含取消/比较清理、轮询、领取、重建和续跑。当前故障是会释放许可的协作中断，**不包含进程死亡后的 TTL 等待**；实际进程硬终止正确性见第 53 章。吞吐从 Worker 启动计到全部终态观察，排除 admission 和 warmup；完成延迟包含 admission 与排队。各口径和失败解释详见[测量方法](../docs/benchmarks/recovery-methodology.md)。

## 离线验证与掌握标准

```sh
cd tests
python -B -m unittest test_recovery_benchmark test_recovery_worker test_api -v
```

离线统计、超时与取消使用手动时钟/端口替身；Windows 受限沙箱按照 AGENTS.md 直接使用获准的沙箱外离线命令。学习者应能改变 pool/并发/轮询后保留完整报告，解释冲突率分母与延迟起止点，复现配置失败/超时非零退出，证明终态消息与 unknown 工具恢复没有额外推理或原工具重跑。

Pi 映射沿用固定 commit `56b25ff4ebbd8a119ef9185447c7b2416059dbcb` 的 `packages/coding-agent/src/core/agent-session.ts::AgentSession` 职责，本章没有对应的上游分布式测量 API。DeepSeek Harness 对照能力接缝与版本化执行证据；省略生产 HA、真实模型成本/质量评测、网络分区和多进程负载发生器。
