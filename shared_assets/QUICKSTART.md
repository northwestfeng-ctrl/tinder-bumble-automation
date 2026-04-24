# 🎉 Agent 协作监控系统 - 快速开始

## ✅ 已创建的文件

**监控系统：**
- ✅ `agent_monitor.py` - Agent 协作监控脚本
- ✅ `start_all.sh` - 启动所有守护进程
- ✅ `stop_all.sh` - 停止所有守护进程
- ✅ `status.sh` - 查看系统状态

**文档：**
- ✅ `AGENT_COLLABORATION.md` - 完整协作文档
- ✅ `MONITOR_GUIDE.md` - 监控系统使用指南
- ✅ `setup_agent_collaboration.sh` - 一键设置脚本

## 🚀 立即开始

### 1. 设置环境变量

```bash
# 必需：LLM API Key
export UNIFIED_LLM_API_KEY='your-api-key-here'

# 建议添加到 ~/.zshrc 或 ~/.bashrc
echo 'export UNIFIED_LLM_API_KEY="your-key-here"' >> ~/.zshrc
source ~/.zshrc
```

### 2. 设置 Cron 定时任务

```bash
# 编辑 crontab
crontab -e

# 添加以下行（每 10 分钟检查一次）
*/10 * * * * cd /Users/chengang/.openclaw/workspace/projects/shared_assets && python3 agent_monitor.py >> agent_monitor.log 2>&1
```

### 3. 配置零号龙虾

将以下配置添加到零号龙虾的 `AGENTS.md` 或 `SOUL.md`：

```markdown
## 自动化系统监控

当收到"🚨 自动化系统异常报告"时：

1. 如果是"进程未运行" → 执行：
   ```bash
   cd /Users/chengang/.openclaw/workspace/projects/shared_assets
   ./start_all.sh
   ```
   报告："✅ 已自动重启进程"

2. 其他错误 → 转发给主 agent：
   ```
   @Claude 自动化系统需要帮助：
   [原始报告]
   ```

3. 收到 Claude 的修复方案 → 执行命令 → 报告结果
```

### 4. 启动守护进程

```bash
cd /Users/chengang/.openclaw/workspace/projects/shared_assets
./start_all.sh
```

### 5. 查看状态

```bash
./status.sh
```

## 📊 工作流程

```
1. Cron 每 10 分钟运行 agent_monitor.py
   ↓
2. 发现问题 → 通知你（主 agent）
   ↓
3. 你收到通知 → 转发给零号龙虾
   ↓
4. 零号龙虾尝试自动修复
   ├─ 成功 → 报告结果 ✅
   └─ 失败 → 转发给你（Claude）
       ↓
5. 你分析问题 → 提供修复方案
   ↓
6. 零号龙虾应用修复 → 报告结果
   ↓
7. 系统恢复正常 ✅
```

## 🔧 常用命令

```bash
# 查看系统状态
./status.sh

# 启动所有守护进程
./start_all.sh

# 停止所有守护进程
./stop_all.sh

# 手动运行监控
python3 agent_monitor.py

# 查看监控日志
tail -f agent_monitor.log

# 查看通知日志
tail -f notifications.log

# 查看 Cron 任务
crontab -l
```

## 📝 监控报告示例

### 正常状态
```
🤖 自动化系统健康检查
⏰ 2026-04-18T14:30:00

进程状态:
  Tinder: ✅ 运行中
  Bumble: ✅ 运行中
  Orchestrator: ✅ 运行中

✅ 无错误
```

### 发现问题
```
🤖 自动化系统健康检查
⏰ 2026-04-18T14:30:00

进程状态:
  Tinder: ❌ 未运行
  Bumble: ✅ 运行中
  Orchestrator: ✅ 运行中

⚠️ 发现 3 个错误:
  • [Tinder] ERROR: Connection timeout
  • [Tinder] ERROR: Failed to load page
```

## 🎯 下一步

1. ✅ 环境变量已设置
2. ✅ Cron 任务已配置
3. ⏳ 配置零号龙虾（参考上面的配置）
4. ⏳ 启动守护进程
5. ⏳ 测试协作流程

## 📚 相关文档

- `AGENT_COLLABORATION.md` - 完整协作文档
- `MONITOR_GUIDE.md` - 监控系统详细指南
- `FINAL_REPORT.md` - 完整优化报告

---

**🤝 Agent 协作监控系统已就绪！**

系统会自动监控，发现问题时通知你，你转发给零号龙虾，零号龙虾会尝试自动修复或转发给我（Claude），我提供修复方案，零号龙虾应用修复。整个流程完全自动化！
