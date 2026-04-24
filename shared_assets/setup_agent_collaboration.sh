#!/bin/bash
# Agent 协作监控系统 - 一键设置脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "Agent 协作监控系统设置"
echo "=========================================="
echo ""
echo "架构："
echo "  监控脚本 → 零号龙虾 → Claude → 修复"
echo ""

# 1. 检查环境
echo "📋 检查环境..."

if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到 python3"
    exit 1
fi

if ! command -v openclaw &> /dev/null; then
    echo "⚠️  警告: 未找到 openclaw CLI"
    echo "   Agent 间通信可能无法工作"
fi

# 2. 测试监控脚本
echo ""
echo "🧪 测试监控脚本..."
python3 "$SCRIPT_DIR/agent_monitor.py"

# 3. 设置 Cron
echo ""
echo "⏰ 设置 Cron 定时任务"
echo ""
echo "将添加以下 Cron 任务（每 10 分钟）："
echo "*/10 * * * * cd $SCRIPT_DIR && python3 agent_monitor.py >> agent_monitor.log 2>&1"
echo ""
read -p "是否添加到 crontab？(y/n) " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    # 备份现有 crontab
    crontab -l > /tmp/crontab.bak 2>/dev/null || true
    
    # 添加新任务（如果不存在）
    if ! crontab -l 2>/dev/null | grep -q "agent_monitor.py"; then
        (crontab -l 2>/dev/null; echo "*/10 * * * * cd $SCRIPT_DIR && python3 agent_monitor.py >> agent_monitor.log 2>&1") | crontab -
        echo "✅ Cron 任务已添加"
    else
        echo "ℹ️  Cron 任务已存在"
    fi
fi

# 4. 创建测试脚本
echo ""
echo "📝 创建测试脚本..."

cat > "$SCRIPT_DIR/test_collaboration.sh" << 'EOF'
#!/bin/bash
# 测试 Agent 协作流程

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "测试 Agent 协作流程"
echo "=========================================="

# 1. 模拟问题（停止 Tinder daemon）
echo ""
echo "1️⃣ 模拟问题：停止 Tinder daemon"
pkill -f tinder_daemon.py && echo "  ✅ 已停止" || echo "  ℹ️  未运行"

# 2. 运行监控脚本
echo ""
echo "2️⃣ 运行监控脚本"
python3 "$SCRIPT_DIR/agent_monitor.py"

# 3. 检查是否通知零号龙虾
echo ""
echo "3️⃣ 检查通知"
echo "  请在 Telegram 中检查零号龙虾是否收到通知"
echo ""
read -p "零号龙虾是否收到通知？(y/n) " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "  ✅ 通知成功"
else
    echo "  ❌ 通知失败，请检查配置"
    exit 1
fi

# 4. 等待零号龙虾转发
echo ""
echo "4️⃣ 等待零号龙虾转发给 Claude"
echo "  零号龙虾应该会自动转发给你"
echo ""
read -p "是否收到零号龙虾的转发？(y/n) " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "  ✅ 转发成功"
else
    echo "  ❌ 转发失败，请检查零号龙虾配置"
    exit 1
fi

# 5. 恢复 Tinder daemon
echo ""
echo "5️⃣ 恢复 Tinder daemon"
cd "$SCRIPT_DIR/../tinder-automation"
nohup python3 tinder_daemon.py > tinder_daemon.out 2>&1 &
echo "  ✅ 已重启，PID: $!"

echo ""
echo "=========================================="
echo "✅ 协作流程测试完成"
echo "=========================================="
EOF

chmod +x "$SCRIPT_DIR/test_collaboration.sh"

# 5. 创建零号龙虾配置指南
echo ""
echo "📄 生成零号龙虾配置指南..."

cat > "$SCRIPT_DIR/DRAGONB00_CONFIG.md" << 'EOF'
# 零号龙虾配置指南

## 🤖 角色定位

零号龙虾是自动化系统的"中间人"：
- 接收监控脚本的异常报告
- 尝试自动修复常见问题
- 无法修复时转发给 Claude
- 应用 Claude 的修复方案

## 📝 需要添加的配置

### 1. 在 AGENTS.md 或 SOUL.md 中添加

```markdown
## 自动化系统监控协作

### 职责
我负责监控 Tinder/Bumble 自动化系统的健康状态。

### 自动处理规则

当收到包含"🚨 自动化系统异常报告"的消息时：

1. **分析问题类型**
   - 进程未运行 → 自动重启
   - API 限流 → 降低频率
   - 其他错误 → 转发给 Claude

2. **自动修复（如果可以）**
   ```bash
   # 重启进程
   cd /Users/chengang/.openclaw/workspace/projects/shared_assets
   ./start_all.sh
   ```

3. **转发给 Claude（如果无法自动修复）**
   发送消息给 Bamboo C：
   ```
   @Claude 自动化系统需要你的帮助：
   
   [原始报告内容]
   
   我已尝试自动修复但失败，请提供诊断和修复方案。
   ```

4. **应用 Claude 的修复方案**
   - 执行 Claude 提供的命令
   - 报告执行结果
   - 确认系统恢复正常

### 响应模板

**自动修复成功：**
```
✅ 问题已自动修复

问题: [问题描述]
修复: [修复操作]
结果: 系统恢复正常

当前状态:
  Tinder: ✅ 运行中
  Bumble: ✅ 运行中
```

**需要 Claude 帮助：**
```
@Claude 自动化系统需要你的帮助：

问题类型: [问题类型]
发生时间: [时间]
自动修复: 已尝试但失败

详细信息:
[错误详情]

请提供诊断和修复方案。
```

**应用修复后：**
```
✅ 修复已应用

执行的操作:
[Claude 提供的修复步骤]

执行结果:
[命令输出]

系统状态:
  Tinder: ✅ 运行中
  Bumble: ✅ 运行中
  
问题已解决，感谢 Claude！
```
```

### 2. 自动修复脚本示例

可以在你的工作区创建 `auto_fix.sh`：

```bash
#!/bin/bash
# 零号龙虾的自动修复脚本

PROJECTS_DIR="/Users/chengang/.openclaw/workspace/projects"

case "$1" in
    "restart_tinder")
        cd "$PROJECTS_DIR/tinder-automation"
        nohup python3 tinder_daemon.py > tinder_daemon.out 2>&1 &
        echo "Tinder daemon 已重启，PID: $!"
        ;;
    
    "restart_bumble")
        cd "$PROJECTS_DIR/bumble-automation"
        nohup python3 bumble_daemon.py > bumble_daemon.out 2>&1 &
        echo "Bumble daemon 已重启，PID: $!"
        ;;
    
    "restart_all")
        cd "$PROJECTS_DIR/shared_assets"
        ./start_all.sh
        ;;
    
    *)
        echo "未知的修复操作: $1"
        exit 1
        ;;
esac
```

### 3. 测试配置

运行测试脚本验证配置：

```bash
cd /Users/chengang/.openclaw/workspace/projects/shared_assets
./test_collaboration.sh
```

## 🔄 工作流程示例

### 场景：Tinder daemon 崩溃

1. **监控脚本检测到问题** → 通知零号龙虾
2. **零号龙虾收到通知** → 尝试自动重启
3. **自动重启成功** → 报告修复结果
4. **如果失败** → 转发给 Claude
5. **Claude 分析** → 提供修复方案
6. **零号龙虾应用** → 报告结果

## 📊 监控指标

零号龙虾应该跟踪：
- 收到的异常报告数
- 自动修复成功率
- 转发给 Claude 的次数
- 平均修复时间

## 🎯 优化建议

1. 学习常见问题的修复模式
2. 提高自动修复成功率
3. 减少需要 Claude 介入的次数
4. 优化通知频率（避免重复）

---

配置完成后，整个协作系统就可以自动运行了！
EOF

# 6. 完成
echo ""
echo "=========================================="
echo "✅ 设置完成"
echo "=========================================="
echo ""
echo "📁 已创建的文件:"
echo "  • agent_monitor.py - 监控脚本"
echo "  • test_collaboration.sh - 测试脚本"
echo "  • DRAGONB00_CONFIG.md - 零号龙虾配置指南"
echo "  • AGENT_COLLABORATION.md - 完整协作文档"
echo ""
echo "📋 下一步:"
echo "  1. 配置零号龙虾（参考 DRAGONB00_CONFIG.md）"
echo "  2. 运行测试: ./test_collaboration.sh"
echo "  3. 启动守护进程: ./start_all.sh"
echo ""
echo "⏰ Cron 任务已设置，每 10 分钟自动检查"
echo "📊 查看监控日志: tail -f agent_monitor.log"
echo ""
