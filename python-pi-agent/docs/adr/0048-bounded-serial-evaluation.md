# ADR 0048：有预算的串行评测与可验证续跑

## 背景

单题实验依赖临时脚本实现请求间隔与重试；请求数上限后曾通过手动恢复补丁/JSONL 续跑。需要将这些接线纳入正式应用，避免重置预算、重复执行工具、遗漏失败题和误把进程退出 0 当作题目解决。本次还发现原镜像的 Git 对象能暴露未来修复。

## 决定

- `EvaluationPolicy` 保存总逻辑请求数、墙钟总时限、物理请求间隔与重试设置。严格验证 JSON，拒绝未知字段；模型参数和秘密仍属于既有 provider 配置。
- `SharedRequestLimiter` 是每批次显式创建的能力实例，每次物理尝试共用它。锁只保护启动间隔，等待可取消；时钟与 sleep 可替换。`LimitedProvider` 借用依赖，重试复用 `ResilientModelRuntime`，仅允许其既有 pre-output 重试语义。
- `run_case` 固定任务、来源、模型设置、提示词 hash、代码版本和策略；首段记录绝对 deadline。每段保留独立请求、trace、报告及 session/patch SHA-256 checkpoint。续跑验证身份与 checksum，恢复工作区和会话，不重放旧工具；总请求数按所有逻辑输入快照计数，时间和请求额度均不增加。没有完整 checkpoint 的崩溃段拒绝自动续跑。
- OS 文件锁保证同一输出只有一个 writer，进程退出自动释放。报告和状态通过 fsync + 原子替换发布。物理重试输入独立存放，prepared 仍不能证明远端接收。
- `run_batch` 串行执行预选清单，共享 limiter，每题先运行 gold control，再执行 Runtime 和独立官方判分。逐题状态原子提交，失败继续下一题；`--resume` 跳过所有已完成的评分，包括失败与空补丁，避免静默重抽样。取消保留状态并传播。
- 汇总固定分母为选定题目数；分别保留 execution/grade 状态、用量与错误。评分器异常不把已完成的执行改写成执行失败。读取官方报告中的任务 ID、resolved/empty/error，不能根据 CLI 退出码判定修复。
- 任务容器默认只保留 base commit：在容器内通过 `git clone --no-local --no-tags --single-branch --depth 1` 复制当前起始修订，替换原 Git 数据并删除 remote。保留原始 commit hash 和代码，后续历史对象不可查询。正式 CLI 不开放 full-history 切换；该事实进入 manifest，集成测试验证后续修复对象不可读。

## Definition / Provider / Consumer / Lifecycle / Evidence

策略与 `TaskEnvironment`/`RestorableTaskEnvironment`/`CaseGrader` 是定义。Docker、OfficialGrader 和离线替身是 Provider。case/batch/CLI 是 Consumer。Provider、limiter 和输出锁显式组装；Runtime/容器始终关闭，borrowed 模型由 CLI 关闭；取消不发布成功结果。证据为 policy/case/batch 离线契约、真实 Docker 历史隔离/恢复、示例 48 与正式小批量实验。

## 备选方案

继续复制脚本会重复预算和关闭逻辑；重写主循环会让评测策略侵入核心；自动重试整道失败题会改变分母和成功率定义。首版选择应用层串行编排，保持原正式 Runtime。

## 后果

共享限流只覆盖一个批次实例，不协调其他进程/账户流量。默认总时限含恢复与限流等待；发布补丁、清理和官方判分有独立有限超时。token 用量来自成功返回的 usage 事件，不是计费承诺或硬 token cap。没有并行调度、自动拉镜像、分布式租约和价格表。恢复已超时 case 不再调用模型；代码、配置或 checkpoint 改变时拒绝恢复。完整进程崩溃而尚未导出补丁时需人工处理，不能猜测文件状态。

## 验证方式

FakeProvider 覆盖 429 重试、取消、恢复后的原历史、工作区恢复和预算不重置；fake clock 覆盖启动间隔；测试覆盖篡改拒绝、变更镜像、输出锁、失败分母、批次恢复和评分进程退出 0 但空补丁。真实 Docker 集成验证仅一个 commit、base hash 不变、未来 Git 对象不可读及 stdin patch 恢复。默认测试不联网、不调用付费模型。
