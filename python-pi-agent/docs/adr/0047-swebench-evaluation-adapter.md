# ADR 0047: SWE-bench Lite 单题评测应用

## 背景

会话 replay 和模型输入快照只能证明状态与输入可还原，不能证明 Agent 修复了真实代码问题。需要连接正式 Runtime 与外部可执行判分器，同时保持默认测试离线。

## 决定

在 application/evaluation 中实现独立应用：CodingTask 白名单投影、TaskEnvironment 协议、持久 Docker Provider、run_task Consumer 和显式 CLI。导入不注册能力、不启动网络。每题只开放已有 BashTool，使用 coding Profile 和显式批准；不改主循环或现有 DockerProcessSandbox。模型只收到问题描述，任务容器不挂载宿主文件或 Docker socket，不转发模型密钥。完整原始行仅在独立宿主 grader-data.json 中供官方评分使用。

prepare 必须固定 HF revision 和 split/ID。Docker 镜像必须已存在本地，启动前解析为不可变 image ID；记录代码 commit、dirty 状态、公共模型参数、请求预算、时间预算和输入快照。Docker 环境保留文件，命令仍使用新 shell。容器内 /testbed 为根，默认禁网、drop capabilities、资源限额；容器根可写，明确标记 partial，不能冒充正式 workspace sandbox 的 full enforcement。

运行 completed 与 resolved 分开：适配器始终写 resolved=null；官方 grader 在干净环境应用补丁并判分。超时/取消使执行环境失效并清理容器，不能仅杀 Docker CLI。运行失败仍尽力导出部分补丁；补丁截断或环境失效不输出残缺预测。错误报告只记录类型，保留异常语义，取消继续向调用方传播。

## 备选方案

- 将第三方 Agent 当执行器：无法直接评测本项目 Runtime，因此不采用。
- 把任务容器语义塞进现有 ProcessSandbox：混淆只读根/workspace 挂载契约，因此采用独立 ShellExecutor Provider。
- 使用 LLM judge：首版优先官方可执行测试，不增加 judge 成本与主观性。

## 后果与边界

任务环境是可信 benchmark 镜像，不是恶意镜像安全执行平台；不保证防恶意 Agent 获取镜像内原有信息。环境准备/模型执行/正式评分必须分离，参考补丁不通过本适配器传给 Agent。首版只支持当前 HF 行中的 image 字段、Linux x86_64 和 testbed conda 布局；不自动猜测旧 schema。没有多题并行、token 硬预算、自动拉镜像、自动重试、价格估算或排行榜上传。时间预算包含环境准备和 Runtime，补丁导出与清理使用额外有限预算。

## 验证

test_swebench_evaluation 使用 FakeProvider、脚本化 Docker runner、临时目录验证输入隔离、工具续轮、请求快照、预测格式、部分补丁、失败和取消。真实子进程传输测试显式使用 PI_AGENT_EVAL_PROCESS_INTEGRATION=1 开启，不调用模型。Docker 实例与官方 gold baseline 是独立手动集成步骤，不计入离线通过数。

参考（核对日期 2026-11-22）：[官方 CLI](https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/cli.md)、[本地 JSON 加载](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/utils.py::load_swebench_dataset)、[开发集](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite/viewer/default/dev)。正式实验另行固定评测器 commit/版本，避免使用漂移的 main 作为结果版本。

## Windows 集成验证补充

实测 swebench 5.0.2 在 Windows 将 eval.sh 与 patch.diff 写成 CRLF，gold baseline 因脚本/补丁执行失败而 unresolved。采用独立 grading 子进程中的模块级 Path 构造器替换，只对这两个传入 Linux 的文件使用 UTF-8/LF 二进制写入；finally 恢复原构造器，不更改全局 pathlib、安装文件或官方测试判分。备选方案为单独部署 Linux grader；本地首版保留 Windows 应用入口。该接缝固定官方 5.0.2，依赖升级需重新验证。

回归测试覆盖原始 CRLF 输入、产物字节和失败后的构造器恢复。修复后同一开发题的 gold baseline resolved=true、77 个测试通过；真实 Agent 成绩尚未测量。默认 `[eval]` 显式安装已验证的官方 grader，完整依赖版本仍由实验环境 lock 保存。
