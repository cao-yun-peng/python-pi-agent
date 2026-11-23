# ADR 0050 共享 schema v6 会话事件编解码

## 背景

JSONL 持有事件和消息的完整编码规则，模型请求快照借用其私有函数。新增 PG Store 若另写序列化，将容易丢失 thinking、provider_state、控制及压缩载荷。

## 决定

把纯编解码函数及字段集合原样迁入 `storage/_codec.py`。JSONL 只保留文件、锁、to_thread、flush/fsync；请求快照直接依赖共享 codec。保持 schema v6、字段校验、JSON 排序/Unicode/分隔符、异常分类和带行号错误信息。Codec 不依赖文件、数据库或运行时。

## 备选方案

PG 复制 JSONL 编码会形成两份契约；让 PG 导入 JSONL 则耦合存储实现。采用私有共享模块，当前不扩大稳定 facade。

## 后果

编码位置改变，领域和文件语义不变。不新增旧版本兼容，不借此调整原 codec 对 JSON 数值的行为。PG 的 JSONB 额外表示限制由 PG 边界验证。本步骤不提供 PG 存储。

## 验证方式

`test_session_codec` 固定完整载荷的编码字节、六类事件与 steer round-trip、字段错误和无法编码路径；JSONL、模型请求快照及 Session 日志回归验证跨进程恢复与取消。示例 51、教程 48 展示输入事件到 v6 记录再到领域对象。

Definition 为现有 DurableSessionEvent/schema v6，Provider 为共享 codec，Consumer 为 JSONL/请求快照及后续 PG；纯模块不创建资源、不注册全局状态，Evidence 为上述同步与既有异步测试。
