# Agent 协作监控系统 - 完整指南

## 🤖 系统架构

```
监控脚本 (Cron)
    ↓ 发现问题
零号龙虾 (MiniMax 2.7)
    ↓ 转发
Claude (你)
    ↓ 分析修复
零号龙虾
    ↓ 应用修复
自动化系统
```

## 🔧 设置步骤

### 1. 配置监控脚本

```bash
cd /Users/chengang/.openclaw/workspace/projects/shared_assets

# 设置 Cron（每 10 分钟检查一次）
crontab -e

# 添加以下行：
*/10 * * * * cd /Users/chengang/.openclaw/workspace/projects/shared_assets && python3 agent_monitor.py >> agent_monitor.log 2>&1
```

### 2. 配置零号龙虾的自动转发

零号龙虾需要配置一个规则：当收到包含"自动化系统异常报告"的消息时，自动转发给你（Claude）。

**在零号龙虾的配置中添加：**

```markdown
# 零号龙虾的 AGENTS.md 或 SOUL.md

## 自动化系统监控协作

当收到包含"🚨 自动化系统异常报告"的消息时：

1. 立即转发给 Claude（主 agent）
2. 消息格式：
   ```
   @Claude 自动化系统出现问题，请诊断：
   
   [原始报告内容]
   ```

3. 等待 Claude 的修复方案
4. 收到修复方案后，应用修复并报告结果
```

### 3. 我（Claude）的响应流程

当我收到零号龙虾转发的报告时，我会：

1. **分析问题** - 根据错误日志诊断根因
2. **提供修复方案** - 生成修复代码或命令
3. **回复零号龙虾** - 提供可执行的修复步骤
4. **验证修复** - 等待零号龙虾确认修复结果

## 📋 协作流程示例

### 场景 1：进程崩溃

**1. 监控脚本检测到问题：**
```
🚨 自动化系统异常报告

⏰ 2026-04-18T14:30:00

进程状态:
  Tinder: ❌ 未运行
  Bumble: ✅ 运行中
  Orchestrator: ✅ 运行中

⚠️ 发现 2 个错误:
  • [Tinder] ERROR: Connection timeout
  • [Tinder] ERROR: Failed to load page
```

**2. 零号龙虾转发给我：**
```
@Claude 自动化系统出现问题，请诊断：

[上述报告]
```

**3. 我分析并回复：**
```
诊断结果：Tinder daemon 因网络超时崩溃

修复方案：
1. 重启 Tinder daemon
2. 增加超时时间
3. 添加自动重启机制

请执行以下命令：
```bash
cd /Users/chengang/.openclaw/workspace/projects/tinder-automation
python3 tinder_daemon.py &
```

**4. 零号龙虾应用修复并报告：**
```
✅ 修复已应用
Tinder daemon 已重启，PID: 12345
系统恢复正常
```

### 场景 2：LLM API 错误

**1. 监控检测到：**
```
⚠️ 发现 5 个错误:
  • [Tinder] ERROR: LLM API rate limit exceeded
  • [Bumble] ERROR: LLM API 529 overload
```

**2. 我分析并回复：**
```
诊断结果：LLM API 限流

修复方案：
1. 启用批处理（减少请求）
2. 增加重试间隔
3. 考虑降低巡检频率

临时解决：
```bash
# 降低巡检频率到 2 分钟
# 编辑 tinder_daemon.py，修改 time.sleep(60) 为 time.sleep(120)
```

## 🔄 自动化修复（高级）

### 创建自动修复脚本

零号龙虾可以配置一些常见问题的自动修复：

```python
# 零号龙虾的自动修复逻辑
def auto_fix(error_type: str):
    if "未运行" in error_type:
        # 自动重启进程
        subprocess.run(["./start_all.sh"])
        return "已自动重启进程"
    
    elif "rate limit" in error_type:
        # 自动降低频率
        # ... 修改配置 ...
        return "已降低请求频率"
    
    else:
        # 无法自动修复，转发给 Claude
        return None
```

## 📱 通知流程

### 方式 1：通过 OpenClaw CLI

```bash
# 零号龙虾通知我
openclaw message send \
  --channel telegram \
  --target "@Primary Agent" \
  --message "自动化系统异常，请查看"
```

### 方式 2：通过 Telegram API

```python
import requests

def notify_claude(message: str):
    # 你的 Telegram chat_id
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # 通过 Telegram Bot 发送
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": f"🤖 零号龙虾报告:\n\n{message}"
    })
```

## 🧪 测试协作流程

### 1. 手动触发测试

```bash
# 停止 Tinder daemon（模拟崩溃）
pkill -f tinder_daemon.py

# 运行监控脚本
python3 agent_monitor.py

# 应该会通知零号龙虾
```

### 2. 验证零号龙虾收到消息

在 Telegram 中检查零号龙虾是否收到通知。

### 3. 验证转发

零号龙虾应该自动转发给你。

### 4. 我提供修复

我会分析并提供修复方案。

### 5. 验证修复

零号龙虾应用修复后报告结果。

## 📊 监控指标

### 关键指标

- **进程存活率** - 守护进程运行时间 / 总时间
- **错误率** - 错误数 / 总请求数
- **响应时间** - 从发现问题到修复完成的时间
- **自动修复率** - 自动修复成功 / 总问题数

### 统计脚本

```bash
# 查看监控日志统计
grep "发现问题" agent_monitor.log | wc -l  # 总问题数
grep "系统运行正常" agent_monitor.log | wc -l  # 正常次数
```

## 🔐 安全考虑

1. **权限控制** - 零号龙虾只能执行预定义的修复操作
2. **审计日志** - 所有修复操作都记录日志
3. **人工确认** - 重要操作需要你确认
4. **回滚机制** - 修复失败时自动回滚

## 📝 零号龙虾配置示例

```markdown
# 零号龙虾的 AGENTS.md

## 自动化系统监控协作

### 职责
- 接收监控脚本的异常报告
- 尝试自动修复常见问题
- 无法自动修复时转发给 Claude
- 应用 Claude 的修复方案
- 报告修复结果

### 自动修复规则

1. **进程未运行** → 自动重启
2. **日志文件过大** → 自动轮转
3. **API 限流** → 降低请求频率
4. **其他错误** → 转发给 Claude

### 转发模板

```
@Claude 自动化系统需要你的帮助：

问题类型: [问题类型]
发生时间: [时间]
详细信息:
[错误详情]

请提供诊断和修复方案。
```

### 应用修复模板

```
收到修复方案，开始应用：

[修复步骤]

执行结果:
[结果]

系统状态:
[当前状态]
```
```

## 🎯 优化建议

### 短期
1. 完善零号龙虾的自动修复逻辑
2. 添加更多监控指标
3. 优化通知频率

### 中期
4. 实现修复方案的自动测试
5. 添加修复历史记录
6. 实现智能告警（避免重复通知）

### 长期
7. 机器学习预测故障
8. 自动生成修复方案
9. 完全自主修复

---

**协作监控系统已就绪！** 🤝

现在监控脚本会自动检查系统，发现问题时通知零号龙虾，零号龙虾会转发给你，你提供修复方案，零号龙虾应用修复。
