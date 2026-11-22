# 第 45 章：用真实代码任务评测 Harness

本章回答：模型能调用工具之后，怎样证明这套系统能修好 Bug？评测应用导入正式 Runtime，评分委托给 SWE-bench，不另写 Agent 循环。

## 先跑离线纵向切片

在仓库根目录执行（先按 README 安装正式包）：

```powershell
.\.venv\Scripts\python.exe .\python-pi-agent\examples\47_swebench_evaluation.py demo --output .tmp/swe-demo-001
```

每次使用新目录。合成题使用 FakeProvider，完成 bash 调用与一次模型续轮，写入 patch.diff、predictions.jsonl、report.json、session.jsonl、trace.jsonl 和 requests/。报告 completed 只表示执行完成，resolved=null 表示未经官方评分。这不是真实 SWE-bench 分数。

## 准备真实开发题

先安装可选的数据和评分依赖到单独评测虚拟环境，并记录具体版本（建议冻结 requirements）；本包导入不要求这些依赖。运行以下步骤会联网下载数据/镜像；真实 run 会调用你配置的模型。

```powershell
python -m pip install datasets swebench
```

从 HF 数据集 Files 页复制当前 commit hash，将下面 REVISION 替换成完整 40 位值。prepare 不接受 main，避免题库更新改变实验。

```powershell
python .\python-pi-agent\examples\47_swebench_evaluation.py prepare --revision REVISION --split dev --instance-id marshmallow-code__marshmallow-1359 --output .tmp/swe-tasks-001
```

tasks.json 只包含 source 和模型任务所需字段；grader-data.json 保存同一 revision 的完整原始行。不要把 grader-data.json、参考 patch、test_patch 或 eval_script 加入提示词或挂载进执行容器。不要提交这些下载数据。

从 tasks.json 中读取 image，显式 docker pull 对应镜像。镜像体积和依赖可能很大；先只取一题。适配器仅使用本地镜像，不在 run 中隐式下载。Windows 使用 Docker Desktop 的 Linux 引擎；任务默认 x86_64。

## 先验证官方判分环境

```powershell
python .\python-pi-agent\examples\47_swebench_evaluation.py grade --dataset .tmp/swe-tasks-001/grader-data.json --gold --instance-id marshmallow-code__marshmallow-1359 --run-id pi-gold-001
```

gold baseline 应满足官方测试；若失败先排查镜像/依赖/评测器，不能归因于我们的 Agent。空补丁可检查空预测处理，但官方评分可能直接跳过，不能据此证明测试环境工作正常。官方 grade 在独立干净容器使用保存的同版数据，不复用 Agent 已修改的容器。命令退出码只说明评分进程是否执行成功，任务是否 resolved 仍需阅读官方报告。

## 运行我们的 Agent

使用只声明 provider 的 agent YAML，不能启用宿主工具 capability。密钥继续通过 api_key_env 和显式 --env 解析；不把密钥写入报告或容器。

```powershell
python .\python-pi-agent\examples\47_swebench_evaluation.py run --tasks .tmp/swe-tasks-001/tasks.json --instance-id marshmallow-code__marshmallow-1359 --config agent.yaml --env .env --output .tmp/swe-run-001 --max-requests 30 --timeout 600
python .\python-pi-agent\examples\47_swebench_evaluation.py grade --dataset .tmp/swe-tasks-001/grader-data.json --predictions .tmp/swe-run-001/predictions.jsonl --instance-id marshmallow-code__marshmallow-1359 --run-id pi-prediction-001
```

依赖应安装在执行这些命令的同一 Python 环境。目录和 run-id 均需唯一，官方 grader 会复用已存在的结果。参考 grader 具体输出位置随版本变化，检查它的 stdout、逐题测试输出和报告；不要将命令退出 0 当作修复成功。

## 如何阅读证据

路径为：CodingTask → DockerTaskEnvironment.start → AgentRuntimeBuilder → BashTool → ShellExecutor → 实际 diff → predictions.jsonl → 官方 grader。

- report.json：环境 image ID、代码 commit/dirty、模型和公共参数、任务 source、请求上限、用量、耗时、事件数、执行状态。dirty=true 的运行不适合作为可复现正式结果。
- requests/：实际模型输入与工具定义，用于排查上下文丢失；不是模型响应可复现保证。
- session.jsonl：durable 消息和工具结果；trace.jsonl 关联模型/工具请求、异常和 settled。
- predictions.jsonl：从真实工作区导出，包含新文件；即使模型不输出补丁也能评分。未完成运行的部分补丁可用于诊断，不能算成功。
- 官方报告：FAIL_TO_PASS 全过且 PASS_TO_PASS 不退化才算 resolved。基础设施失败单列，仍保留全部选定题的分母。

第一个目标是 1 题 gold baseline 和 1 次模型执行，之后固定 3～5 题 dev 调试，再预先固定测试列表做对照。保持模型、预算、题目和 grader 版本一致，每次只改工具或上下文策略之一。真实模型重复运行可能不同；多次尝试须分别记录，不能只挑成功结果。

## 失败与取消练习

默认测试离线：`python -m unittest discover -s python-pi-agent/tests -p test_swebench_evaluation.py -v`。Windows 受限沙箱按 AGENTS.md 直接在允许 Temp/本地 socket 的环境运行。

尝试使用不存在的本地镜像：应写 infrastructure_error、resolved=null，无预测。运行中取消应传播到模型并删除任务容器；单条命令超时也会作废容器，不能自动重试副作用未知的命令。patch 超过输出预算时拒绝发布半份补丁。

本章没有实现多题调度、模型 judge、排行榜提交或压缩质量数据集。与 Pi/DeepSeek Harness 的对应关系见 ADR 0047 和 upstream-mapping；本章评测入口属于 Python 应用扩展，不依赖上游 SDK。
