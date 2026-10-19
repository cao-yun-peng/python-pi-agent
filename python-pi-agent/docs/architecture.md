# Architecture

## 当前边界

```text
domain.py <- ports.py <- runtime.py <- examples / future apps
               ^
               ├── testing.py implements ModelProvider
               └── providers/deepseek.py implements ModelProvider
```

- `domain.py` 只包含无 I/O 的值对象。
- `ports.py` 定义核心运行时所需能力，不提供供应商实现。
- `runtime.py` 消费端口并维护已提交消息。
- `events.py` 提供最小观察适配器。
- `testing.py` 是确定性 Provider 实现，也是 Provider 契约的第一份证据。
- `providers/deepseek.py` 隔离 DeepSeek 的 HTTP、认证、SSE 和错误格式。

## 状态模型

当前消息列表是唯一模型可见状态。用户消息在请求前提交；assistant 消息只有在收到明确的 `Finish` 后才提交。流式文本仅通过 live event 暴露。

下一阶段引入 `SessionStore` 后，durable event 会成为恢复状态的依据，内存列表则成为其投影。

## 依赖规则

- domain 不依赖 ports 或 runtime。
- ports 只依赖 domain。
- runtime 依赖 domain 和 ports，不依赖具体 Provider。
- apps 负责显式组装，不通过 import 副作用注册。
