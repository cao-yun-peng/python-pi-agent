# 顶层 YAML、`.env` 与脱敏配置诊断

这一节解决的是应用组装边界，而不是 Provider 协议本身：模型、URL 和 thinking 等非秘密配置放进仓库顶层 YAML；API key 只通过环境变量或被 Git 忽略的 `.env` 进入进程；内部 Provider 仍只接收已经验证过的强类型配置对象。

## 文件职责

仓库根目录提供两个模板：

```text
agent.yaml.example  # 可审查的非秘密配置
.env.example        # 只声明变量名，不含真实值
```

首次使用：

```powershell
Copy-Item .\agent.yaml.example .\agent.yaml
Copy-Item .\.env.example .\.env
```

`.gitignore` 已忽略 `.env` 和 `.env.*`，但保留 `.env.example`。`agent.yaml` 不应包含 `api_key`；加载器遇到内联密钥会直接拒绝。

Kimi 示例：

```yaml
version: 1
provider:
  name: kimi
  api_key_env: MOONSHOT_API_KEY
  model: kimi-k2.6
  base_url: https://api.moonshot.cn/v1
  timeout_seconds: 120
  thinking: enabled
  thinking_keep: all
  max_tokens: 16000
```

切换到 DeepSeek 时使用：

```yaml
version: 1
provider:
  name: deepseek
  api_key_env: DEEPSEEK_API_KEY
  model: deepseek-flash
  thinking: disabled
```

切换到 GLM 标准 API 时使用：

```yaml
version: 1
provider:
  name: glm
  api_key_env: ZAI_API_KEY
  model: glm-5.3
  base_url: https://open.bigmodel.cn/api/paas/v4
  thinking: enabled
  reasoning_effort: high
```

切换到 TiMi CC 的 Codex/GPT/Grok Responses API 时使用：

```yaml
version: 1
provider:
  name: timicc
  api_key_env: TIMI_CC_API_KEY
  model: gpt-6-astra
  base_url: https://timicc.com/v1
  reasoning_effort: high
  reasoning_summary: auto
  max_output_tokens: 16000
```

TiMi 的 key 分组决定可用模型，因此 `model` 没有默认值，必须按控制台实际授权显式填写。

## 两阶段边界

`parse_app_config()` 只读取 YAML，返回不含秘密的 `AppConfigSpec`。这一步会检查：

- `version` 必须为整数 `1`；
- Provider 和字段名必须在白名单中；
- YAML 不得出现重复键；
- 数字、字符串和环境变量名称必须类型正确；
- Kimi/DeepSeek/GLM/TiMi 的模型能力组合必须通过各自 `Config` 校验。

`load_app_config()` 再解析 API key，得到 `LoadedAppConfig`。密钥优先级固定为：

```text
api_key_override
    ↓
进程环境变量
    ↓
显式指定的 .env 文件
```

它不会向父目录搜索 `.env`，也不会把 `.env` 内容写入 `os.environ`。调用者必须在应用入口显式选择路径：

```python
from pathlib import Path

from pi_agent_py import load_app_config

root = Path(__file__).resolve().parents[1]
loaded = load_app_config(
    root / "agent.yaml",
    env_path=root / ".env",
)

# 这是四种一等 Provider config 的联合类型，已经完成边界验证。
provider_config = loaded.provider_config
```

这种设计避免包内部根据当前工作目录猜配置文件，也避免测试或嵌入式应用被意外的父目录 `.env` 接管。

## 友好的显式写入

推荐使用示例命令写入，而不是在命令行参数中直接传 key：

```powershell
$env:PYTHONPATH = (Resolve-Path .\python-pi-agent\src).Path
python .\python-pi-agent\examples\22_provider_configuration.py set-key
python .\python-pi-agent\examples\22_provider_configuration.py check
```

`set-key`：

- 使用 `getpass`，输入不回显；
- 要求输入两次；
- 从 YAML 的 `api_key_env` 得到变量名；
- 原子替换 `.env`，保留其他变量和注释；
- 默认拒绝覆盖已有非空值，只有显式 `--force` 才替换；
- 不在返回值或成功消息中携带密钥。

库级写入 API 也要求显式路径：

```python
from pi_agent_py import write_env_secret

write_env_secret(
    root / ".env",
    "MOONSHOT_API_KEY",
    secret_from_secure_prompt,
)
```

在 POSIX 上写入文件会请求 `0600` 权限；Windows 上仍应依赖当前用户目录 ACL，不要把工作区共享给不受信任账户。

## 脱敏诊断

```python
from pi_agent_py import format_config_diagnostics

print(format_config_diagnostics(loaded))
```

输出类似：

```text
provider: kimi
model: kimi-k2.6
config: E:\project\self-agent\pi-agent\agent.yaml
credential: MOONSHOT_API_KEY from dotenv E:\project\self-agent\pi-agent\.env (********1234)
```

诊断只显示最后四位；短密钥完全隐藏。`CredentialDiagnostic` 不保存原始密钥，Provider config 的 `api_key` 字段也被 dataclass `repr` 排除。

## 失败路径

以下情况都会在发起网络请求前抛出 `ConfigurationError`：

- YAML 缺失、语法错误、重复键或未知字段；
- 写了 `provider.api_key`；
- Provider 参数类型错误或模型能力组合不合法；
- 环境变量名称不合法；
- 指定来源都没有密钥；
- `.env` 重复定义同一个变量；
- key 为空、带首尾空格、换行或 NUL；
- 未授权覆盖已有 `.env` 值。

错误文本只包含变量名和被检查的路径，不包含密钥内容。

## 与 Pi 的映射

Pi 的 `ModelRuntime` 将认证解析和模型配置集中在应用运行时，并允许环境变量、运行时覆盖和持久化凭据。本项目当前规模更小：只实现 DeepSeek/Kimi/GLM/TiMi 的 YAML + dotenv 边界，不复制 OAuth、全局用户目录或动态模型注册。共同原则是密钥解析属于组装层，Provider 和 Agent loop 不读取隐式全局配置。

掌握标准：能说明为什么 YAML 存变量名而不是密钥、为什么加载器不搜索父目录、为什么诊断对象不能保存原始 key，以及如何在测试中用 `environ={...}` 完全隔离真实环境。
