# 自动化系统监控 - 使用指南

## 📦 监控系统说明

监控系统会定期检查：
- ✅ 守护进程是否运行
- ✅ 日志中是否有错误
- ✅ 系统健康状态

发现问题时会通过 Telegram 通知你。

## 🚀 快速开始

### 1. 设置环境变量

```bash
# 必需：LLM API Key
export UNIFIED_LLM_API_KEY='your-api-key-here'

# 可选：Telegram 通知
export TELEGRAM_BOT_TOKEN='your-bot-token'
export TELEGRAM_CHAT_ID='your-chat-id'
```

### 2. 运行设置脚本

```bash
cd /Users/chengang/.openclaw/workspace/projects/shared_assets
chmod +x setup_monitor.sh
./setup_monitor.sh
```

这会：
- ✅ 测试监控脚本
- ✅ 设置 Cron 定时任务（每 10 分钟）
- ✅ 创建启动/停止/状态脚本

### 3. 启动守护进程

```bash
./start_all.sh
```

### 4. 查看状态

```bash
./status.sh
```

## 📱 Telegram 通知设置

### 创建 Telegram Bot

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新 bot
3. 按提示设置名称，获得 `BOT_TOKEN`

### 获取 Chat ID

1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送任意消息，获得你的 `Chat ID`

### 配置环境变量

```bash
# 添加到 ~/.zshrc 或 ~/.bashrc
export TELEGRAM_BOT_TOKEN='your-telegram-bot-token'
export TELEGRAM_CHAT_ID='your-chat-id'

# 重新加载
source ~/.zshrc
```

## 🔧 可用命令

### 启动所有守护进程
```bash
./start_all.sh
```

### 停止所有守护进程
```bash
./stop_all.sh
```

### 查看系统状态
```bash
./status.sh
```

### 手动运行监控
```bash
python3 monitor.py
```

### 查看监控日志
```bash
tail -f monitor.log
```

## 📊 监控报告示例

### 正常状态
```
🤖 自动化系统健康检查
⏰ 时间: 2026-04-18T14:30:00

进程状态:
  Tinder: ✅ 运行中
  Bumble: ✅ 运行中
  Orchestrator: ✅ 运行中

✅ 无错误
```

### 发现问题
```
🤖 自动化系统健康检查
⏰ 时间: 2026-04-18T14:30:00

进程状态:
  Tinder: ❌ 未运行
  Bumble: ✅ 运行中
  Orchestrator: ✅ 运行中

⚠️ 发现 3 个错误:
  • [Tinder] ERROR: Connection timeout
  • [Tinder] ERROR: Failed to load page
  • [Bumble] ERROR: LLM API rate limit
```

## 🔄 Cron 任务管理

### 查看当前 Cron 任务
```bash
crontab -l
```

### 编辑 Cron 任务
```bash
crontab -e
```

### 删除监控任务
```bash
crontab -l | grep -v "monitor.py" | crontab -
```

### 修改检查频率

**每 5 分钟：**
```cron
*/5 * * * * cd /path/to/shared_assets && python3 monitor.py >> monitor.log 2>&1
```

**每 30 分钟：**
```cron
*/30 * * * * cd /path/to/shared_assets && python3 monitor.py >> monitor.log 2>&1
```

**每小时：**
```cron
0 * * * * cd /path/to/shared_assets && python3 monitor.py >> monitor.log 2>&1
```

## 🐛 故障排查

### 问题：Cron 任务未执行

**检查 Cron 服务：**
```bash
# macOS
sudo launchctl list | grep cron

# Linux
systemctl status cron
```

**查看 Cron 日志：**
```bash
# macOS
tail -f /var/log/system.log | grep cron

# Linux
tail -f /var/log/syslog | grep CRON
```

### 问题：Telegram 通知未发送

**检查环境变量：**
```bash
echo $TELEGRAM_BOT_TOKEN
echo $TELEGRAM_CHAT_ID
```

**测试 Bot：**
```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\": \"$TELEGRAM_CHAT_ID\", \"text\": \"测试消息\"}"
```

### 问题：监控脚本报错

**手动运行查看详细错误：**
```bash
python3 monitor.py
```

**检查 Python 版本：**
```bash
python3 --version  # 需要 3.7+
```

## 📝 自定义监控

### 添加自定义检查

编辑 `monitor.py`，在 `check_system_health()` 函数中添加：

```python
def check_system_health() -> dict:
    health = {
        # ... 现有检查 ...
        "custom_check": False,
    }
    
    # 添加自定义检查
    try:
        # 例如：检查缓存命中率
        cache_stats = get_cache_stats()
        if cache_stats["hit_rate"] < 50:
            health["errors"].append("缓存命中率过低")
    except Exception as e:
        health["errors"].append(f"自定义检查失败: {e}")
    
    return health
```

### 修改通知条件

编辑 `monitor.py`，修改 `main()` 函数中的 `should_notify`：

```python
should_notify = (
    not health["tinder_running"] or
    not health["bumble_running"] or
    len(health["errors"]) > 5  # 改为 5 个错误才通知
)
```

## 🔐 安全建议

1. ✅ 不要将 Bot Token 提交到 Git
2. ✅ 使用环境变量存储敏感信息
3. ✅ 定期轮换 Bot Token
4. ✅ 限制 Bot 权限（只需发送消息）

## 📈 监控最佳实践

### 检查频率建议

| 场景 | 频率 | 说明 |
|------|------|------|
| 开发测试 | 每 5 分钟 | 快速发现问题 |
| 生产环境 | 每 10 分钟 | 平衡及时性和资源 |
| 稳定运行 | 每 30 分钟 | 减少通知干扰 |

### 日志管理

**定期清理日志：**
```bash
# 保留最近 7 天
find . -name "*.log" -mtime +7 -delete

# 或使用 logrotate
```

**监控日志大小：**
```bash
du -sh *.log
```

## 🎯 与 Claude 协作流程

虽然我（Claude）无法直接运行长期进程或主动发消息，但你可以这样协作：

### 方式 1：定期检查（推荐）

1. **Cron 自动监控** - 每 10 分钟检查一次
2. **Telegram 通知你** - 发现问题时通知
3. **你转发给我** - 将错误信息发给我
4. **我分析并修复** - 我提供解决方案
5. **你应用修复** - 运行我提供的修复代码

### 方式 2：手动检查

```bash
# 每天运行一次
./status.sh

# 如果发现问题，将输出发给我
```

### 方式 3：使用 OpenClaw 心跳

如果你在 OpenClaw 中配置了心跳，可以在 `HEARTBEAT.md` 中添加：

```markdown
- 每天检查一次自动化系统状态
- 如果发现错误，报告给用户
```

## 📚 相关文档

- `FINAL_REPORT.md` - 完整优化报告
- `CONFIG.md` - 配置管理指南
- `PERFORMANCE_OPTIMIZATION.md` - 性能优化指南

---

**监控系统已就绪！** 🎉

现在系统会自动监控，发现问题时通过 Telegram 通知你。
