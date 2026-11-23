# 第 46 章：将单题实验变成可恢复的串行评测

## 问题与位置

评测是一个应用：数据选择 → 环境 control → 模型/工具执行 → 独立判分 → 固定分母汇总。复用正式 Runtime；业务层不重新实现 Agent loop。

先运行不需要 Docker/网络/密钥的例子：

```powershell
.\.venv\Scripts\python.exe python-pi-agent/examples/48_serial_evaluation.py --output .tmp/evaluation/demo-batch-001
```

它的 synthetic grader 仅验证编排，不代表真实修复成绩。查看 batch.json、state.json、summary.json，以及 cases/ 下的 case.json、report.json 和 segments/。默认离线测试为 test_evaluation_policy、test_evaluation_batch、test_swebench_evaluation。

## 配置和执行

仓库根 `evaluation-policy.json.example` 是无秘密策略，包含每题 40 次逻辑请求、1200 秒墙钟预算、全批次物理请求启动间隔 30 秒和每次请求最多 3 次物理尝试。模型输出上限、thinking 和凭据引用仍在 provider YAML。provider YAML 不能另外启用 resilience 或业务 capability，以免策略重复。

先按第 45 章固定 HF revision、选定 ID，使用 prepare 生成 tasks.json 和 grader-data.json，并显式准备本地镜像。

```powershell
python python-pi-agent/examples/47_swebench_evaluation.py batch --tasks PATH/tasks.json --grader-data PATH/grader-data.json --config PROVIDER.yaml --env .env --policy evaluation-policy.json.example --output .tmp/evaluation/batches/dev-001
```

每题 gold control 不通过时不调用模型，仍保留在选定题目的分母内。模型使用新任务容器，只能查询 base commit；gold 数据不进入模型。候选补丁在另一容器评分。运行中每完成一个阶段都会更新 state.json 和 summary.json。trace.jsonl 在该段退出时写出，session.jsonl 在运行中逐消息持久化。

## 限流和重试

请求链为 Recording(logical) → ResilientModelRuntime → LimitedProvider(shared limiter) → Recording(physical) → raw Provider。物理尝试共用批次 limiter；重试不占新的逻辑请求预算，但消耗墙钟时间。已有 ResilientModelRuntime 在开始输出后不自动重试，避免把不完整输出混进另一次回答。失败和取消仍保存已完成 usage 的汇总。

## 续跑恢复两种状态

单题 `run` 与批次 `batch` 均支持原输出目录上的 `--resume`。必须使用完全相同的任务、代码、策略和模型设置。manifest 中的截止时间不变，已准备的逻辑请求继续计数。CLI 重启后的首次 admission 保守等待一个间隔，避免立即撞上上个进程的限流窗口。

每段结束时对 session.jsonl 与 patch.diff 计算 SHA-256。恢复前先核对 checksum，在新容器恢复原 base tree 和 patch，再由 SessionStore 还原对话。仅追加继续任务的用户消息，不重新执行之前的工具。

如果缺少 checkpoint、文件被修改或镜像身份改变，将在模型调用前拒绝恢复。运行中取消若仍能导出工作区，会保存 checkpoint；工具取消导致容器失效时不能假装能恢复。硬崩溃的未完成段也拒绝自动重放。

批次 resume 会跳过已经评分的失败题，保留原始尝试；它用于恢复中断，不用于反复尝试直到成功。需要新的实验时选新输出目录。不要在运行期间移动输出或修改代码/配置。OS 锁会拒绝第二个 writer。

## 汇总怎样读

```powershell
python python-pi-agent/examples/47_swebench_evaluation.py summarize --output .tmp/evaluation/batches/dev-001
```

summarize 不调用模型或容器；离线重建汇总时需要获得输出锁。运行中直接查看已发布的 summary.json。

- selected_instances：原始选择的题数，包含未启动、失败与空补丁。
- execution_counts：completed/timeout/runtime_error/environment_failed 等运行终态。
- grade_counts：graded/empty_patch/no_prediction/grader_error 等判分状态。
- resolved_instances / selected_instances：官方解决比例，不以完成题数替换分母。
- reported_usage：已返回的 usage 之和，不承诺涵盖服务端未返回的消耗。

run_failed 与 resolved=true 可以并存：请求次数耗尽时导出的补丁仍可能通过官方测试。应同时报告两项。多段续跑不是一次正常收尾的运行，也不等同于全量数据集成绩。

## 练习与取舍

取消一次 FakeProvider run，再恢复，验证第一次用户消息仍在模型上下文中，工具结果不会被重复执行；修改 patch 验证 checksum 拒绝；删除一条 batch row 验证汇总拒绝丢失分母；让 grader 退出 0 但报告 empty_patch 验证 resolved 仍为 false。

对应 Pi/DeepSeek Harness 的会话、Provider 策略与可重放证据接缝，使用单包应用编排。省略多 Agent、并行 worker、分布式锁、模型 judge 与排行榜上传，设计见 ADR 0048。

## 首批真实运行：dev-smoke-001

2026-11-23 使用代码 commit `e1ae46950d5e6364a6a3d48d9413f519a8222f43`、SWE-bench/SWE-bench_Lite dev split、HF revision `b0dde1093fe417d83b7184254edf8199c1f0dff5`，预先固定三题，不按结果替换。模型为 `kimi-k2.6`，thinking enabled，max_tokens 8192，provider timeout 90 秒；策略为本章默认的 40 次逻辑请求、1200 秒、30 秒启动间隔、最多 3 次物理尝试。独立评测环境为 Python 3.12、swebench 5.0.2、datasets 5.2.0、docker 7.2.0；任务内 Python 与依赖由各自镜像提供。

| 题目 | Gold control | 运行终态 / 请求数 / 耗时 | 候选判分 |
|---|---|---|---|
| marshmallow-code__marshmallow-1343 | 通过 | request_limit / 40 / 1183 秒 | empty_patch，未执行候选测试，未解决 |
| marshmallow-code__marshmallow-1359 | 通过 | completed / 36 / 1067 秒 | graded，76 passed、1 failed，未解决 |
| pydicom__pydicom-1139 | 未通过 | environment_failed / 0 / 未调用模型 | preflight_failed，未生成候选 |

固定分母汇总为 0/3；实际两题调用模型，一题在环境校验时被拦截。这是流程和失败诊断的小样本，不代表整个数据集的能力分数。累计已返回 usage 为 input 584435、output 13255、total 597690 tokens；76 次逻辑请求对应 76 次物理请求，本批没有触发供应商重试。重试和中断续跑依靠离线确定性测试验证，不能声称本批经历了这两种路径。

三题实际镜像 ID 分别为：

- 1343：`sha256:a61197e0b29461e34010cd83570424eb91a7f265574a81c82ff79e55c6c10f29`
- 1359：`sha256:440807ded8d239ea71daaf31d3158013e58b9cdfad8a56d4f82e646426d3f4e6`
- pydicom：`sha256:a2344b4b6db428913523e14f576fe358d6ae0c349cd77ac82e616188955ddb3b`

输出位于仓库根 `.tmp/evaluation/batches/dev-smoke-001/`，不提交原始模型响应、数据或会话。先打开 summary.json 看固定分母及两个终态，再沿 row.case_path 或 row.official_report 查证：

1. 1343 的 session 最后一个工具是 `git checkout -- src/marshmallow/schema.py`。模型先后产生了缩进错误与重复函数声明，最终主动撤销修改，因此 checkpoint 中 patch 为零字节。
2. 1359 的补丁用嵌套 getattr 避免访问不存在的 opts，复现中的 List/Tuple 可以初始化。但官方新增测试 `TestParentAndName::test_datetime_list_inner_format` 期望继承 Schema 的 `iso8601`，实际得到默认 `iso`。其余 76 项通过，说明“崩溃消失”仍不足以证明语义正确。gold control 的 77 项全部通过。
3. pydicom 的 gold 新增三项迭代/包含测试通过，但两个既有 PASS_TO_PASS 测试因 `self.tag` 不存在而失败。镜像使用 pytest 8.3.5，原测试用旧式 setup/teardown。已在该镜像内禁网运行最小对照：setup 未调用、setup_method 调用，得到 1 failed / 1 passed。这里的 environment_failed 是本项目预检分类；官方报告的 infra_failure=false，不能混淆。后续应单独固定兼容的测试依赖和新镜像身份，再以新批次重做 gold control，不能改写本批报告或删除失败测试。

本批结束后执行正式 summarize，并以完全相同的参数执行 batch --resume。前后核对 185 个证据文件的内容 hash 与时间戳（排除可重建的 state/summary 和锁文件）均不变，物理请求仍为 76 次；没有新解题或新判分。这验证已完成题目的幂等跳过。冻结历史也在实测中生效：模型多次 git log 只能看到 base commit，无法沿后续提交找到修复。

下一轮先修复 pydicom 测试环境，再围绕“重复查历史、碎片读取、修改后缺少测试”设计独立实验。不要在本批中追加提示、放宽预算、替换题目或反复跑失败题后仅报告成功结果。
