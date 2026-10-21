# ADR 0012: Explicit Transactional Plugin Host

## 背景

项目已经有 `Tool`、`EventSink` 和 `NextTurnHook` 等可替换能力，但组装仍由调用者逐项传给 `Agent`。随着能力增多，需要一个插件边界把相关工具、Hook 和资源生命周期组合起来，同时不能依赖 import 副作用或进程级全局注册表。

Pi 的扩展系统提供了值得借鉴的结构：扩展 factory 接收受限 API；加载期间的注册先暂存，factory 成功后 commit，失败则 discard；ExtensionRunner 按稳定顺序派发拦截事件；扩展资源在 session shutdown/reload 时释放。Pi 同时包含 UI、命令、快捷键、Provider、MCP 和磁盘发现，这些不属于本项目当前的最小核心。

## 决定

引入显式的 `PluginHost`：

1. 应用调用 `await host.load(name, factory)`，不会通过导入自动注册。
2. factory 只能使用 `PluginAPI` 注册 `Tool`、`NextTurnHook` 和 cleanup。
3. 注册先进入私有 staging 区；factory 和冲突校验都成功后才原子发布。
4. 工具名称冲突直接拒绝整个插件，不采用隐式覆盖或 first-wins。
5. Hook 按 `(priority, registration_order)` 串行执行，前一个返回值成为后一个输入。
6. Hook 被主循环等待；超时或异常以 `PluginExecutionError` 失败关闭，外部取消原样传播。
7. 每次注册返回幂等 `Registration`；插件卸载先撤销能力，再逆序执行全部 cleanup。
8. `snapshot()` 返回用于一次显式 Agent 组装的不可变能力快照。当前不支持运行中的热重载；调用者必须在已组装的 Agent 停止使用插件资源后再关闭 host。

## 备选方案

- **直接扫描目录并 import 插件**：上手方便，但过早引入信任、路径、缓存和热重载问题，也违反显式组装原则。
- **把所有扩展点做成统一字符串事件总线**：灵活但丢失类型和每个 Hook 的返回语义，容易混淆观察与拦截。
- **重复工具名 first-wins 或 last-wins**：接近部分动态框架行为，但配置错误会静默改变安全边界。
- **插件异常记录后继续**：适合观察者；对能改变下一次模型请求的拦截器会产生不可预测状态，因此当前选择失败关闭。

## 后果

- 插件初始化、能力发布和释放都有明确所有者及确定顺序。
- 插件只组合既有端口，Agent Loop 不依赖具体插件。
- 当前能力快照不会自动修改已经构造好的 Agent；热卸载需要未来的 runtime lease 或会话级插件容器。
- 当前 `NextTurnHook` 适合无持久会话的 `Agent`。对 `AgentSession` 做模型可见历史变换前，必须先定义 durable transition，不能制造内存历史与 replay 历史分叉。

## 验证方式

- 端到端测试证明插件工具和 Hook 可被 `Agent` 消费。
- 顺序测试证明 priority 和注册顺序稳定。
- factory 失败和工具冲突测试证明无部分发布且 cleanup 会回滚。
- 超时和取消测试证明 fail-closed 与取消传播。
- 注销和卸载测试证明注册句柄幂等、cleanup 逆序执行。
