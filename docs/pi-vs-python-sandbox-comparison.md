# Pi vs Python-Pi-Agent 沙箱设计对比

本文档对比 **Pi 项目**（TypeScript）和 **Python-Pi-Agent** 的沙箱设计。

参考 Pi commit: `56b25ff4e` (docs(coding-agent): align documented message types)

---

## 核心差异总览

| 维度 | Pi (TypeScript) | Python-Pi-Agent |
|------|----------------|-----------------|
| **沙箱类型** | 语言级沙箱（QuickJS VM） | 进程级沙箱（Docker 容器） |
| **隔离对象** | JavaScript 代码 | Shell 命令 |
| **隔离强度** | 中等（语言运行时限制） | 强（操作系统级别隔离） |
| **启动开销** | 极小（毫秒级） | 较大（秒级） |
| **适用场景** | 执行模型生成的 JS 代码片段 | 执行文件操作、编译、系统命令 |
| **网络隔离** | 默认无网络 API | Docker `--network none` |
| **文件访问** | 无文件系统访问 | 挂载特定 workspace |
| **跨平台** | 完全一致（纯 JS） | 依赖 Docker（Windows 体验差异） |

---

## Pi 的沙箱设计：QuickJS VM + Worker Thread

### 1. 架构概览

```
Pi 沙箱架构：

┌─────────────────────────────────────┐
│   CodemodeSandbox (Host Process)    │
│   - 工具注册表                       │
│   - 配置管理                         │
│   - 生命周期管理                     │
└─────────────┬───────────────────────┘
              │
              │ Worker Threads
              │
    ┌─────────▼──────────┐
    │   Worker Thread    │
    │  ┌──────────────┐  │
    │  │  QuickJS VM  │  │  ← JavaScript 沙箱
    │  │  用户脚本     │  │
    │  └──────────────┘  │
    └────────────────────┘
```

**关键组件**：

1. **`CodemodeSandbox`**：宿主侧的沙箱管理器
   - 位置：`packages/codemode/src/runtime/host.ts`
   - 职责：工具注册、执行调度、生命周期管理

2. **Worker Thread**：Node.js 的工作线程
   - 每次执行创建新的 Worker
   - 执行完成后销毁（避免状态污染）

3. **QuickJS VM**：轻量级 JavaScript 引擎（WebAssembly）
   - 在 Worker 内运行
   - 完全独立的 JS 环境，无 Node.js API

### 2. 核心代码解读

#### 2.1 沙箱接口

```typescript
export interface CodemodeSandboxOptions {
    tools?: CodemodeTool[];           // 可调用的工具
    globals?: CodemodeTool[];         // 全局函数（如 console）
    timeoutMs?: number;               // 超时时间（默认 5 分钟）
    memoryLimitBytes?: number;        // 内存限制
    wasm?: CodemodeWasmModule;        // QuickJS WebAssembly 模块
    workerUrl?: string | URL;         // Worker 脚本路径
}

export class CodemodeSandbox {
    execute(code: string, options: CodemodeExecuteOptions): Promise<CodemodeResult>
    close(): Promise<void>
}
```

**对比 Python-Pi-Agent**：

```python
# Python-Pi-Agent 的接口
class ProcessSandbox(Protocol):
    async def run(
        self,
        request: SandboxProcessRequest,  # 命令 + 参数
    ) -> SandboxProcessResult:
        ...
```

**差异**：
- Pi 执行的是 **JavaScript 代码字符串**
- Python-Pi-Agent 执行的是 **精确的命令参数**

#### 2.2 执行流程

**Pi 的执行流程**：

```typescript
// 来自 packages/codemode/src/runtime/host.ts

class Execution {
    constructor(options: ExecutionOptions) {
        // 1. 创建 SharedArrayBuffer 用于中断信号
        this.interrupt = new SharedArrayBuffer(4);
        
        // 2. 设置超时定时器
        if (Number.isFinite(options.timeoutMs)) {
            this.timer = setTimeout(() => {
                this.finish({ 
                    kind: "timeout", 
                    message: `Execution timed out after ${options.timeoutMs} ms` 
                });
            }, options.timeoutMs);
        }
        
        // 3. 加载 QuickJS WASM 模块
        options.wasm.then(
            (wasm) => this.start(options, wasm),
            (error) => {
                this.finish({ 
                    kind: "sandbox", 
                    message: `Failed to load QuickJS: ${error}` 
                });
            }
        );
    }
    
    private start(options: ExecutionOptions, wasm: CodemodeWasmModule): void {
        // 4. 创建 Worker Thread
        const worker = new Worker(options.workerUrl, { 
            workerData: {
                code: options.code,        // 用户脚本
                tools: [...],              // 可用工具
                wasm: wasm,                // QuickJS 模块
                memoryLimitBytes: ...,     // 内存限制
                interrupt: this.interrupt  // 中断信号
            }
        });
        
        // 5. 监听 Worker 消息
        worker.on("message", (msg) => this.handleMessage(msg));
        worker.on("error", (err) => this.finish({ kind: "sandbox", ... }));
        worker.on("exit", (code) => this.finish({ kind: "sandbox", ... }));
    }
    
    private finish(error?: CodemodeError, value?: unknown): void {
        // 6. 清理资源
        clearTimeout(this.timer);
        
        // 7. 设置中断标志（通知 QuickJS 停止）
        Atomics.store(new Int32Array(this.interrupt), 0, 1);
        
        // 8. 终止 Worker
        this.worker?.terminate();
    }
}
```

**对比 Python-Pi-Agent 的流程**：

```python
# python-pi-agent/src/pi_agent_py/sandbox.py

class DockerProcessSandbox:
    async def run(self, request: SandboxProcessRequest):
        # 1. 构建 Docker 命令
        container_name = f"pi-agent-{uuid4().hex}"
        arguments = self.build_command(request, container_name)
        
        # 2. 启动 Docker 进程
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        # 3. 等待执行（带超时）
        try:
            async with asyncio.timeout(request.timeout_seconds):
                stdout, stderr, ... = await _capture_output(process, ...)
        except TimeoutError:
            # 4. 超时清理
            await self._stop_container(container_name, process)
            raise SandboxTimeoutError(...)
        
        # 5. 返回结果
        return SandboxProcessResult(...)
```

**差异总结**：

| 步骤 | Pi | Python-Pi-Agent |
|------|----|----|
| **创建隔离环境** | 启动 Worker Thread + QuickJS VM | 启动 Docker 容器 |
| **传递代码/命令** | 代码字符串通过 `workerData` | 命令参数通过 `docker run` |
| **超时机制** | `setTimeout` + `SharedArrayBuffer` 中断 | `asyncio.timeout` + `docker rm` |
| **资源限制** | QuickJS 内存限制 | Docker cgroups（CPU、内存、进程数） |
| **清理** | `worker.terminate()` | `docker rm --force` + `process.kill()` |

### 3. Pi 的隔离边界

#### 3.1 隔离了什么？

**Pi 使用 QuickJS，隔离的是 JavaScript 运行时**：

```typescript
// Worker 内的脚本只能访问：
const availableGlobals = {
    tools,           // 注册的工具
    ALL_TOOLS,       // 工具列表
    console,         // 受限的 console
    text,            // 输出文本
    image,           // 输出图片
    exit,            // 提前退出
    store,           // 持久化 KV
    load,            // 读取 KV
    globalThis,      // 有限的全局对象
};

// 不能访问：
// - Node.js API (fs, http, child_process, ...)
// - 定时器 (setTimeout, setInterval)
// - fetch / XMLHttpRequest
// - require / import
// - process, Buffer, __dirname, ...
```

**文件系统隔离**：
- ❌ **没有文件系统访问**
- 脚本无法读写文件
- 需要文件操作时，通过 `tools` 调用宿主提供的工具

**网络隔离**：
- ❌ **没有网络 API**
- 无 `fetch`、`XMLHttpRequest`
- 需要网络请求时，通过 `tools` 调用

**进程隔离**：
- ✅ **Worker Thread 级别隔离**
- 共享主进程的内存空间（V8 堆）
- 但有独立的事件循环

#### 3.2 对比 Python-Pi-Agent 的隔离

**Python-Pi-Agent 使用 Docker，隔离的是整个操作系统环境**：

```python
# Docker 参数：
docker run \
    --cap-drop ALL \              # 删除所有 Linux 能力
    --security-opt no-new-privileges \
    --read-only \                 # 根文件系统只读
    --network none \              # 禁止网络
    --pids-limit 128 \            # 进程数限制
    --memory 512m \               # 内存限制
    --cpus 1.0 \                  # CPU 限制
    --mount type=bind,source=/host/workspace,target=/workspace \
    --workdir /workspace \
    python:3.12-slim \
    bash -c "your-command"
```

**文件系统隔离**：
- ✅ **只能访问挂载的 workspace**
- 看不到宿主机的其他文件
- 根文件系统只读，不能修改系统文件

**网络隔离**：
- ✅ **Docker `--network none`**
- 容器内没有网络接口
- 所有网络请求都会失败

**进程隔离**：
- ✅ **完全独立的进程树**
- 容器内进程看不到宿主进程
- cgroups 限制资源使用

### 4. 工具调用机制

#### 4.1 Pi 的工具调用

**定义工具**：

```typescript
// packages/codemode/src/types.ts
export interface CodemodeTool {
    name: string;
    description?: string;
    inputSchema?: CodemodeJsonSchema;   // JSON Schema
    outputSchema?: CodemodeJsonSchema;
    execute(args: unknown, context: CodemodeToolContext): Promise<unknown>;
}
```

**工具注册**：

```typescript
const sandbox = new CodemodeSandbox({
    tools: [
        {
            name: "read_file",
            description: "读取文件内容",
            inputSchema: { type: "object", properties: { path: { type: "string" } } },
            async execute(args: any) {
                return await fs.readFile(args.path, "utf-8");
            }
        }
    ]
});
```

**脚本内调用**：

```javascript
// 用户脚本（在 QuickJS VM 内）
const content = await tools.read_file({ path: "README.md" });
console.log(content);
```

**通信机制**：

```
QuickJS VM (Worker)          →  Host Process
1. 脚本调用 tools.read_file
2. 发送消息到 Host            { type: "call", id: 1, name: "read_file", args: "{...}" }
3. Host 执行真实的文件读取
4. 返回结果                  { type: "result", id: 1, ok: true, payload: "..." }
5. QuickJS 恢复脚本执行
```

#### 4.2 Python-Pi-Agent 的工具调用

**定义工具**：

```python
# python-pi-agent/src/pi_agent_py/ports.py
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition:
        ...
    
    async def execute(
        self,
        arguments: JSONObject,
        context: ToolExecutionContext,
    ) -> ToolOutput:
        ...
```

**BashTool 示例**：

```python
class BashTool(Tool):
    def __init__(self, executor: ShellExecutor):
        self._executor = executor
    
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            description="执行 Bash 命令",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            side_effects="write",
        )
    
    async def execute(self, arguments: JSONObject, context: ToolExecutionContext):
        request = ShellExecRequest(command=arguments["command"])
        spec = self._executor.resolve(request)
        result = await self._executor.execute(spec)
        return ToolOutput(content=result.stdout)
```

**工具调用链**：

```
Agent Runtime
  ↓
ToolExecutionGate（权限检查）
  ↓
BashTool.execute
  ↓
ShellExecutor.execute
  ↓
ProcessSandbox.run（Docker）
  ↓
容器内执行命令
```

**差异**：

| 维度 | Pi | Python-Pi-Agent |
|------|----|----|
| **工具位置** | 宿主进程（Node.js） | 宿主进程（Python） |
| **脚本位置** | 沙箱内（QuickJS VM） | N/A（模型生成 ToolCall） |
| **通信方式** | Worker 消息传递 | 直接函数调用 |
| **隔离对象** | 脚本调用工具的过程 | 工具内部的命令执行 |

---

## 设计哲学对比

### Pi 的设计哲学

**目标**：让模型生成的 **JavaScript 代码** 安全执行

**场景**：
- Codemode：模型生成代码来处理数据、调用工具
- 类似 Jupyter Notebook 的交互式编程

**隔离层次**：
```
用户请求
  ↓
模型生成 JavaScript 代码
  ↓
CodemodeSandbox 执行代码（QuickJS VM）
  ↓
代码调用 tools.xxx()
  ↓
宿主进程执行工具（文件、网络等真实操作）
```

**优势**：
- 模型可以写复杂逻辑（循环、条件、数据处理）
- 启动极快（毫秒级）
- 跨平台一致

**局限**：
- 只能隔离 JavaScript 代码
- 无法隔离工具本身（工具在宿主进程）
- 不适合运行任意系统命令

### Python-Pi-Agent 的设计哲学

**目标**：让模型调用的 **Shell 命令** 安全执行

**场景**：
- Coding Agent：文件操作、编译、测试、Git 等
- 类似 ChatGPT Code Interpreter

**隔离层次**：
```
用户请求
  ↓
模型生成 ToolCall（命令字符串）
  ↓
Agent 调用 BashTool
  ↓
ShellExecutor 构造命令
  ↓
ProcessSandbox 在 Docker 容器内执行
```

**优势**：
- 可以运行任意系统命令（bash、python、git、make...）
- 操作系统级隔离（文件、网络、进程）
- 资源限制完善（cgroups）

**局限**：
- 启动较慢（秒级）
- 依赖 Docker（Windows 体验差）
- 不能执行复杂的宿主侧逻辑（模型只能调用预定义工具）

---

## 混合方案：Pi 的 Coding Agent

Pi 项目实际上**同时使用了两种沙箱**：

### 1. CodemodeSandbox（语言级）

用于执行模型生成的 JavaScript 代码片段。

### 2. Bash/Docker 沙箱（进程级）

Pi 的 Coding Agent 也有类似 Python-Pi-Agent 的 Docker 沙箱！

**位置**：`packages/coding-agent/src/bun/sandbox-env-setup.ts`

**关键代码**（简化）：

```typescript
// Pi 也会运行 Docker 来隔离 Shell 命令
async function executeBashInDocker(command: string) {
    const result = await exec([
        "docker", "run",
        "--rm",
        "--network", "none",
        "--read-only",
        "--mount", `type=bind,source=${workspace},target=/workspace`,
        "--workdir", "/workspace",
        "debian:stable-slim",
        "bash", "-c", command
    ]);
    return result;
}
```

**所以 Pi 实际上是**：
- **CodemodeSandbox**：隔离模型生成的 JavaScript 逻辑
- **Docker Sandbox**：隔离 Bash 命令执行

---

## 总结：何时用哪种沙箱？

### 使用语言级沙箱（QuickJS / Deno / WASM）当：

✅ 需要执行**模型生成的代码逻辑**（循环、条件、数据处理）  
✅ 启动速度很重要（毫秒级）  
✅ 只需要隔离脚本，工具可以在宿主运行  
✅ 跨平台一致性重要  

**典型场景**：
- 数据处理脚本（类似 Jupyter）
- 简单的业务逻辑生成
- 插件系统（不信任的插件代码）

### 使用进程级沙箱（Docker / VM）当：

✅ 需要执行**任意系统命令**（bash、编译器、测试工具）  
✅ 需要**操作文件系统**（读写项目代码）  
✅ 需要**强隔离**（不信任的代码仓库）  
✅ 资源限制很重要（防止 fork 炸弹、内存爆炸）  

**典型场景**：
- Coding Agent（文件编辑、编译、测试）
- CI/CD 任务
- 代码执行服务（用户提交的任意代码）

### 你的项目当前选择

**Python-Pi-Agent 选择了进程级沙箱（Docker）**，因为：

1. **目标是 Coding Agent**：需要文件操作、编译、测试
2. **教学清晰**：Docker 的隔离边界更明确、更容易理解
3. **与上游对齐**：Pi 的 Coding Agent 也用 Docker

**如果未来需要语言级沙箱**：
- 可以增加一个 `PythonCodeSandbox`（用 RestrictedPython 或 Pyodide）
- 用于执行模型生成的 Python 数据处理脚本
- 保持接口一致（`ProcessSandbox` 协议）

---

## 学习建议

基于你的情况（已经理解 Python-Pi-Agent 的沙箱），建议：

1. **快速浏览 Pi 的 CodemodeSandbox 源码**（15 分钟）
   - `packages/codemode/src/runtime/host.ts`
   - 重点看 `Execution` 类的生命周期

2. **运行 Pi 的 Codemode 示例**（30 分钟）
   - 看看 QuickJS VM 是怎么执行的
   - 观察工具调用的消息传递

3. **对比两种沙箱的取舍**（15 分钟）
   - 什么场景用语言级？
   - 什么场景用进程级？
   - 能否结合使用？

理解了这两种沙箱，你就掌握了主流 Agent 系统的核心安全机制！
