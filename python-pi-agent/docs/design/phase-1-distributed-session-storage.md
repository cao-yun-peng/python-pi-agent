# Phase 1 PostgreSQL Redis 分布式会话存储与恢复开发规格

> 状态：待实现的开发规格；不代表当前项目已支持分布式执行。
> 日期：2026-11-23。
> 源码基线：`dfcf7de`；适用包：`python-pi-agent/src/pi_agent_py`。
> 决策级别：本文确定本阶段的建议实现路线；实现前将重要决定写入新 ADR，并明确修订 ADR 0017 的恢复范围。

> 开发进度：Step 1 已加入执行值对象、端口、纯状态机与契约测试，见 [ADR 0049](../adr/0049-execution-journal-contracts.md) 和[教程 47](../../tutorials/47-execution-journal-contracts.md)。当前只生成可持久化计划，PG/Redis 与运行时接线尚未实现。领域类型位于 `execution_domain.py`，纯转换位于 `storage/execution_transitions.py`；绑定 writer 的完整操作摘要与纯命令摘要须分开。

本阶段解决两个问题：将会话事件持久化到 PostgreSQL；在执行进程崩溃后，由其他 Worker 安全取得执行权，继续未完成的 Run。PostgreSQL 保存会话历史、Run 检查点和执行权代次，Redis 提供执行租约协调。租约过期不等于工具副作用已经停止，恢复始终保留“外部执行结果未知”的语义。

## 1 目标与范围

### 1.1 必须交付

- 保留 `InMemorySessionStore`、`JsonlSessionStore` 和现有六方法 `SessionStore` 协议；新增 `PostgresSessionStore`。
- 默认本地运行、离线评测和教学示例不依赖数据库或 Redis。
- 分布式模式支持普通 prompt 和 follow-up 的持久接收、FIFO 调度、执行互斥、心跳、失租停止和崩溃接管。
- 同一 Session 同时最多存在一个具有有效 PG 执行权的执行者；旧执行者不能污染接管后的会话日志。
- 恢复不重复提交已有 prompt，不重跑已经确定完成的 Run，不自动重试结果未知的工具调用。
- 所有重要执行决定可从会话日志与执行日志重建；心跳作为独立、短期有效的协调状态管理。
- 单测离线且确定；真实 PG/Redis 测试显式开启，不调用付费模型。

### 1.2 本阶段边界

- 不承诺 LLM 请求、工具副作用或跨系统操作 exactly-once。
- 不实现跨 Worker 的实时 steer/abort 路由、远程 RunHandle 事件订阅及 Redis Pub/Sub。非持有者调用 steer/abort 必须明确拒绝或返回归属信息，不能假装成功。prompt/follow-up 可跨 Worker 持久提交。
- 不自动恢复等待人工审批的挂起交互。本阶段审批必须可取消；执行者丢失后，结果未知的调用按工具恢复规则处理，后续新调用重新审批。
- 不恢复进程内插件对象、终端进程或容器内存。需要持久 workspace 的配置必须提供可重新定位的 workspace 标识。
- 不引入读副本、分库、日志截断和增量快照加载。`load()` 首版完整 replay。
- 不直接修改上游 `pi/`，不提前创建 Redis Cluster、Sentinel 或生产部署系统。

## 2 当前实现与本阶段变化

| 当前事实 | 对开发的约束 |
| --- | --- |
| `SessionStore` 有 load/read_events/append/compact/record_control/fork 六个方法 | PG 适配器保持相同签名；分布式执行权通过新增端口和绑定 writer 传递 |
| `control_delivered` 同时提交 user message 并移除 pending control | delivered 不能充当 Run 完成状态 |
| 普通 prompt 入队不持久化 control | 分布式模式必须独立持久接收普通 Run |
| `runtime/session.py` 已补齐尾部缺失 tool result | 抽取或接入现有机制，不另建一套恢复算法 |
| `_SessionWriter` 负责消息、delivery 和压缩提交 | 执行权保护必须覆盖 writer，而不仅是 `runtime/control.py` |
| `SessionSnapshot.version` 是 durable event 数量 | 执行日志、心跳不占用会话 sequence；独立维护执行 revision |
| JSONL 当前只接受 schema v6 | 抽取 codec 时保持字节语义之外的完整数据与错误契约，不添加隐式旧格式兼容 |
| fork 复制 control ID 和 claim | SQL 投影使用会话内复合主键；执行权和 Run 调度记录不随历史复制 |

源码入口：`ports.py:SessionStore`、`storage/transitions.py`、`storage/_writer.py`、`runtime/control.py:_SessionControl`、`runtime/session.py:AgentSession`、`application/runtime.py:AgentRuntime`。现有规则见 ADR 0005、0007、0016、0017、0028。

`prepare_session_append()` 当前不主动调用 `find_unresolved_tool_calls()`；`prepare_session_control()` 也不负责验证所有者和租约。开发者必须分别落实历史合法性、控制迁移和执行权校验，不能把“复用 transitions”当作全部约束已经存在的证据。

## 3 核心术语与不变量

| 术语 | 定义 |
| --- | --- |
| Session | 一个有序的模型历史及其控制指令集合 |
| Run | 一次被接受的执行任务，保留稳定 run_id 和原始 prompt |
| Control | steer/follow-up 的交付意图；状态结束不表示 Run 结束 |
| Attempt | 某个 Worker 对 Run 的一次执行尝试；接管增加 attempt，不更换 run_id |
| ExecutionGrant | PG 颁发的许可，包含 session_id、purpose(run/maintenance)、可选 run_id、owner_id、claim_id、epoch 和有效期 |
| Checkpoint | 下一步安全执行位置，以及恢复所需的请求编号、预算、finish 信息与消息引用 |
| Operation ID | 一个逻辑写操作的稳定标识，用于识别提交结果未知时的重复请求 |

必须持续满足：

1. PG 是执行权最终裁决者。Redis 获取成功后，在 PG 接管事务提交前不得执行模型、工具或历史修复。
2. epoch 在 PG 中持久单调递增，不因 Redis 重启、过期或清空而回退；普通提交不能自行提升 epoch。
3. 执行者提交必须匹配当前 epoch、owner、claim、许可用途和有效期；Run 写入还须匹配 active_run。维护许可只能执行已声明的维护操作。已配置分布式模式的 Session 不允许无许可执行写入。
4. 外部任务入队不需要执行许可，但必须经过输入授权与幂等校验；入队不能伪装成模型/工具提交。
5. 会话事件、相关控制投影、执行检查点及操作去重记录在一个 PG 事务中更新。
6. 一次工具结果只能关闭其对应的未决调用。恢复补偿也是普通 durable message，保留 run/provenance 关联。
7. Run 终态不可自动重新入队；用户主动重试生成新 Run，并记录因果关系。
8. 心跳不改变 Session.version，不把协调有效期冒充事件日志投影。

## 4 模块和端口

继续保持单个 Python 包，新增模块按职责放置，不再拆发行包。

| 模块 | 职责 |
| --- | --- |
| `storage/_codec.py` | 共享事件、消息、control、compaction、provenance 严格编解码 |
| `storage/postgres.py` | 实现六方法 SessionStore，调用共享 transitions |
| `storage/execution.py` | PG 执行存储、绑定执行 writer、操作去重与原子检查点提交 |
| `storage/redis_lease.py` | Redis 租约协调；不产生最终 fencing epoch |
| `storage/migrations/` | 有版本的 SQL 迁移与资源文件 |
| `runtime/recovery.py` | 面向端口的候选扫描、接管和恢复调度 |
| `runtime/session.py`、`control.py`、`storage/_writer.py` | 注入执行组件、复用修复路径、记录安全边界 |
| `application/` | Builder/config/capability 注册及生命周期组装 |

新增领域值对象放在 domain 或其正式拆分后的领域模块，协议放在 ports，禁止 domain 依赖驱动 SDK。

### 4.1 SessionLeaseManager

提供 `acquire(session_id, owner_id, claim_id, ttl)`、`renew(lease, ttl)`、`release(lease)`。返回不可变租约值，至少包含 session、owner、claim、唯一随机 nonce 和有效期；时间、TTL 必须有限且为正。

`acquire` 返回“取得”或“占用”；驱动错误抛稳定领域错误，不把异常当成占用。`renew` 区分有效、失租和基础设施异常。TTL 测试使用注入 clock/scheduler。

同时提供实例级 `InMemoryLeaseManager` 测试替身。它不提供跨进程保障，不得在分布式 profile 中静默替代 Redis。

### 4.2 SessionExecutionStore

新增执行存储端口，不给旧 `SessionStore` 增加可选 fencing 参数。下列为必须提供的能力名与语义，最终类型签名在 Step 1 通过契约测试固定：

| 能力 | 输入和输出 | 原子要求 |
| --- | --- | --- |
| `submit_run` | Submission，含 session/run、prompt、delivery、幂等键、恢复配置引用；返回持久 receipt | 普通 prompt 和 follow-up 均先持久接受；follow-up 同事务关联原有 control enqueue |
| `list_candidates` | limit；返回去重的 Session 候选 | 仅发现，不宣称已领取；使用 PG 时间判断过期 |
| `claim_next` | Session、owner/claim、协调租约身份；返回 grant 与 RunRecord | 锁 Session，选择未终态活跃 Run 或最早 queued Run；推进 epoch、attempt，写执行日志 |
| `renew_grant` | grant、TTL；返回更新许可 | 精确校验身份和旧有效期；过期许可不能通过续租复活 |
| `claim_maintenance/release_grant` | Session、维护种类、owner/claim；返回维护 grant，或比较释放既有 grant | 与 Run 共用 Session 行锁、epoch 和有效期；持有有效 Run 许可或存在待恢复 active_run 时不能领取维护许可 |
| `load_execution` | session/run；返回 RunRecord、checkpoint 和事实引用 | 可供离线检查与恢复 |
| `bind_writer` | grant；返回作用域内执行 writer | 许可不可被其他 Run 的全局可变字段替换 |
| `commit_step` | grant、operation_id、typed mutation、checkpoint、expected versions | 校验执行权；原子提交会话变化、执行检查点、投影和去重结果 |
| `finish_run` | grant、operation_id、终态及结果引用 | 写不可变终态，清除 active_run；不得丢失最终消息或接受中的 steer |
| `resolve_operation` | session、operation_id | 查询逻辑提交结果；不能通过重新调用模型或工具来确认 |

mutation 使用有限 tagged union，覆盖消息批次、控制交付、补偿和压缩，不接收 SQL 字符串、任意 callback 或无边界 `Any`。公共签名、错误类型和取消语义都需要类型检查。

绑定 writer 可复用旧 `_SessionWriter` 的流程，但必须把实际写入委托给 `commit_step`。基础 PG Store 可用于本地单执行者模式；进入分布式模式后，直接调用无许可 append/compact 等执行写操作必须失败。配置模式持久保存在 sessions，不能在重启时偷偷降级。

人工导入、分叉属于显式管理操作；不能提供一个通用 `skip_fencing=True` 绕过执行写保护。

## 5 Run 状态和检查点

### 5.1 状态

执行状态使用 `queued -> running -> completed | failed | cancelled`。Worker 丢失不直接写 cancelled；租约过期的 running Run 可由新 attempt 接管。恢复失败达到上限时写 failed 并记录安全错误码。当前公开 SessionState 仍表示本地 controller 的 idle/running/aborting/settled，不复用为数据库 Run 状态。

session_controls 继续保持 queued/claimed/delivered/cancelled；不新增 `delivery='prompt'` 来绕过现有领域类型。普通 prompt 的持久接受由 RunRecord 表达。

### 5.2 检查点和恢复动作

| 检查点 | 必须持久保存 | 接管后动作 |
| --- | --- | --- |
| `before_prompt` | 原 prompt、提交配置、当前 run_id | 完成必要的准备工作，然后原子提交一次 user message；follow-up 复用 control_delivered |
| `awaiting_model` | prompt 已提交的事实引用、next_request_number、已用预算 | 从 committed history 发起这一轮请求，不再追加 prompt |
| `awaiting_tools` | 已提交 assistant 引用、完整 Finish 元数据、工具调用列表 | 为缺失结果补偿；已提交结果保持不变 |
| `awaiting_boundary` | assistant/工具批次已提交、Finish、当前请求编号、已消费 steer 引用 | 执行安全边界决策：续轮、完成或达到上限 |
| terminal | 最终消息引用或错误/取消原因 | 返回终态，不调模型，不重新入队 |

assistant 消息及其 Finish/checkpoint 同事务提交。最后一个 tool result 与进入 awaiting_boundary 同事务提交。工具失败、truncation、请求上限沿用当前 loop 语义，不能仅凭“assistant 没有 tool_calls”猜测最终结果。

当前 loop 的完整 assistant 已在 Finish 后提交，但安全边界决策和 run_completed 是后续步骤。实现中增加受限的持久边界接口，将这些决定交给执行存储；本地路径沿用原行为。不要把 PG SQL 或 Redis SDK 放入 loop。

安全边界将“交付 steer 并进入下一请求”或“关闭接受窗口并进入终态”串行化。针对原 Run、已接受且尚未交付的 steer 必须先被明确交付或按既有孤立 steer 规则取消，不能静默丢弃。`finish_run` 必须校验边界版本，拒绝基于过时观察完成 Run。

崩溃在最终 assistant 提交后、finish_run 前：从 awaiting_boundary 恢复，若没有可交付 steer 且无需工具续轮，直接写完成标记。不得再次请求模型。崩溃在两个续轮操作之间：依赖 checkpoint 与 operation_id 确认已提交进度。

request_number 在接管后继续增长，不从 1 重置。总逻辑请求上限、实际模型派发次数上限和恢复 attempt 上限分别限制；每次网络派发前持久预留一次派发预算，即使模型流中途崩溃也不能无限免费重置预算。模型请求与用量按尝试记录，流中断导致的远端真实计费可能未知，报告不得假装精确可重放。

## 6 PostgreSQL 数据模型

推荐 PostgreSQL 16 开发环境，兼容目标从 15 起。首版使用外部注入的 asyncpg 异步池；SQL 占位符统一 `$1`。依赖放入独立 extra，普通安装不要求驱动。以下为表的必须字段和约束，Step 3 将其落实为完整 SQL，而非直接照搬原规格的 DDL。

| 表 | 必须字段与约束 |
| --- | --- |
| `schema_migrations` | version 主键、checksum、applied_at；并发迁移锁 |
| `sessions` | session_id PK；version BIGINT >= 0；last_compaction_seq；execution_mode(local/distributed)；epoch BIGINT >= 0；lease_purpose(run/maintenance)、owner_id、claim_id、active_run_id、lease_expires_at TIMESTAMPTZ；parent_session_id、forked_at_version；created/updated_at |
| `session_events` | (session_id, sequence BIGINT) PK；event_id UNIQUE；kind、run/turn、schema_version、payload JSONB、provenance JSONB；session FK |
| `session_controls` | (session_id, control_id) PK；delivery 仅 steer/follow_up；原 prompt/run/target/cause；state、claim/owner、last_sequence、cancel_reason；字段来自 durable control log |
| `session_runs` | (session_id, run_id) PK；queue_order；delivery(prompt/follow_up)、control_id；submission_key；state、attempt、execution_revision；checkpoint JSONB及版本；result_event_id；recovery_spec_ref/hash；created/updated_at |
| `run_events` | (session_id, run_id, revision) PK；kind、operation_id、epoch、typed payload、schema_version、created_at；记录接受、领取、进度、终态及恢复决定 |
| `session_operations` | (session_id, operation_id) PK；请求摘要、已提交 version/revision 与结果引用；包含多事件批次的边界 |

额外约束：

- lease_purpose、owner、claim、lease_expires_at 一起存在或一起为空；epoch 保留最后值，即使释放许可也不归零。Run 许可必须关联 active_run，维护许可要求 active_run 为空。过期 active_run 保留，直到恢复或确定终态，不能仅因有效期到达而丢掉归属任务。
- active_run_id 必须指向本 Session 的 Run。使用可延迟外键或同事务约束实现，写明插入顺序。
- 同一 Session 最多一个 running Run；可用部分唯一索引兜底。
- (session_id, submission_key) UNIQUE。相同键不同内容为校验错误，不得返回别人的提交结果。
- queue_order 在 Session 行锁内分配，保持会话内稳定顺序；不能仅按 created_at 排序。
- session_controls.last_sequence 必须引用对应 Session 的事件；投影更新零行视为一致性错误，回滚事务。
- 编码 schema version 与数据库迁移版本分别管理。JSONB 中保留完整消息字段，包括 thinking、provider_state、structured_content。
- payload 由 kind 唯一确定结构，严格解码；禁止只提取 content 而丢失工具身份或 opaque provider state。
- Phase 1 不提供删除会话接口；外键删除策略不应被误当成已授权的数据清理功能。

索引至少包括：Session 活跃执行权到期索引、Run queued 顺序索引、Run 非终态索引、事件 run/sequence 索引、control pending 索引。首版先正确再通过 EXPLAIN 和基准调整，不能承诺固定 O(K) 扫描复杂度。

迁移使用版本、checksum 和事务，启动不隐式执行。提供显式 migrate 命令；ensure_schema 仅作开发辅助。SQL 文件加入 setuptools package-data，并验证安装后的 wheel 中确实可读取。

## 7 PG 事务与并发语义

### 7.1 普通 SessionStore

append/compact/record_control：校验边界输入 → 开事务 → 幂等建立 Session 行 → 锁 Session 行 → 校验 expected_version 与模式 → 读取完整事件 → 共享 prepare_* → 写新事件及相关投影 → 推进 version → 提交。

必须复用 `prepare_session_append`、`prepare_session_compaction`、`prepare_session_control`、`replay_session`、`fork_session_events`。新增执行状态也应通过纯函数计算计划，再由适配器原子保存。已有历史检查和新增约束分别测试，禁止 SQL 重写一份消息状态机。

`load/read_events` 不因读取未知 ID 创建 Session。首版用一次有序事件查询构造自洽历史；若同时读取元数据或执行记录，必须使用同一一致性读事务并验证版本，不能拼接不同提交时刻的状态。

### 7.2 执行者提交

统一锁顺序：Session 行 → Run 行 → 相关 control 行。所有路径遵守，包括入队、心跳、接管、补偿、手动压缩和结束。

事务内执行顺序：

1. 查找 operation_id；已提交且请求摘要一致则返回原结果，不再写入。此路径不能产生副作用。
2. 获取上述行锁，在取得 Session 锁之后获取 PG 当前时间；不要使用事务开始时就固定的时间来校验长时间等待后的租约。
3. 校验 epoch、owner、claim、许可用途、有效期及 expected session/execution versions；Run 写入校验 active_run，维护写入校验维护操作白名单。
4. 加载必要历史，生成纯转换计划，校验消息尾部和 checkpoint 一致性。
5. 原子写会话事件、control 投影、run_events、session_runs 和 operation 记录。
6. 提交后才更新运行内存、发 durable observer 通知或返回成功。

许可校验的线性化点为取得行锁后的检查；事务必须短且有 lock/statement timeout。先取得许可锁并校验通过的事务可能完成于租约到期之后，B 的接管会等待该事务；B 接管提交后，A 后续事务必须失败。不得在事务内执行模型、工具或 Redis 网络请求。

`expected_version` 冲突不能通过盲目加载新版本后重放所有行为解决。只有经过证明的纯控制写或确定尚未提交的操作可以重算；模型与工具执行结果未知时走查询确认。

### 7.3 候选发现与接管

候选扫描包含 queued Run，以及 active_run 非终态且 PG lease 已过期的 Session，返回 Session 去重结果。扫描不是领取证明。

首版不要求候选查询使用 SKIP LOCKED；最终执行权由短事务 `claim_next` 保证。若后续加入 SKIP LOCKED，只能作为事务内批量预留优化，不能在函数返回后继续假设行锁有效，也不能先锁 control 再锁 Session。

在同一 Session 内，优先恢复已有 running Run，再取 queue_order 最小的 queued Run。发现占用、冲突或 Redis 获取失败时使用有界退避和公平轮询，不把候选全部标记失败。

### 7.4 提交结果未知与错误

| 情况 | 行为 |
| --- | --- |
| expected version 不匹配、旧 epoch、fork 目标存在 | SessionConflictError；可新增其子类区分失租 |
| 输入、幂等键内容不匹配 | SessionValidationError |
| 历史损坏、投影与日志矛盾 | SessionReplayError 或明确的一致性领域错误 |
| 驱动、连接、锁超时 | SessionStorageError，保留异常链 |
| COMMIT 后连接断开，结果无法确认 | 新增 SessionCommitUnknownError(SessionStorageError)，携带安全 operation_id |
| 调用取消 | 保持 CancelledError 传播；若已发送提交，保留操作身份供恢复查询 |

只把已知并发唯一约束映射为 conflict，不把所有 SQLSTATE 23505 笼统当成正常竞争。死锁/序列化失败仅能对具有稳定 operation_id 的数据库事务做有界重试，不包住工具和模型调用重试。

## 8 Redis 协调与心跳

使用单个 key：`pi:lease:session:{encoded_session_id}`。花括号为实际 Redis hash tag，编码必须稳定且避免用户 ID 中的花括号改变 slot。value 使用不可歧义的序列化形式，包含 owner、claim、随机 nonce；不得简单用冒号分割任意 ID。

获取可使用单条 `SET key value NX PX ttl_ms`。续期与释放必须 Lua 原子比较完整 value 后 PEXPIRE/DEL，不得 Python GET 后再写。脚本本身不发放 PG epoch，Redis 不保存需长期可靠的 fence counter。

### 8.1 取得执行权

1. 生成新的 claim 和 nonce，获取 Redis 协调租约。
2. 调用 PG claim_next；若 PG 有未过期持有者，领取失败并比较删除自己的 Redis lease。
3. PG 在 Session 行锁内递增 epoch、写新 owner/claim/有效期、attempt 和领取事件；事务提交后返回 grant。
4. 若 PG 提交结果未知，通过 claim/operation 身份查询，不立即执行或再次领取。
5. 确认 grant 后启动心跳，再进入恢复或运行。领取流程有时间预算；获得 PG grant 时若 Redis 有效期已不足，先确认续期，不能带着过期协调身份开始工具调用。

Redis 被清空时 B 可能先取得协调 key，但仍会被 PG 有效许可拦住。PG 到期后 B 才能取得新 epoch。系统安全性依赖 PG 裁决，不依赖 Redis 计数或主从互斥不丢失。

### 8.2 续期和失效

配置 `ttl_seconds`、`heartbeat_interval`、单次 Redis/PG I/O deadline 和本地安全余量。默认可取 TTL 30 秒、间隔 10 秒，但必须校验 interval、deadline 和余量能够在 TTL 内留出检测时间。

心跳先确认 Redis 仍属于自己，再以当前 grant 续 PG 许可。任一步明确失租或异常超过本轮允许预算，采取 fail closed：关闭新模型/工具派发，取消 run/手动压缩任务，等待可取消任务退出，停止续期。后台异常必须被监督者取出和报告。

PG 使用数据库时间判断有效期，客户端调度使用 monotonic clock。主机 wall clock 只用于显示，不作为跨 Worker 接管依据。ControlClaim 的原 lease_expires_at 是交付 claim 的 durable 事实，不由心跳静默改写投影。

结束顺序：持久终态或停止执行 → 停止并 await 心跳 → 按完整 grant 比较清理 PG 归属 → 按完整 value 释放 Redis。任何清理都不能删除新 owner 的许可。清理失败记录诊断，租约自然过期，不把已持久成功改报成未执行。

## 9 恢复流程与工具边界

SessionRecoveryWorker 只依赖执行存储、租约端口、恢复调度端口和受监督的 task group。不能直接依赖 PostgresSessionStore 的私有方法，也不能复写 Agent loop。

恢复步骤：

1. 有界扫描候选，获取 Redis lease 和 PG grant。
2. 用 recovery_spec_ref/hash 解析原运行配置，验证 profile、Provider、工具契约、审批/Gate、workspace 标识及版本。
3. 在执行前加载 Run 和会话，验证 checkpoint 引用的事件存在且一致。
4. 按第 5 节恢复位置执行；已有终态直接返回，已有 prompt 不再提交。
5. 需要补偿时，调用与 AgentSession 共用的尾部检查和补偿计划，携带当前 grant 和稳定 operation_id 原子提交。
6. 将原 run_id、checkpoint 和 grant 交给内部 resume 入口。该入口不生成新 run_id，不重新 submit，不再次领取。
7. 成功或确定失败时提交终态；失租只停止当前 attempt，留待合法持有者接管。

恢复配置引用只能指向应用注册、授权且可重新组装的配置，不保存密钥或序列化 Python 对象。不兼容、缺失 workspace 或无法重建插件时，记录明确失败原因，不自动换成默认工具或放宽权限。

工具补偿沿用当前 `_INTERRUPTED_TOOL_RESULT` 的含义：执行结果未知，不得自动重试。每条补偿包含 tool_call_id、tool_name、is_error、structured_content 和 recovery provenance。已完成结果不重复生成；非尾部损坏历史直接拒绝。

首版对所有未决工具一律补偿。`side_effects='read'` 不足以证明调用可安全重放；未来自动重试还需 retry_safe/replay_safe、输入资源稳定性、幂等策略和单独测试。

PG fencing 只保护 PG 提交，不能撤销已经发出的 Shell、文件或外部网络写入。取消不能被宣传为进程一定已停止；工具后端若需要更强保证，必须自行校验 epoch、使用幂等键或隔离 workspace。补偿错误消息也不保证模型永远不会建议再次调用，因此权限与执行 Gate 仍必须有效。

## 10 分叉与手动压缩

fork 保持六方法协议：同源同目标、空前缀、越界版本、已有目标等契约与 Memory/JSONL 一致。源历史使用一致快照；目标插入和全部复制事件在一个事务内完成。

session_controls 按复制后的日志重建，复合主键允许源与目标保留相同 control_id。已有 claim 仍是复制历史，不代表目标获得了执行权。目标不复制 session_runs、run_events、operations、epoch 或活跃租约。

分布式目标首版创建为未调度的历史分支，调用方必须显式选择分支策略：取消孤立 pending control 后提交新 Run，或明确导入为新的 Run。不能仅打开目标 Session 就自动把复制的控制当成已授权新任务。若需要更改现有本地 fork 恢复行为，单独 ADR，不静默改变。

手动压缩也取得 Session 级维护许可，使用同一个 epoch 与心跳体系；不能因没有 active Run 而绕过写保护。维护许可与 Run 执行互斥，状态需明确区分，恢复中断压缩时保留旧历史并可重新发起管理操作。自动压缩沿用 Run 的 grant。

## 11 生命周期与应用入口

- Builder 显式装配可选 distributed execution 组件。未配置时所有旧构造路径保持可用。
- 分布式模式替换本地 `restore_pending_controls()/resume_pending_runs()` 的自动调度入口；从 Run journal 调度，禁止同一任务被旧 follow-up 恢复路径与 RecoveryWorker 各启动一次。steer 的失效处理仍复用共享控制转换。
- PG pool、Redis client、RecoveryWorker 的 owned/borrowed 必须显式；借用资源不由 Runtime 关闭。
- distributed profile 必须拒绝 Jsonl/Memory Store 与真实跨 Worker 调度的错误组合，不静默退回单机锁。
- RecoveryWorker 的 start/aclose 与 Runtime 生命周期绑定，启动失败回滚，后台任务错误可观察。
- 关闭先拒绝新请求、停止候选扫描，允许有界 drain，取消剩余执行，等待心跳与子任务退出，再按所有权关闭客户端。
- 区分本地 RuntimeSession 引用计数 lease、PluginSessionLease 和分布式 ExecutionGrant，避免混用名称与释放路径。
- 跨 Worker submit 返回持久 receipt，现有 RunHandle 只保证本进程观察能力。新结果查询端口读取持久终态，不把 EventSink 当事实来源。

## 12 分步开发与验收

每步先读目标模块和局部规则，先写契约/回归测试，再完成最小纵向切片。保持当前分支，不修改 pi 子模块；每步一个完整意图提交，包含相应教学材料。

| 步骤 | 交付 | 验收门槛 |
| --- | --- | --- |
| 1 契约与 ADR | Run/checkpoint/grant/operation 值对象、执行端口及纯状态机；记录新 ADR | 普通 prompt 可持久接受；delivered 与 completed 分离；全部状态转换及非法转换有确定性测试 |
| 2 Codec | 抽取完整事件编解码，JSONL 复用 | schema v6 完整字段、错误、thinking/provider_state/控制/压缩 round-trip 不变 |
| 3 PG 基础存储 | 迁移、外部 pool、六方法适配器、错误映射 | 共享 Store 契约、重开恢复、fork、原子回滚、同版本竞争；迁移重复/并发/校验和测试 |
| 4 执行存储 | Run journal、检查点、幂等、PG epoch、绑定 writer | 旧 epoch 拒绝、同键重复返回同结果、同键异内容拒绝、提交不确定可查询、无许可写拒绝 |
| 5 Redis 和心跳 | LeaseManager、fake 与真实适配器、受监督心跳 | 互斥、比较续期/释放、超时、Redis 清空、失租与取消、无后台泄漏 |
| 6 Runtime 接线 | Builder/Session/Writer 注入、安全边界、resume、维护压缩许可 | 现有本地行为通过；同 Run 无重复 prompt；最终消息后崩溃不重复推理；序号预算连续 |
| 7 接管编排 | 候选发现、同 Session 串行、配置重建、共享尾部补偿 | 完成第 13 节故障矩阵，两个 Worker 接管，无真实模型费用 |
| 8 开发环境与证据 | compose、示例、教程、benchmark 与文档更新 | 一键启动、显式 integration 开关、可重复 trace、准确记录测试条件与限制 |

Step 3 可先独立交付单执行者 PG 存储；Step 7 前不得把已有 PG+Redis 适配器描述为完成自动故障恢复。

## 13 测试矩阵

### 13.1 默认离线测试

FakeProvider、InMemoryLeaseManager、fake execution store、fake clock 和可控 scheduler 覆盖：

| 场景 | 必须观察到 |
| --- | --- |
| prompt 接受后、提交 user 前崩溃 | 原 Run 恢复，user 恰好提交一次 |
| follow-up delivered 后崩溃 | Run 仍可发现，不依赖 pending control |
| 模型输出中途崩溃 | 不提交半条 assistant；允许重新请求并记录新尝试 |
| assistant tool_calls 提交后崩溃 | 只补齐未决结果，再进入模型续轮 |
| 多工具部分结果已提交 | 已完成结果不重复，补偿有正确 ID、名称和 provenance |
| 最终 assistant 提交后、完成标记前崩溃 | 恢复直接完成，不重复调用模型 |
| 交付 steer 与完成竞争 | 成功接收的 steer 有交付或明确取消事实，不静默丢失 |
| 接管后旧 Worker 写入 | 无许可、旧 epoch、旧 owner/claim 均拒绝 |
| Redis 超时/清空、PG 续租失败、心跳异常 | 停止新派发，任务取消被等待，故障可观察 |
| 占用的 Session 有更早 follow-up | 同 Session 不并行，恢复活跃 Run 后才运行队列 |
| COMMIT 后回包丢失 | 按 operation_id 查回结果，不重新执行工具 |
| 达到请求上限或恢复 attempt 上限 | 不因重启重置预算，不无限恢复 |
| close、abort、手动压缩与失租竞争 | 不删除新 owner 许可，无未回收后台任务 |
| 配置版本/权限/workspace 不匹配 | 不运行工具，明确终止恢复或失败 |

所有时间推进由 fake clock/scheduler 驱动，禁止单测依赖真实 sleep 1 秒。现有 Store、控制、恢复、压缩和错误分类回归按影响范围执行。

### 13.2 显式集成测试

通过独立环境开关启用 PG/Redis；默认 discover 跳过，fixture 不访问付费模型。使用两个独立 pool/客户端模拟两个 Worker，不能只在单个共享 Python 锁内启动十个协程。

必须验证：十个相同 expected_version 写入仅一个成功；领取竞争；心跳与接管锁顺序；事务中断完整回滚；Redis 重启不回退 PG epoch；含控制事件的 fork；数据库真实唯一约束和超时分类。进程终止测试用同步屏障定位崩溃点，TTL 等待有截止时间，避免依赖固定 sleep 猜测完成。

Windows managed/restricted sandbox 中，依赖系统 Temp、Proactor/socketpair 的测试按 AGENTS.md 直接使用获准的沙箱外离线命令；不要先重复触发已知沙箱阻塞。纯同步、仅 workspace 文件写入的聚焦检查仍在沙箱内完成。

## 14 可观测性与教学交付

执行日志记录 accepted、claimed、checkpoint、recovery decision 和 terminal 等事实；Live/Capability 事件记录 lease acquired/renew failed/lost、recovery started/completed、commit unknown 与冲突。名称在契约测试中固定，不能与现有 live run_completed 混淆。

公共 trace 包含 session/run/attempt/epoch、operation_id、相关 durable event ID、恢复源版本和错误分类；不包含 Redis 完整租约 value、数据库 DSN 密码、prompt、工具参数、provider_state 或供应商响应原文。原始 prompt 仍属于受控数据库内容，不能因用于去重而复制到公开日志。

提供可运行 FakeProvider 示例，展示正常文本、工具中断补偿、最终提交后接管和旧 Worker 被拒绝。教程导入正式包，解释 PG 与 Redis 分工、双写窗口、检查点和未知副作用，不维护第二套实现。

更新 `docs/architecture.md`、`learning-path.md`、`knowledge-map.md`、`upstream-mapping.md` 和新 ADR。映射记录所参考的具体符号和固定版本；不要把本阶段 Run journal 描述成 Pi 原样实现。

benchmark 至少记录代码 commit、Python/驱动/数据库版本、数据规模、池容量、Worker 数、TTL/心跳、故障点、恢复延迟分位数、冲突率、失败分类和样本数。并发吞吐与正确性分别报告，不用有限次“成功率 100%”代替故障模型说明。

## 15 完成定义

- 三个 Store 的共享契约和原有本地路径通过，六方法协议没有破坏性变更。
- 普通 prompt、已 delivered follow-up 和尾部工具中断均可恢复。
- Run/消息/检查点关联一致，可独立检查和重建；心跳只属于协调状态。
- Redis 失效不导致 PG epoch 回退；接管后的僵尸写入被拒绝。
- 没有重复 prompt、自动重放未知副作用、重置预算或恢复终态任务。
- 取消、超时、提交结果未知、关闭和资源释放有确定性证据。
- 默认测试离线，真实集成与 benchmark 显式运行；示例、教程、知识地图和映射齐全。
- `git status --short`、目标检查、`git diff --check`、工作区及暂存 diff 已检查；没有秘密、产物或上游 submodule 变更。

## 16 外部机制参考

- [PostgreSQL 15 显式锁](https://www.postgresql.org/docs/15/explicit-locking.html)：行锁持续至事务结束；候选列表不是事务外的执行许可。
- [PostgreSQL 15 SELECT](https://www.postgresql.org/docs/15/sql-select.html)：SKIP LOCKED 的用途与限制。
- [Redis 分布式锁](https://redis.io/docs/latest/manual/patterns/distributed-locks/)：TTL、完整 value 比较释放及异步复制的互斥限制。
- [Redis Cluster 规范](https://redis.io/docs/latest/operate/oss_and_stack/reference/cluster-spec/)：未来多 key Lua 必须使用相同 hash slot。

本规格选择 PG 维护 epoch、独立执行日志和受保护 writer，是本项目的设计决定；上述资料仅支持数据库与 Redis 机制，不代表外部系统替本项目提供端到端 exactly-once。
