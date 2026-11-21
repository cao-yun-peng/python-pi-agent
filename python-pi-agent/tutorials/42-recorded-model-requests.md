# 第 42 章：记录模型输入，在进程退出后离线还原

## 要解决的问题

Session 保存长期消息历史，请求中间件可能另外插入检索片段。模型看过片段后，片段不会自动进入长期历史。若插件卸载、索引更新或进程关闭，再次运行检索不一定得到相同输入。

本章在正式包的 ModelProvider 端口记录最终输入。快照读取与模型调用是独立操作；读取不会重新运行检索、工具或模型。

## 在生命周期中的位置

```text
Session 历史 -> ContextStrategy -> Middleware -> Session 请求插件
  -> RecordingModelProvider -> ModelRequestStore.record
  -> 提交成功 -> 被包装的 ModelProvider.stream

另一个进程 -> FileModelRequestStore.read_requests -> RecordedModelRequest
```

记录器不修改主循环。RecordedModelRequest 保存消息、工具定义、ProviderRequestContext，以及应用声明的 ModelRequestTarget。消息包括 thinking、tool call、结构化 tool result 和 opaque provider state；请求身份可以与普通运行事件关联。

Target 保存明确的 provider/model 与公共参数，由应用从自己用于构造 Provider 的同一配置中提供；不要填入 API key、Authorization、URL 或 transport 配置。记录器不反射供应商内部状态，快照因此是标准端口参数和应用声明的选择，不是供应商最终 HTTP body 或所有隐式默认值。

## 运行两个独立进程

安装正式包后，在仓库根目录的 PowerShell 执行：

```powershell
$demoRoot = Join-Path $PWD '.tmp/request-recording-demo'
New-Item -ItemType Directory -Path $demoRoot -Force | Out-Null
[System.IO.File]::WriteAllText((Join-Path $demoRoot 'reference.txt'), 'original retrieved fact')
.\.venv\Scripts\python.exe -B .\python-pi-agent\examples\44_request_recording.py record $demoRoot
Remove-Item -LiteralPath (Join-Path $demoRoot 'reference.txt')
.\.venv\Scripts\python.exe -B .\python-pi-agent\examples\44_request_recording.py restore $demoRoot
```

record 进程用 FakeProvider 完成一次工具调用和模型续轮，输出 `{"recorded_requests": 2}` 后退出。restore 进程应输出原来的检索片段两次、每次请求的 echo 工具定义、请求编号 `[1, 2]`，以及 `history_excludes_retrieval: true`。恢复函数不创建 Provider 或检索 Hook；删除检索文件不会影响读取。目录会累积记录，重复练习时选用新的空目录。

示例导入正式包，不另写一套循环。自动测试也运行这个示例，并断言跨进程结果。

## 在应用中接入

从 pi_agent_py.api 导入 RecordingModelProvider、ModelRequestTarget 和 FileModelRequestStore。将已构造的 Provider、显式请求目录和公共设置交给记录器，再把该记录器传给 AgentComponents，或传给 AgentRuntimeBuilder.with_provider。内存替身 InMemoryModelRequestStore 提供同样的 record/read_requests 协议。

记录器借用两个依赖，不负责关闭它们；Builder 使用 borrowed Provider 的默认方式时，应用仍负责原 Provider 的关闭。FileModelRequestStore 只有每次操作的线程任务，没有长期 worker。若使用自定义 Store，它必须在 record 返回前完成自己的提交，而不能把数据交给可丢弃的后台队列便返回。

包装 ResilientModelRuntime 外层，记录一次逻辑请求；将每个候选 Provider 分别包装并提供各自 Target，可按相同 request_id、不同 attempt_id 保存重试/回退输入。压缩模型同样可以使用记录器，purpose、parent_request_id 等字段会原样保存。只有经过包装器的调用会被记录。

## 失败、取消与文件发布

FileModelRequestStore 对每条记录使用独立 v1 JSON 文件，消息声明固定 codec v6；Session JSONL 版本保持不变。文件名由请求与尝试身份的摘要生成，不把原始 id 当路径。临时文件写完并 fsync 后以硬链接排他发布，同身份的第二次提交失败；没有自动覆盖或自动重发。

编码、字节预算、文件写入或同步失败时，错误传播，Provider 不执行。取消提交时，记录器不继续发送；文件 Store 等待后台线程结束后传播取消。记录可能已经发布，因此“有记录”只说明输入已准备，不能证明 Provider 已执行。Provider 自身失败或取消时，已经准备的记录保留。

读取不会把损坏文件当作可跳过数据；未知版本、重复字段、不完整 JSON、非有限数字、身份与文件名不匹配，以及超出 max_record_bytes 都会失败。未发布的 pending 文件不属于记录。首版不自动清理崩溃遗留文件，不承诺父目录条目掉电后的持久性；目录应由应用控制权限和保留期限。read_requests 的文件排序不是时间顺序，分析某个 run 时显式按身份和 request_number 组织。

## 如何验证已经掌握

运行 `python -B -m unittest discover -s tests -p test_model_request_recording.py -v`。解释以下现象：RAG 出现在请求快照但不在 Session 消息；存储失败时 FakeProvider 收不到调用；取消文件操作可以留下 prepared 记录；两个重试 attempt 的身份不同；恢复进程不需要原检索源。

然后实现一个只读取 RecordedModelRequest 的输入检查器，例如检查“必需约束是否仍在 messages 中”。它应无需插件和网络即可工作，但不能把还原输入误认为能重复生成完全相同的回答。

## 与参考项目的关系

DeepSeek Harness 的 Model-visible means logged 要求模型输入可从日志重建。这里借鉴这个要求，以单包内的端口和自包含文件实现，不引入 Cordis、请求头增量日志或跨版本迁移框架。本切片没有依赖上游 Pi 的特定接口；设计取舍见 ADR 0044。
