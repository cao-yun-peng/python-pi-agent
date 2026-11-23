# 第 48 章 共享会话事件编解码

JSONL 与数据库需要保存同一个 DurableSessionEvent。消息不仅包含 content，还包含工具身份、结构化结果、thinking 和供应商 opaque state。共享 codec 让两种适配器使用同一套必需字段和领域校验，减少格式漂移。

`storage/_codec.py` 接收领域事件，生成排序后的完整 schema v6 JSON；解码严格校验版本、字段集合、类型、事件载荷和领域约束。`storage/jsonl.py` 负责文件与 fsync，不再定义格式。模型请求快照也直接引用共享消息编码规则。

运行 `python python-pi-agent/examples/51_session_codec.py`，可看到 schema=6、稳定 event_id、round_trip=True，以及旧 schema 被带行号的 SessionReplayError 拒绝。示例导入正式模块，不维护教学版编码器。

运行 `python -m unittest discover -s python-pi-agent/tests -p test_session_codec.py -v` 验证固定编码字节和完整载荷。异步 JSONL/请求快照回归仍按仓库 Windows 沙箱约定执行。尝试删除 provider_state、给 sequence 传 bool 或改变 schema_version，应拒绝解码；给 tool structured_content 放不可编码对象，应得到 SessionValidationError。

格式抽取没有改变 JSONL 的单写者限制或取消行为。未来 PG 仍需事务、并发和 JSONB 边界测试。Pi/DeepSeek Harness 的映射沿用已有事件与显式接缝；这次是 Python 内部复用，不宣称引入新的上游 API。详见 ADR 0050。
