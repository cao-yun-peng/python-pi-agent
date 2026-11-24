# Phase 1 第 13 节故障矩阵与阶段 7 验收证据

对应[开发规格第 13 节](phase-1-distributed-session-storage.md#13-测试矩阵)。测试代码是可执行事实来源，以下列出定位入口，避免把已覆盖的旧契约重复实现为一套恢复框架。除显式服务测试外，均使用 FakeProvider、端口替身与手动时钟，不访问付费模型。

## 13.1 默认离线矩阵

下表 `W` 指 [test_recovery_worker.py](../../tests/test_recovery_worker.py)，`R` 指 [test_distributed_runtime.py](../../tests/test_distributed_runtime.py)，`H` 指 [test_lease_heartbeat.py](../../tests/test_lease_heartbeat.py)。每行测试以 `test_` 开头，可直接用 unittest 模块/类/方法定位。

| 故障点 | 可执行证据 | 必须成立的结果 |
| --- | --- | --- |
| 接受 prompt 后、写 user 前中断 | W.test_accepted_prompt_recovers_once_and_does_not_claim_twice | 原 Run；user 一次，attempt=1，无二次领取 |
| follow-up 已 delivered | W.test_delivered_follow_up_is_discovered_via_run_not_pending_control | 从 Run 发现，不依赖 pending control |
| 模型流中途崩溃 | W.test_partial_model_output_is_not_committed_and_dispatch_budget_continues | 无半条 assistant，新 attempt 连续扣派发预算 |
| assistant tool_calls 已提交 | W.test_assistant_tool_checkpoint_does_not_reexecute_any_tool | 原工具不重跑，补偿后续轮 |
| 多工具部分结果持久化 | W.test_tool_calls_and_partial_results_share_unknown_compensation | 完成结果保留，缺失结果有正确 ID/name/provenance，outcome=unknown |
| 最终 assistant 后、终态前 | W.test_final_assistant_recovers_without_inference | 原 Finish 语义保留，空 FakeProvider 无请求，Run completed |
| steer 交付与完成竞争 | W.test_steering_and_later_follow_up_survive_recovery_runtime_close；R.test_accepted_follow_up_and_steer_are_not_double_delivered | steer 有 durable 交付/取消事实，无重复，后续 Run 保留队列 |
| 接管后旧 Worker 写入 | W.test_late_old_epoch_owner_and_claim_writes_are_rejected；执行存储旧许可契约 | epoch、owner、claim 不符拒绝；无许可写拒绝 |
| Redis 超时/丢失、PG 续租失败、心跳异常 | W.test_redis_clear_and_pg_renewal_failure_stop_attempt_without_terminal；W.test_heartbeat_exception_is_observed_and_joined；H.test_redis_timeout_stops_pg_renewal_and_joins_cancelled_io；H.test_pg_timeout_and_error_cancel_work | 派发门关闭，工作/心跳 join；Run 可接管，事件可诊断 |
| 活跃 Session 与更早 follow-up | W.test_two_workers_do_not_execute_same_session_in_parallel；W.test_steering_and_later_follow_up_survive_recovery_runtime_close；R.test_out_of_order_submission_does_not_spend_an_earlier_run_attempt | 先恢复活跃 Run，后执行 FIFO，不消耗错误目标的 attempt |
| COMMIT 成功、回包丢失 | W.test_unknown_claim_and_tool_commit_query_original_without_reexecution；R.test_unknown_terminal_commit_resolves_original_result | 查原 operation；工具只执行一次 |
| 请求/attempt 上限 | W.test_request_limit_is_not_reset_by_worker；W.test_rebuild_timeout_is_bounded_and_attempt_limit_stops_retry；R.test_physical_retries_reserve_separately_and_cannot_reset_budget | 不重置预算；耗尽后 failed，不无限扫描/重建 |
| close、abort、维护压缩与失租 | W.test_repeated_close_cancellation_waits_for_owned_runtime_cleanup；W.test_close_interrupts_a_blocked_candidate_scan_without_waiting_for_ttl；W.test_close_racing_start_does_not_leave_a_pending_start_task；R.test_idle_abort_does_not_cancel_a_later_submission；R.test_maintenance_lease_loss_cancels_summary_without_committing | 关闭顺序明确，无残留任务；不提交失租摘要、不删除新 owner |
| 配置版本/权限/workspace 不匹配 | W.test_missing_and_mismatched_catalogs_fail_without_default_provider；W.test_workspace_and_permissions_rejection_is_durable_and_redacted | 无默认 Provider/工具降级，稳定 failed code，无敏感错误文本 |

编排补充证据：忙队头排除、公平继续扫描其他 Session、重建期间心跳、配置重建超时、checkpoint 引用损坏、清理失败保留成功、Builder 启停与启动失败回滚均在 W。test_graceful_shutdown_drains_or_cancels_at_fake_deadline 验证有界排空和截止取消；test_expected_scan_failure_is_observed_then_polling_recovers 验证扫描暂时故障后继续轮询。tearDown 检查手动时钟无剩余 sleep/timeout，且无 recovery/lease/agent 后台任务。

## 13.2 显式真实服务矩阵

| 要求 | 可执行证据 |
| --- | --- |
| 两个独立 Worker 竞争同一 Session | [test_postgres_recovery.py](../../tests/test_postgres_recovery.py)::test_two_pools_and_clients_contend_with_one_physical_execution：两个独立 pool/client、同步扫描屏障；一个 completed、一个 busy，物理模型请求一次 |
| 进程终止与接管 | 同模块 test_hard_process_exit_mid_stream_is_taken_over_without_partial_message：真实子进程 stdout 屏障后 kill；原 Run/单 prompt，epoch/attempt/dispatches=2，无半条消息 |
| Redis 清空与旧许可拒绝 | 同模块 test_redis_clear_cannot_bypass_pg_then_other_worker_takes_over：只删除自有 key；PG 有效时 B 不能领取，失租后接管，旧 owner/claim/epoch 写拒绝 |
| Redis 服务真实重启不回退 PG epoch | 同模块 test_real_redis_restart_preserves_pg_epoch_and_stops_old_worker：额外显式开关，核对专用 Docker 容器标签、名称、固定回环端口；关闭该测试服务持久化后重启，重连后验证 PG 仍拒绝 B，随后安全接管 |
| 真实工具提交回包丢失 | 同模块 test_unknown_real_tool_commit_is_queried_without_tool_reexecution：PG 实际 COMMIT 后丢回复，查询回执，工具一次 |
| 忙队头不阻塞其他 Session | 同模块 test_excluded_busy_head_does_not_starve_ready_session：SQL 在 LIMIT 前应用 exclude |
| 十个相同 expected_version 写入仅一个成功 | [test_postgres_session_store.py](../../tests/test_postgres_session_store.py)::test_ten_independent_writers_only_one_same_version_succeeds |
| PG claim、心跳与接管锁顺序 | [test_postgres_execution.py](../../tests/test_postgres_execution.py)::test_concurrent_claim_and_same_operation_are_serialized_in_pg；[test_redis_pg_heartbeat.py](../../tests/test_redis_pg_heartbeat.py)::test_redis_key_loss_does_not_bypass_pg_then_heartbeat_stops_old_worker |
| 原子回滚与取消 | test_postgres_execution.test_invalid_reference_and_driver_failure_rollback_entire_step、test_cancellation_after_run_progress_before_session_insert_rolls_back；test_postgres_session_store.test_mid_batch_driver_failure_rolls_back_new_session_and_events |
| 含控制的 fork 与共享 Store 契约 | test_postgres_session_store.test_fork_rebuilds_terminal_and_pending_projections、test_concurrent_forks_do_not_overwrite_target；同模块继承共享 SessionStoreContract；[test_session_store_contracts.py](../../tests/test_session_store_contracts.py) 验证 Memory/JSONL |
| 真实唯一约束与超时映射 | test_postgres_session_store.test_real_event_unique_violation_maps_conflict_and_rolls_back、test_lock_timeout_is_storage_error_and_cancel_rolls_back |
| steer 与完成真实竞争 | test_postgres_execution.test_finalization_serializes_with_accepted_steering |
| 维护压缩与运行互斥 | [test_postgres_runtime.py](../../tests/test_postgres_runtime.py)::test_maintenance_cannot_bypass_pg_when_redis_key_is_lost |

基础集成仅在同时设置 PI_AGENT_POSTGRES_TEST_DSN、PI_AGENT_REDIS_TEST_URL 时执行；其他存储测试沿用各自显式开关。真实 Redis 重启额外要求 PI_AGENT_REDIS_TEST_RESTART_CONTAINER：接受阶段 7 的专用任务容器，或阶段 8 Compose 同时标记 distributed-dev/test-redis-restart、service=redis 且 project 以 pi-agent- 开头的专用容器。两者都要求 HostConfig 显式固定回环端口且与测试 URL 完全一致。此开关会关闭该测试 Redis 的 RDB/AOF 并重启，只能用于本次独占的服务，不能与其他测试并发运行。阶段 8 的 `distributed_dev.py test --restart-redis` 自动解析对应容器身份。

子进程测试用环境变量传 DSN，不把凭据写命令行；服务端到期注入只修改隔离 schema 与随机 Redis 前缀。容器重启等待实际 PING 条件并有截止时间，不靠固定 TTL sleep。默认单测不执行 Docker、不访问服务，真实重启不代表覆盖 Redis HA 复制/分区拓扑。

## 本地验收记录

环境为 Windows、Python 3.12、PostgreSQL 16、Redis 7、asyncpg 0.32、redis-py 8.2；全部 FakeProvider。最终测试数量、静态检查与示例结果在本次提交对应 ADR 0055 中记录。统一 Compose 与条件化 benchmark 已由阶段 8 提供，见教程 54 与测量方法；当前服务验证为本地 Windows/Python 3.12，不声称已运行 CI 多版本服务矩阵，不以本地正确性测试推算生产性能或外部副作用 exactly-once。
