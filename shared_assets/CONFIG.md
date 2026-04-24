# 统一配置管理 - 使用指南

## 📦 安装依赖

```bash
pip install pydantic python-dotenv
```

## 🚀 快速开始

### 1. 创建配置文件

```bash
cd /Users/chengang/.openclaw/workspace/projects/shared_assets
cp .env.example .env
```

### 2. 填写必需配置

编辑 `.env` 文件，至少设置：

```bash
UNIFIED_LLM_API_KEY=your-actual-api-key-here
```

### 3. 在代码中使用

```python
from config import get_config

# 获取全局配置
config = get_config()

# 使用配置
print(config.llm.api_key)
print(config.llm.model)
print(config.tinder.profile_dir)
print(config.browser.headless)

# 访问嵌套配置
if config.proxy.enabled:
    print(f"代理服务器: {config.proxy.server}")

# 热重载配置（检测到 .env 变更后）
from config import reload_config
config = reload_config()
```

## 📝 配置项说明

### LLM 配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `UNIFIED_LLM_API_KEY` | str | **必需** | LLM API Key |
| `APP_LLM__MODEL` | str | MiniMax-M2.7 | 模型名称 |
| `APP_LLM__BASE_URL` | str | https://api.minimax.chat/... | API 端点 |
| `APP_LLM__TEMPERATURE` | float | 0.7 | 温度参数（0.0-2.0） |
| `APP_LLM__MAX_TOKENS` | int | 300 | 最大 token 数 |
| `APP_LLM__TIMEOUT` | int | 30 | 请求超时（秒） |
| `APP_LLM__MAX_RETRIES` | int | 4 | 最大重试次数 |

### 浏览器配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `APP_BROWSER__HEADLESS` | bool | true | 无头模式 |
| `APP_BROWSER__VIEWPORT_WIDTH` | int | 1280 | 视口宽度 |
| `APP_BROWSER__VIEWPORT_HEIGHT` | int | 800 | 视口高度 |
| `APP_BROWSER__USER_AGENT` | str | None | 自定义 User-Agent |

### 代理配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `APP_PROXY__ENABLED` | bool | false | 启用代理 |
| `APP_PROXY__SERVER` | str | None | 代理服务器（ip:port） |
| `APP_PROXY__USERNAME` | str | None | 代理用户名 |
| `APP_PROXY__PASSWORD` | str | None | 代理密码 |
| `APP_PROXY__STICKY_DURATION` | int | 300 | 会话粘性时长（秒） |

### 平台配置（Tinder/Bumble）

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `APP_TINDER__ENABLED` | bool | true | 启用 Tinder |
| `APP_TINDER__PROFILE_DIR` | Path | ~/.tinder-automation/browser-profile | 浏览器配置目录 |
| `APP_TINDER__URL` | str | https://tinder.com | Tinder URL |
| `APP_TINDER__MAX_SESSION_ACTIONS` | int | 20 | 单次会话最大操作数 |
| `APP_TINDER__COOLDOWN_MINUTES` | int | 1 | 冷却时间（分钟） |

Bumble 配置同理，将 `TINDER` 替换为 `BUMBLE`。

### 日志配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `APP_LOG__LEVEL` | str | INFO | 日志级别 |
| `APP_LOG__MAX_BYTES` | int | 10485760 | 单文件最大字节数（10MB） |
| `APP_LOG__BACKUP_COUNT` | int | 5 | 备份文件数 |

### 应用配置

| 环境变量 | 类型 | 默认值 | 说明 |
|---------|------|--------|------|
| `APP_ENV` | str | production | 运行环境 |
| `APP_DEBUG` | bool | false | 调试模式 |
| `APP_WORKSPACE_DIR` | Path | ~/.openclaw/workspace/projects | 工作区目录 |
| `APP_EVOLUTION_HOUR` | int | 3 | 演化流水线执行时间（小时） |
| `APP_CURFEW_START` | int | 0 | 宵禁开始时间（小时） |
| `APP_CURFEW_END` | int | 8 | 宵禁结束时间（小时） |

## 🔄 迁移指南

### 从旧代码迁移

**旧代码（硬编码）：**
```python
LLM_API_KEY = "sk-your-api-key-here"
LLM_MODEL = "MiniMax-M2.7"
PROFILE_PATH = Path.home() / ".tinder-automation" / "browser-profile"
```

**新代码（统一配置）：**
```python
from config import get_config

config = get_config()
llm_api_key = config.llm.api_key
llm_model = config.llm.model
profile_path = config.tinder.profile_dir
```

### 批量替换示例

**unified_reply_engine.py:**
```python
# 旧代码
LLM_API_KEY = os.environ.get("UNIFIED_LLM_API_KEY")
LLM_MODEL = os.environ.get("UNIFIED_LLM_MODEL", "MiniMax-M2.7")

# 新代码
from config import get_config
config = get_config()
LLM_API_KEY = config.llm.api_key
LLM_MODEL = config.llm.model
```

**tinder_daemon.py:**
```python
# 旧代码
PROFILE = Path.home() / ".tinder-automation" / "browser-profile"
ERROR_LOG = SCRIPT_DIR / "daemon_error.log"

# 新代码
from config import get_config
config = get_config()
PROFILE = config.tinder.profile_dir
ERROR_LOG = SCRIPT_DIR / "daemon_error.log"
```

**unified_orchestrator.py:**
```python
# 旧代码
TINDER_PROFILE = Path.home() / ".tinder-automation" / "browser-profile"
BUMBLE_PROFILE = Path.home() / ".bumble-automation" / "test-profile"

# 新代码
from config import get_config
config = get_config()
TINDER_PROFILE = config.tinder.profile_dir
BUMBLE_PROFILE = config.bumble.profile_dir
```

## ✅ 验证配置

创建测试脚本 `test_config.py`：

```python
#!/usr/bin/env python3
from config import get_config

def test_config():
    config = get_config()
    
    print("=" * 50)
    print("配置验证")
    print("=" * 50)
    
    # LLM
    print(f"\n[LLM]")
    print(f"  API Key: {config.llm.api_key[:20]}...")
    print(f"  Model: {config.llm.model}")
    print(f"  Base URL: {config.llm.base_url}")
    print(f"  Temperature: {config.llm.temperature}")
    
    # Browser
    print(f"\n[Browser]")
    print(f"  Headless: {config.browser.headless}")
    print(f"  Viewport: {config.browser.viewport_width}x{config.browser.viewport_height}")
    
    # Tinder
    print(f"\n[Tinder]")
    print(f"  Enabled: {config.tinder.enabled}")
    print(f"  Profile Dir: {config.tinder.profile_dir}")
    print(f"  Cooldown: {config.tinder.cooldown_minutes} min")
    
    # Bumble
    print(f"\n[Bumble]")
    print(f"  Enabled: {config.bumble.enabled}")
    print(f"  Profile Dir: {config.bumble.profile_dir}")
    print(f"  Cooldown: {config.bumble.cooldown_minutes} min")
    
    # Log
    print(f"\n[Log]")
    print(f"  Level: {config.log.level}")
    print(f"  Max Bytes: {config.log.max_bytes / 1024 / 1024:.1f} MB")
    print(f"  Backup Count: {config.log.backup_count}")
    
    print("\n✅ 配置加载成功")

if __name__ == "__main__":
    test_config()
```

运行测试：
```bash
python test_config.py
```

## 🔒 安全建议

1. **永远不要提交 `.env` 文件到 Git**
   ```bash
   echo ".env" >> .gitignore
   ```

2. **使用密钥管理工具（生产环境）**
   - 1Password CLI
   - AWS Secrets Manager
   - HashiCorp Vault

3. **定期轮换 API Key**

4. **限制 `.env` 文件权限**
   ```bash
   chmod 600 .env
   ```

## 🐛 故障排查

### 问题：`RuntimeError: LLM API Key is required`

**原因：** 未设置 `UNIFIED_LLM_API_KEY` 环境变量

**解决：**
```bash
export UNIFIED_LLM_API_KEY='your-key-here'
# 或在 .env 文件中设置
```

### 问题：`ValidationError: 1 validation error for LLMConfig`

**原因：** 配置值类型错误或超出范围

**解决：** 检查 `.env` 文件中的值是否符合类型要求

### 问题：配置修改后未生效

**原因：** 配置已缓存

**解决：**
```python
from config import reload_config
config = reload_config()
```

## 📚 进阶用法

### 从 JSON 文件加载配置

```python
from pathlib import Path
from config import load_config_from_file

config = load_config_from_file(Path("config.json"))
```

### 动态覆盖配置

```python
from config import get_config

config = get_config()

# 临时修改（不影响环境变量）
config.llm.temperature = 0.9
config.browser.headless = False
```

### 多环境配置

```bash
# .env.development
APP_ENV=development
APP_DEBUG=true
APP_LOG__LEVEL=DEBUG

# .env.production
APP_ENV=production
APP_DEBUG=false
APP_LOG__LEVEL=INFO
```

加载指定环境：
```bash
ln -sf .env.development .env
python your_script.py
```

## 🎯 下一步

配置管理已完成，接下来可以：

1. **迁移现有代码** - 将硬编码配置替换为 `get_config()`
2. **浏览器实例复用** - 使用配置中的 `browser` 和 `tinder/bumble` 配置
3. **对话历史缓存** - 基于配置的 `workspace_dir` 创建缓存目录

---

**相关文件：**
- `config.py` - 配置加载器
- `.env.example` - 配置模板
- `CONFIG.md` - 本文档
