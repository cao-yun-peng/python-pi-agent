# ADR 0043：契约归属与静态依赖边界

状态：已接受，2026-11-20。

## 背景

ports 在 TYPE_CHECKING 中反向引用 providers/runtime 的模型策略 DTO，以及 runtime/session_spec 的 SessionSpec。运行时虽不执行这些导入，接口理解、类型检查和维护仍依赖上层。用户授权直接移除旧具体路径，不添加兼容层。

## 决定

- ProviderCandidate、AttemptFailure、RetryDecision、RetryAction 移到 ports，与消费者策略协议相邻；保持字段、冻结行为和校验不变。Candidate 持有 ModelProvider，不能放进不依赖端口的 domain。
- SessionSpecResolver 与 SessionSpec、StaticSessionSpecResolver 同属 runtime/session_spec；基础 ports 不承担运行时完整组件选择。
- providers/runtime 通过私有模块限定引用契约，不再留下原公开类型属性。根包既有 DTO 导出、api 的 SessionSpecResolver 导出直接指向新定义；不扩大 api 导出集合。旧 providers.runtime 中四个名字与 ports.SessionSpecResolver 已移除。
- AST 检查基础层与 runtime 的静态导入，覆盖 TYPE_CHECKING、局部导入、相对路径和包成员导入。安全默认和压缩编排有精确的消费者/模块例外；不新建发布包或通用依赖框架。
- mypy 显式加入 session_spec、components、middleware、providers.runtime。迁移前四模块独立 strict 基线通过；应用组装层后续另批扩展。

## 备选方案

把 DTO 全部塞入 domain 会引入 domain → ModelProvider；把 SessionSpec 一并下移会牵动 AgentComponents、Middleware 与安全默认；拆出 contracts 包或 ports 包会扩大当前迁移面。保留 TYPE_CHECKING 反向引用只修改文档可接受，但无法达到本次明确的基础依赖目标。旧路径 shim 与用户授权的删除方向相反。

## 后果

具体模块导入和类的 __module__ 改变；不承诺旧 pickle 路径兼容。业务控制流、错误分类、事件顺序、durable 格式与资源所有权不变。AST 只处理静态导入，不证明动态加载安全、不追踪全部传递依赖。允许的 runtime/providers.runtime 是供应商无关组合层；安全默认仍属有记录的分层例外。

## 验证

test_architecture 在迁移前实际捕获 ports 的反向引用，迁移后通过；合成违规源码验证类型分支、局部/绝对/相对导入和精确例外。test_port_contracts 验证唯一身份、旧属性移除、get_type_hints 和原校验。模型重试/回退/取消、SessionSpec 冲突/取消、组件、生命周期与 API 用现有离线测试覆盖。示例 23、31 从正式包运行。同步教程 41、架构、学习路径、知识地图和上游映射。
