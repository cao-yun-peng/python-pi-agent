# 设计交接：B5 Middleware 链与 B6 `@tool` 装饰器

> 状态：待实现（交给 Codex）
> 基线：`codex/refactor-functional-layout` @ `85951c6`
> 关联：[路线图](../roadmap.md) B5、B6；ADR 0012、0019、0028、0031
> 交付时须拆为 ADR 0033（B5）与 ADR 0034（B6），内容取自本文对应章节。

## 0. 给实现者的约定

- 先读 `AGENTS.md`，尤其 §5 教学交付、§7 Capability 五要素、§8 观察与拦截分离、§11 测试、§13 提交规则。
- 本文中的"现状"均已对照基线源码核实，行号以 `85951c6` 为准。实现前用 `rg` 复核一次，行号漂移时以符号名为准。
- 先做 B6，再做 B5。B6 独立、改动面小；B5 的测试和示例会用到 `@tool`。
- 每部分一个原子提交，测试、示例、教程、文档随代码同提交：
  1. `feat: add typed @tool decorator`
  2. `feat: add middleware chain for provider requests and tool results`
- 默认测试保持离线、确定性；超时测试沿用 `tests/test_tool_runtime.py:233` 的 `timeout_seconds=0` 写法，不引入真实等待。
- 不得通过 import 副作用注册任何东西；装饰器和 Middleware 都只产生值对象，由调用方显式交给 `AgentComponents`、Builder 或 `PluginAPI`。

---

# 第一部分：B6 `@tool` 装饰器（ADR 0034）

## 1.1 背景

现在写一个工具要手写三样东西（见 `examples/03_tool_calling.py`）：

1. `ToolDefinition`，其中 `input_schema` 是手写的 JSON Schema（`domain.py:113`）；
2. 签名固定为 `(arguments: JSONObject, context) -> ToolOutput` 的 async handler（`tools/function.py:10`）；
3. 在 handler 里从 `arguments` 字典取值、自己转换类型、自己包装 `ToolOutput`。

schema 与函数签名分处两地，容易漂移；schema 写错时只在模型调用时才暴露。参数校验器 `tools/validation.py` 只支持 `type/properties/required/additionalProperties/items/enum` 子集，手写 schema 时很容易写出校验器会静默忽略的关键字。

## 1.2 决定

新增 `tools/decorator.py`，提供 `tool` 装饰器、`ToolError` 异常和 `DecoratedTool` 类型。函数签名是 schema 的唯一来源，生成的 schema 只使用校验器真正执行的关键字。

### 用法

```python
from typing import Annotated, Literal
from pi_agent_py.api import AgentComponents, ToolError, ToolExecutionContext, tool

@tool(side_effects="read", timeout_seconds=10)
async def read_note(
    path: Annotated[str, "Workspace-relative note path"],
    max_lines: int = 200,
    mode: Literal["head", "tail"] = "head",
) -> str:
    """Read one note from the workspace."""
    if ".." in path:
        raise ToolError("path must stay inside the workspace")
    ...

@tool
async def whoami(context: ToolExecutionContext) -> dict[str, object]:
    """Report the current session identity."""
    return {"session_id": context.session_id}

components = AgentComponents(provider, tools=(read_note, whoami))
```

### 签名

```python
def tool(
    fn: Callable[..., Awaitable[object]] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    side_effects: ToolSideEffects = "none",
    requires_approval: bool = False,
    retry_safe: bool = False,
    replay_safe: bool = False,
    timeout_seconds: float | None = None,
) -> DecoratedTool | Callable[[Callable[..., Awaitable[object]]], DecoratedTool]:
```

- 同时支持 `@tool` 和 `@tool(...)`。
- 安全元数据的默认值与 `ToolDefinition` 完全一致，不另设默认；`side_effects` 必须由作者如实声明，默认 Gate 只放行 `"none"`。
- 返回 `DecoratedTool`，它实现 `Tool` 端口（`definition` 属性 + `async execute(arguments, context)`），另暴露 `.fn`（原函数，供单元测试直接调用）和 `__wrapped__`。
- 装饰器不维护任何注册表。同名工具重复装饰互不影响；重名只会在放进同一个 `AgentComponents` 时被已有校验拒绝。

### 名称与描述

- `name` 缺省取 `fn.__name__`。必须匹配 `^[A-Za-z0-9_-]{1,64}$`（OpenAI-compatible 函数名约束），否则装饰时抛 `ValueError`。
- `description` 缺省取 `inspect.getdoc(fn)` 的第一段（到第一个空行为止，去首尾空白）。两者都为空时抛 `ValueError`。
- v1 不解析 docstring 里的参数说明（Google/NumPy 风格），参数描述只来自 `Annotated`。

### 类型到 schema 的映射

用 `typing.get_type_hints(fn, include_extras=True)` 解析注解，兼容 `from __future__ import annotations`；解析失败抛 `TypeError`，消息包含工具名和参数名。

| Python 注解 | 生成的 schema |
|---|---|
| `str` / `int` / `float` / `bool` | `{"type": "string" / "integer" / "number" / "boolean"}` |
| `None`（仅作为 Optional 成员） | `"null"` |
| `Literal[v1, v2, ...]`，成员同为 str、int 或 bool | `{"type": T, "enum": [v1, v2, ...]}` |
| `list[T]` | `{"type": "array", "items": schema(T)}` |
| `dict[str, object]` / `dict[str, Any]` / `JSONObject` | `{"type": "object"}`（值不校验） |
| `X \| None`（`Optional[X]`），X 为上表任一 | X 的 schema，`type` 改为 `[X 的 type, "null"]`；若有 `enum` 则追加 `None` |
| `Annotated[T, "说明文字"]` | schema(T) 加 `"description": "说明文字"` |
| 注解为 `ToolExecutionContext` 的参数 | 不进 schema，执行时注入当前 context；最多一个 |

根 schema 固定为：

```json
{"type": "object", "properties": {...}, "required": [...], "additionalProperties": false}
```

- 无默认值的参数进入 `required`，保持声明顺序。
- 有默认值的参数不进 `required`，并写入 `"default"`；默认值必须是 JSON 值（`json.dumps` 可序列化），否则装饰时抛 `TypeError`。
- 下列情况一律在**装饰时**抛 `TypeError`，不留到运行时：
  - 同步函数、async generator（只接受 `async def` 协程函数）；
  - 缺少注解的参数；`*args`、`**kwargs`、仅限位置参数；
  - 不在上表中的类型：`set`、`tuple`、`bytes`、裸 `list`/`dict`、键不是 str 的 dict、多类型 Union（如 `int | str`）、`Enum`、`TypedDict`、dataclass、pydantic 模型、任意自定义类；
  - 混合类型的 `Literal`；
  - 两个以上 `ToolExecutionContext` 参数。
- 实例方法会因 `self` 无注解被拒绝。文档中推荐用工厂闭包携带依赖，这和 `AgentProfile.tool_factories` 的用法一致：

  ```python
  def make_note_tools(store: NoteStore) -> tuple[Tool, ...]:
      @tool(side_effects="read")
      async def read_note(path: str) -> str:
          """Read one note."""
          return await store.read(path)
      return (read_note,)
  ```

### 校验器的配套修改

`tools/validation.py::_matches_type` 目前只接受字符串形式的 `type`（`validation.py:34-37`），列表形式会被静默跳过。新增：`type` 为字符串列表时，值匹配任一成员即通过；列表为空或含非字符串时视为 schema 错误，记一条 violation。这是 Optional 参数能真正被校验的前提，必须和装饰器同提交并有独立测试。

`description`、`default` 是标注关键字，校验器继续忽略它们，不需要改动。

### 执行语义

`DecoratedTool.execute(arguments, context)`：

1. **不重复校验。** Agent loop 在调用前已经按 `definition.input_schema` 校验过参数（`runtime/loop.py:606`）。`execute` 直接绑定参数；被测试直接调用时若参数不匹配，由 Python 自然抛出 `TypeError`。
2. **参数绑定。** 按参数名从 `arguments` 取值；缺省的可选参数交给 Python 默认值；注入 `ToolExecutionContext`。`float` 参数收到 JSON 整数时转成 `float`，其余类型原样传入。
3. **返回值映射：**

   | handler 返回 | 产生的 `ToolOutput` |
   |---|---|
   | `ToolOutput` | 原样返回 |
   | `str` | `ToolOutput(content=value)` |
   | `None` | `ToolOutput(content="")` |
   | dict / list / int / float / bool（JSON 值） | `content=json.dumps(value, ensure_ascii=False, sort_keys=True)`，`structured_content=value` |
   | 其他类型、不可序列化的值 | 抛 `TypeError`，由 loop 映射为通用错误结果 |

4. **异常：**
   - `ToolError(message, *, structured_content=None)`：作者主动抛出的、**面向模型的**错误。转换为 `ToolOutput(content=message, is_error=True, structured_content=...)`。这是唯一会把异常文本交给模型的路径。
   - 其他异常原样抛出，由 `_run_tool`（`loop.py:795`）映射为 `Tool execution failed (TypeName)`。异常消息不进入模型上下文，保持现有的防泄漏行为。
   - `CancelledError` 原样传播。
   - `ToolError` 继承 `Exception`，**不**继承 `AgentError`：它表达的是工具业务结果，不是运行时错误分类。

### 公开导出

`pi_agent_py.api` 与根包新增：`tool`、`ToolError`、`DecoratedTool`。同步更新 `tests/test_api.py` 的 `_STABLE_EXPORTS`，并在 `test_tool_modules_supply_root_exports_without_package_aliases` 的同类断言里加入新对象，确认它们定义在 `tools/decorator.py`。`FunctionTool` 保留，作为不需要 schema 推导的底层适配器。

## 1.3 备选方案

1. **依赖 pydantic 生成 schema 并校验。** 拒绝作为默认：核心目前只依赖 httpx 和 PyYAML（ADR 0029 同样把 OTel 放进 extra）。pydantic 模型参数可在后续以 optional extra 支持。
2. **生成完整 JSON Schema（`anyOf`、`minimum`、`pattern` 等）。** 拒绝：校验器不执行这些关键字，模型看到的约束和运行时实际执行的约束会不一致。等校验器被替换为可插拔 `SchemaValidator`（路线图 B6 后半）后再扩展。
3. **允许同步函数并放进线程执行。** 拒绝：线程无法被取消，违反 AGENTS.md §10 的取消传播要求。
4. **模块级注册表（`@tool` 即注册）。** 拒绝：违反 §6"禁止 import 副作用注册"。
5. **所有异常都把消息交给模型。** 拒绝：异常文本可能含路径、密钥或内部细节；用显式 `ToolError` 区分"可以告诉模型"的错误。

## 1.4 后果

- 写一个工具从"定义 + schema + handler + 包装"降为一个带注解的 async 函数。
- schema 只能表达校验器能执行的约束，复杂参数仍需手写 `ToolDefinition` + `FunctionTool`。
- 校验器支持 `type` 列表，对手写 schema 也生效；原先被静默跳过的列表 `type` 现在会被执行，这是一处行为收紧，需写进 CHANGELOG。
- 工具作者必须如实声明 `side_effects`；装饰器无法检测声明不实。

## 1.5 测试清单（`tests/test_tool_decorator.py`）

- **schema 映射表**：上表每一行对应一个用例，断言生成的 schema 与期望字典完全相等。
- `required` 顺序；默认值写入 `default`；`Annotated` 描述；Optional 的 `type` 列表与 `enum` 追加 `None`。
- context 参数不进 schema，执行时收到的就是传入的 context 对象。
- 名称与描述：缺省推导、显式覆盖、非法名称、空描述。
- 装饰时拒绝：同步函数、async generator、缺注解、`*args`、`**kwargs`、仅限位置参数、`set[int]`、`tuple[int]`、`int | str`、`dict[int, str]`、自定义类、混合 `Literal`、不可序列化默认值、两个 context 参数。每个用例断言 `TypeError` 或 `ValueError`，且消息包含工具名。
- `@tool` 与 `@tool(...)` 两种写法；`.fn` 可直接 await；同名装饰两次互不影响。
- 返回值映射：`ToolOutput`、`str`、`None`、dict（content 为排序后的 JSON，structured_content 为原值）、不支持的返回值抛 `TypeError`。
- `ToolError` 转为 `is_error=True` 并保留 `structured_content`；普通异常原样抛出。
- `float` 参数收到 JSON 整数时得到 `float`。
- **经 Agent 的端到端**（用 `FakeProvider`）：
  - 模型调用装饰工具，结果提交为 tool 消息；
  - 类型错误的参数产生 `tool_validation_failed`，handler 未被调用；
  - `requires_approval=True` 的装饰工具在默认审批策略下被拒绝；
  - `side_effects="write"` 被默认 Gate 拒绝（`tool_execution_denied`）；
  - `timeout_seconds=0` 产生 timeout 结果（沿用 `test_tool_runtime.py:233` 的写法）；
  - handler 内部等待被取消时，run 以 `run_cancelled` 结束。
- **校验器**：在 `tests/test_tool_runtime.py` 增加 `type` 列表的通过、拒绝、非法列表三个用例。

## 1.6 教学交付

- `examples/34_tool_decorator.py`：离线，`FakeProvider` 驱动，展示推导出的 `definition.input_schema`（打印出来）、一次成功调用、一次 `ToolError`、一次参数校验失败。
- `tutorials/33-tool-decorator.md`：讲清楚"签名就是契约"、为什么 schema 只用校验器支持的子集、`ToolError` 与普通异常的边界、为什么拒绝同步函数、工厂闭包写法。片段从示例导入正式包。
- 更新 `docs/knowledge-map.md` 的"工具系统"行、`docs/architecture.md` 的 `tools/` 模块说明、`docs/learning-path.md`。
- `docs/upstream-mapping.md`：先用 `rg` 在 `pi/` 中查找工具定义的对应写法（例如工具注册与参数 schema 的定义位置），记录文件、符号和 submodule commit；找不到直接对应物时如实写"无直接对应"。不要凭印象填写。
- CHANGELOG 记录新增 API 和校验器行为收紧。

---

# 第二部分：B5 Middleware 链（ADR 0033）

## 2.1 背景：现有拦截点

| 拦截点 | 位置 | 能做什么 | 缺口 |
|---|---|---|---|
| `ContextStrategy` | `AgentComponents.context_strategy`，`loop.py:498` | 从完整历史选出投影 | 只有一个，不可组合 |
| `ProviderRequestHook` | `_AgentWiring.provider_request_hook`，`loop.py:514` | 改写单次请求投影 | 只由 Session 内部接线注入（`session.py:294`），standalone `Agent` 和 `AgentComponents` 都无法配置 |
| `SessionPlugin.before_provider_request` | `plugins.py:47`，经 `PluginSessionLease.prepare` 串联（`plugins.py:323`） | 同上，带 per-session 状态 | 必须走 PluginHost + Session |
| `NextTurnHook` | `AgentComponents.next_turn_hook`，`loop.py:547` | 替换下一轮的 canonical history | 会改变 durable 语义，用于压缩，不适合做投影注入 |
| 工具结果 | 无 | — | `_run_tool` 的返回值直接提交（`loop.py:651-652`），无法截断或脱敏 |

两个最常见的下游需求：

- **RAG 注入**：每次模型请求前按当前问题检索资料，注入到发给模型的消息里，但不写进 durable history。现有 `SessionPlugin.before_provider_request` 能做到，但必须组装 PluginHost 和 Session；standalone Agent 做不到。
- **工具结果截断**：Shell、HTTP、文件读取工具可能返回数 MB 文本，直接进入 durable log 和后续每一轮上下文。现在没有任何拦截点。

## 2.2 决定

### 总体形态：按阶段分型的顺序变换，不做洋葱模型

- 每个阶段一个窄 Protocol，输入一个值、返回同类型的新值；链上各项按顺序执行，前一项的输出是后一项的输入。
- **不**采用 `call_next` 洋葱式中间件。洋葱式允许中间件跳过下游（例如不执行工具直接返回结果），这会绕过 ApprovalPolicy 和 ToolExecutionGate 的安全管线，也会让"模型请求一定发出"的观测语义失效。
- 与现有 Hook 的顺序、超时、错误、事件语义保持一致（参照 `plugins.py:772` `_PluginNextTurnHook` 与 `plugins.py:323` `PluginSessionLease.prepare`）。

### 第一版只开两个阶段

| 阶段 | 发生时机 | 输入 → 输出 | 是否 durable |
|---|---|---|---|
| `provider_request` | `ContextStrategy` 之后、Session 插件之前、发送模型请求之前 | `(ProviderRequestContext, ContextPlan) → ContextPlan` | 否，只影响本次请求投影 |
| `tool_result` | `_run_tool` 返回之后、提交 tool 消息之前 | `(ToolResultContext, ToolOutput) → ToolOutput` | **是**，变换后的结果就是被提交的事实 |

明确推迟的阶段（各自需要独立 ADR）：

- `run_start`（prompt 预处理、记忆写入）：RAG 已由 `provider_request` 覆盖；改写用户输入会让 durable 的 user 消息与用户实际输入不一致，需要单独设计来源标注。
- `assistant_message`（输出校验、guardrail）：校验失败后的处理（重试、替换、终止）需要和 Provider resilience 及请求计数一起设计。
- `tool_call`（执行前改写参数）：与"审批参数 = 执行参数"的快照不变量冲突（`loop.py:624-649`、ADR 0014）。要做必须放在校验和审批之前，并重新走审批。

### 类型定义（Definition）

`domain.py` 新增：

```python
@dataclass(frozen=True, slots=True)
class ToolResultContext:
    """Identity and outcome of one executed tool call before its result commits."""
    execution: ToolExecutionContext   # call、definition、request_number、call_index、session_id、approval
    outcome: str                      # _run_tool 的 outcome：success / error / timeout /
                                      # execution_denied / sandbox_timeout / sandbox_error
    run_id: str | None = None
    turn_id: str | None = None
```

`ports.py` 新增（`provider_request` 阶段直接复用已有的 `ProviderRequestHook`，`ports.py` 中已定义并已导出）：

```python
class ToolResultHook(Protocol):
    async def transform(self, context: ToolResultContext, output: ToolOutput) -> ToolOutput:
        """Return the tool result that will be committed and shown to the model."""
        ...
```

新模块 `runtime/middleware.py`：

```python
@dataclass(frozen=True, slots=True)
class Middleware:
    name: str
    provider_request: ProviderRequestHook | None = None
    tool_result: ToolResultHook | None = None
    priority: int = 0
    timeout_seconds: float | None = None
```

- 一个 `Middleware` 可以同时实现两个阶段（例如 RAG 注入 + 引用标注），共享同一个名字、优先级和超时。
- 构造校验：`name` 非空；至少提供一个阶段；`priority` 是 `int` 且不是 `bool`；`timeout_seconds` 为 `None` 或有限的非负数。违反时抛 `ValueError` / `TypeError`。
- `errors.py` 新增 `MiddlewareError(AgentError)`，并在 `errors.py:207` `_error_category` 的映射表中加入 `(MiddlewareError, "middleware")`，使 `run_failed.error_category` 稳定为 `"middleware"`。在 `tests/test_minimal_loop.py` 或新测试中断言该类别。

### 组装（Consumer 与 Lifecycle）

| 来源 | 新增 API | 语义 |
|---|---|---|
| `AgentComponents` | 字段 `middleware: tuple[Middleware, ...] = ()` | 构造时校验名称唯一（重复抛 `ValueError`）；`_ComponentChanges` 同步加字段 |
| `AgentProfile` | 字段 `middleware_factories: tuple[Callable[[], Middleware], ...] = ()` | `build_components()` 中逐个调用一次 |
| `AgentRuntimeBuilder` | `with_middleware(middleware: Sequence[Middleware])` | 与 `with_tools` 一致：**替换** Profile 的 middleware，跳过原 factory |
| `PluginAPI` | `register_middleware(middleware: Middleware) -> Registration` | staging 后原子发布；与已注册名称冲突时拒绝整个插件（同 `register_tool`）；`unregister` 撤销 |
| `PluginCapabilities` | 字段 `middleware: tuple[Middleware, ...]` | Builder 在 `builder.py:362` 合并工具处一并追加到 `components.middleware` 之后；合并后重名 → `RuntimeBuildError` |
| `SessionSpec._matches` | 处理 `middleware` 字段 | 与 `tools` 相同：逐项按实例身份比较（`runtime/session_spec.py:33`） |

`SessionPlugin.before_provider_request` **保留不变**。选择规则写进教程：

- 无状态或跨 Session 共享状态 → `Middleware`（standalone Agent 也能用）；
- 需要 per-session 状态和关闭生命周期 → `SessionPlugin`。

Middleware 没有自己的 close。需要释放的资源由创建者管理：Profile/Builder 路径由应用管理，插件路径用已有的 `register_cleanup`。

### 执行顺序

- 某个阶段的链 = `components.middleware` 中实现了该阶段的项，按 `(priority, 在 tuple 中的位置)` **升序**执行。这与 PluginHost 的排序键 `(item.priority, item.order)` 一致（`plugins.py:589`）。
- `provider_request` 阶段的完整顺序：

  ```text
  ContextStrategy.prepare
    → components.middleware 的 provider_request 链
    → _AgentWiring.provider_request_hook（Session 插件 lease）
    → context_prepared 事件 → 发送模型请求
  ```

  `context_prepared` 继续在全部变换之后发出，因此 `selected_message_count` 反映最终发给模型的消息数。

### 通用执行语义（两个阶段共用）

链的执行器放在 `runtime/middleware.py`（私有类，例如 `_MiddlewareChain`），在 `Agent.__init__` 中从 `components.middleware` 构建一次。Agent loop 只调用两个方法，循环本身只增加两处调用：

- `loop.py:_prepare_context`：在 `source_message_count` 校验（`loop.py:510-513`）之后、`hook = self._wiring.provider_request_hook`（`loop.py:514`）之前；
- `loop.py:_execute_tool_calls`：`output = await self._run_tool(...)`（`loop.py:651`）与 `_commit_tool_result`（`loop.py:652`）之间。`_run_tool` 需改为同时返回 `outcome`。

每一项 middleware 的调用：

1. 传入值先 `deepcopy`，防止实现者原地修改共享对象（`Message.structured_content`、`ToolCall.arguments` 是可变 JSON）。与 `plugins.py:345` 做法一致。
2. `timeout_seconds` 不为 `None` 时用 `asyncio.timeout` 包裹。
3. 发出 capability 事件：`middleware_started`，然后是 `middleware_finished` / `middleware_failed` / `middleware_cancelled` 之一。payload 包含 `middleware_name`、`stage`，`failed` 另带 `error=type(error).__name__`，`tool_result` 阶段另带 `tool_call_id`、`tool_name`。envelope 带 `span_id` 和 `duration_ms`。**payload 不得包含消息正文或工具输出。**
4. `CancelledError` 原样传播。
5. 异常、超时、返回值违反契约一律视为失败。错误消息不得包含原异常文本，原异常放进 `__cause__`。

### `provider_request` 阶段

- **契约**：必须返回 `ContextPlan`；`source_message_count` 必须等于输入值（与 `loop.py:530-533` 对 Session hook 的约束相同）。
- **失败**：抛 `MiddlewareError`，run 失败（`run_failed`）。理由：投影不完整或未经过滤就发出请求，可能缺少必需的上下文或泄露应被移除的内容，所以 fail closed。
- **不 durable**：注入的消息只存在于本次请求，不进 `Agent.messages`、不进 SessionStore、replay 不重建。RAG 结果必须能在每次请求重新算出。
- **每次模型请求都会执行**，包括工具循环中的每一轮。需要按 run 缓存检索结果的实现自行以 `context.run_id` 为键缓存。
- **不覆盖压缩请求**：压缩摘要由 `CompactionStrategy.summarize` 直接调用 Provider（`runtime/compaction_orchestration.py:247`），不经过 `_prepare_context`。要写进 ADR 和教程，避免使用者以为脱敏 middleware 也作用于摘要请求。
- **调用方责任**：注入后不得拆开 tool call 与 tool result 的配对，也要注意部分供应商对 system 消息位置有要求。v1 不在运行时检查这两点，只在教程中说明，并在示例里演示"插在开头的 system 消息之后"的安全写法。

### `tool_result` 阶段

- **作用范围**：每个经 `_run_tool` 返回的结果都会经过此链，包括工具成功、工具返回错误、超时、Gate 拒绝、沙箱错误（`ToolResultContext.outcome` 区分）。以下由运行时生成的固定文本**不**经过此链：工具不存在、参数校验失败、审批拒绝、`finish_reason="length"` 时未执行的调用（`loop.py:589-641`、`816`），以及 Session 恢复时补写的中断结果（`session.py:349`）。
- **契约**：必须返回 `ToolOutput`；`is_error` 必须与输入一致，不允许把失败改成成功，也不允许反过来。
- **durable**：链的最终输出就是提交到 SessionStore 的 tool 消息，replay、fork 和后续每轮上下文都看到变换后的内容。原始输出不会被持久化。需要保留完整输出的实现要自己写到外部存储，并把引用放进 `structured_content`（参照 `ShellCollectedOutput.spill_path` 的做法）。
- **失败**：**不**提交原始输出（若这是脱敏 middleware，原文可能含敏感内容），改为提交一条固定的错误结果，run 继续执行：

  ```python
  ToolOutput(
      content=(
          "The tool ran, but its result could not be processed. "
          "The outcome is unknown; do not assume it failed and do not retry automatically."
      ),
      is_error=True,
      structured_content={
          "status": "middleware_failed",
          "middleware": "<失败的 middleware 名称>",
          "tool_outcome": "<原 outcome>",
      },
  )
  ```

  理由：工具的外部副作用已经发生，让 run 失败并不能撤销副作用，反而会让模型失去"结果未知"这一信息。措辞与 `_INTERRUPTED_TOOL_RESULT` 保持同一立场。
- **观测**：链非空时，每个调用在提交前发出一次 `tool_result_transformed`（capability），payload：`tool_call_id`、`tool_name`、`middleware_names`（实际执行的名称列表）、`changed`（布尔）、`original_content_chars`、`final_content_chars`。不含正文。

### 内置实现（Provider，满足 §7 "至少一个实现"）

`tools/result_truncation.py`：

```python
@dataclass(frozen=True, slots=True)
class TruncateToolResults:
    """A ToolResultHook that bounds ToolOutput.content by characters."""
    max_chars: int
    keep: Literal["head", "tail", "head_tail"] = "head_tail"

    async def transform(self, context: ToolResultContext, output: ToolOutput) -> ToolOutput: ...
```

- `max_chars` 必须是 `int` 且 `>= 64`，否则抛 `ValueError`。
- `len(output.content) <= max_chars` 时原样返回同一个对象（便于 `changed=False`）。
- 超出时插入标记 `\n[... truncated {omitted} characters ...]\n`，最终 `len(content) <= max_chars`（标记计入长度）。`head_tail` 时首尾各取一半剩余空间，首部向下取整。
- 只处理 `content`；`structured_content` 与 `is_error` 不变。按字符计数，不按字节，教程中说明这与 Shell 的每流字节预算是两回事。
- 使用方式：`Middleware("tool-result-truncation", tool_result=TruncateToolResults(8000))`。

RAG 不提供内置实现，因为它依赖外部检索后端，只在示例中给出一个基于假检索器的实现。

### 可观测性接线

- `domain.EventKind` 增加：`middleware_started`、`middleware_finished`、`middleware_failed`、`middleware_cancelled`、`tool_result_transformed`。
- `observability/otel.py`：在 `_START` 中加 `"middleware_started": "middleware"`，在 `_END` 中加另外三个终态（参照 `plugin_hook_*` 的写法，`otel.py:18-31`）。
- `RedactingEventSink` 无需修改，因为 payload 不含正文；但要加一条测试断言脱敏后的记录里仍没有正文字段。

## 2.3 备选方案

1. **洋葱式 `call_next` 中间件。** 拒绝，理由见 2.2。可以绕过安全管线，观测语义也不再确定。
2. **一个大而全的 `Middleware` Protocol，每个阶段一个可选方法。** 拒绝：Protocol 上的可选方法要靠 `hasattr` 探测，类型检查无法发现拼写错误。改用"值对象 + 每阶段一个窄 Protocol"。
3. **把 `NextTurnHook` 和 `SessionPlugin.before_provider_request` 并入 middleware。** 本期不做。前者改变 durable 历史，后者带 per-session 生命周期，语义不同；强行合并会让一个阶段同时承担两种语义。
4. **`tool_result` 失败时让 run 失败。** 拒绝，理由见上文"失败"一条。
5. **`tool_result` 失败时提交原始输出。** 拒绝：若 middleware 负责脱敏，这会泄露原文。

## 2.4 后果

- RAG 注入可以用一个 `Middleware` 完成，standalone Agent、Session、Runtime 三条路径都可用。
- 工具输出在进入 durable log 之前就有了统一的截断和脱敏点。
- `tool_result` middleware 会改变持久化的事实，所以它的实现错误会被永久记录。教程必须强调：这里的变换要确定、可解释。
- `AgentComponents` 新增字段，需要同步更新三处字段覆盖测试和 `SessionSpec._matches`（见下文）。
- 推迟的三个阶段在 ADR 0033 的"后续工作"中列出，不在本期实现。

## 2.5 测试清单

`tests/test_middleware.py`：

- **构造**：空名称、没有任何阶段、`priority=True`、负数或无穷大超时；`AgentComponents` 中 middleware 重名。
- **`provider_request`**：
  - 注入的消息出现在 `FakeProvider.requests` 中，但不在 `Agent.messages`、`store.read_events()` 中；
  - 工具循环中每次模型请求各执行一次（两轮请求 → 两次调用，`context.request_number` 依次为 1、2）；
  - 按 `(priority, 位置)` 升序执行，后一项看到前一项的输出；
  - 实现原地修改输入不会影响 Agent 历史（deepcopy 生效）；
  - 返回非 `ContextPlan`、改变 `source_message_count`、抛异常、`timeout_seconds=0` 超时：均抛 `MiddlewareError`，run 以 `run_failed` 结束，`__cause__` 保留原异常，错误消息不含原异常文本；
  - 取消传播：run 以 `run_cancelled` 结束，并发出 `middleware_cancelled`；
  - 与 `SessionPlugin.before_provider_request` 同时存在时，component middleware 先执行；
  - `session.compact()` 期间 middleware 不被调用；
  - standalone `Agent`（无 Session）可用。
- **`tool_result`**：
  - `TruncateToolResults` 生效后，durable log 中的 tool 消息是截断后的内容，下一次模型请求看到的也是截断后的内容；
  - 链按顺序执行；
  - 翻转 `is_error`、抛异常、超时：提交固定的 `middleware_failed` 结果，原始内容在 durable log 中**任何位置都不存在**，run 继续并完成下一轮请求；
  - 取消：异常传播，该调用没有提交任何 tool 消息；随后重新打开同一 Session 时，由已有的恢复逻辑补写中断结果；
  - `outcome` 正确传入：成功、工具错误、`timeout_seconds=0` 超时、Gate 拒绝四种情况各一个用例；
  - 工具不存在、参数校验失败、审批拒绝三种情况下 middleware **不**被调用；
  - `tool_result_transformed` 的 payload 包含字符计数，`changed` 正确，不包含正文。
- **事件**：每个 `middleware_started` 都有配对的终态事件，`span_id` 相同且带 `duration_ms`；经 `RedactingEventSink` 后仍不含正文。
- **组装**：
  - `AgentProfile.middleware_factories` 每个只调用一次；
  - `Builder.with_middleware` 替换 Profile middleware 且跳过原 factory；
  - `PluginAPI.register_middleware`：名称冲突时整个插件加载回滚，`unregister` 后不再执行；
  - 插件 middleware 追加在 Builder middleware 之后；
  - SessionSpec：同一 middleware 实例视为相同规格，不同实例视为冲突。

`tests/test_result_truncation.py`：未超限时返回同一对象；`head`、`tail`、`head_tail` 三种模式的精确输出；最终长度不超过 `max_chars`；标记中的省略字符数正确；`max_chars < 64` 被拒绝；`structured_content`、`is_error` 不变。

`tests/downstream_middleware/`（src 之外的包，对应路线图阶段 3 的验收标准）：一个只 import `pi_agent_py.api` 的 RAG middleware，加一个测试证明它能通过 `AgentRuntimeBuilder.with_middleware` 和 `PluginAPI.register_middleware` 两条路径接入，且不修改 `src/`。

**必须同步修改的已有测试**（新增 `AgentComponents` 字段后它们会失败，这是预期的守护机制）：

- `tests/test_app_runtime.py:93` `test_component_fields_are_consumed_through_the_builder`：加入 `middleware` 的观测；
- `tests/test_session_spec.py:289` 字段覆盖断言：加入 `middleware` 的变更用例；
- `tests/test_api.py` 的 `_STABLE_EXPORTS`：加入 `Middleware`、`MiddlewareError`、`ToolResultContext`、`ToolResultHook`、`TruncateToolResults`；
- `tests/test_otel.py`：middleware span 的父子关系和终态。

## 2.6 教学交付

- `examples/35_middleware.py`：离线，`FakeProvider` 驱动。一个假检索器做 RAG 注入，一个 `TruncateToolResults(200)` 截断一个返回长文本的 `@tool` 工具。运行后打印：模型实际收到的消息、durable log 中的消息、`middleware_*` 和 `tool_result_transformed` 事件。三者的对比就是这一章的核心观察点。
- `tutorials/34-middleware.md`：讲清楚
  - 为什么用顺序变换而不是洋葱模型；
  - 投影变换与 durable 变换的区别（`provider_request` 对比 `tool_result`）；
  - 两个阶段为什么采用不同的失败策略；
  - Middleware、SessionPlugin、NextTurnHook、ContextStrategy 各自负责什么，配一张选择表；
  - 推迟的三个阶段及推迟原因。
- 更新 `docs/architecture.md`：Agent loop 阶段列表和新模块说明；`docs/knowledge-map.md` 新增"Middleware 链"一行；`docs/learning-path.md`；`docs/roadmap.md` 的 B5、B6 标注完成的 commit。
- `docs/upstream-mapping.md`：用 `rg` 在 `pi/` 中查找扩展、钩子或事件拦截的对应实现，记录文件、符号和 submodule commit；没有对应物时如实说明。
- CHANGELOG 记录新增 API；`AgentComponents` 新字段有默认值，不属于破坏性变更。

---

## 3. 完成定义（两部分共用）

逐条对照 `AGENTS.md` §15，提交前全部满足：

- [ ] 默认测试套件离线通过：`python -m unittest discover -s tests`
- [ ] CI 范围的 `ruff check src tests` 和 `mypy` 通过（版本见 `pyproject.toml` 的 dev extra）
- [ ] 新示例可离线运行，旧示例全部仍可运行
- [ ] 正常路径、失败路径、取消路径都有测试
- [ ] 不依赖真实模型即可复现全部行为
- [ ] 新事件在 OTel 映射和脱敏中有测试覆盖，payload 不含正文
- [ ] 没有 import 副作用注册，没有进程级可变状态
- [ ] ADR 0033、0034 已从本文提取，包含背景、决定、备选方案、后果、验证方式
- [ ] 教程、knowledge-map、upstream-mapping、architecture、CHANGELOG 已更新
- [ ] `git diff --check` 干净；工作区只包含本次意图的改动

## 4. 需要人工确认的决定

以下决定已按推荐方案写进本文。若需改动，在交给 Codex 之前修改对应章节：

1. `tool_result` 失败时提交固定错误结果并让 run 继续，而不是让 run 失败（2.2）。
2. 装饰工具返回 `None` 时提交空字符串，而不是报错（1.2）。
3. 第一版只开放 `provider_request` 与 `tool_result` 两个阶段（2.2）。
4. `with_middleware` 与 `with_tools` 一致，采用"替换 Profile 默认值"语义，而不是追加（2.2）。
