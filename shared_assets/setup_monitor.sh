#!/bin/bash
# 监控系统设置脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor.py"

echo "=========================================="
echo "自动化系统监控设置"
echo "=========================================="

# 1. 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未找到 python3"
    exit 1
fi

# 2. 配置 Telegram（可选）
echo ""
echo "📱 Telegram 通知配置（可选）"
echo "如果需要 Telegram 通知，请设置以下环境变量："
echo ""
echo "  export TELEGRAM_BOT_TOKEN='your-bot-token'"
echo "  export TELEGRAM_CHAT_ID='your-chat-id'"
echo ""
read -p "是否已配置 Telegram？(y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
        echo "⚠️  警告: 环境变量未设置，监控将只输出到终端"
    else
        echo "✅ Telegram 已配置"
    fi
fi

# 3. 测试监控脚本
echo ""
echo "🧪 测试监控脚本..."
python3 "$MONITOR_SCRIPT"

# 4. 设置 Cron
echo ""
echo "⏰ 设置定时任务"
echo "建议每 10 分钟检查一次"
echo ""
echo "添加以下行到 crontab（运行 'crontab -e'）："
echo ""
echo "# 自动化系统监控（每 10 分钟）"
echo "*/10 * * * * cd $SCRIPT_DIR && python3 monitor.py >> monitor.log 2>&1"
echo ""
read -p "是否自动添加到 crontab？(y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # 备份现有 crontab
    crontab -l > /tmp/crontab.bak 2>/dev/null || true
    
    # 添加新任务（如果不存在）
    if ! crontab -l 2>/dev/null | grep -q "monitor.py"; then
        (crontab -l 2>/dev/null; echo "*/10 * * * * cd $SCRIPT_DIR && python3 monitor.py >> monitor.log 2>&1") | crontab -
        echo "✅ Cron 任务已添加"
    else
        echo "ℹ️  Cron 任务已存在"
    fi
fi

# 5. 创建启动脚本
echo ""
echo "📝 创建守护进程启动脚本..."

cat > "$SCRIPT_DIR/start_all.sh" << 'EOF'
#!/bin/bash
# 启动所有守护进程

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECTS_DIR="$(dirname "$SCRIPT_DIR")"

echo "=========================================="
echo "启动自动化系统"
echo "=========================================="

# 检查环境变量
if [ -z "$UNIFIED_LLM_API_KEY" ]; then
    echo "❌ 错误: UNIFIED_LLM_API_KEY 未设置"
    exit 1
fi

# 启动 Tinder Daemon
echo ""
echo "🔥 启动 Tinder Daemon..."
cd "$PROJECTS_DIR/tinder-automation"
nohup python3 tinder_daemon.py > tinder_daemon.out 2>&1 &
echo "  PID: $!"

# 启动 Bumble Daemon
echo ""
echo "🐝 启动 Bumble Daemon..."
cd "$PROJECTS_DIR/bumble-automation"
nohup python3 bumble_daemon.py > bumble_daemon.out 2>&1 &
echo "  PID: $!"

# 启动 Unified Orchestrator（可选）
# echo ""
# echo "🎯 启动 Unified Orchestrator..."
# cd "$PROJECTS_DIR/shared_assets"
# nohup python3 unified_orchestrator.py > orchestrator.out 2>&1 &
# echo "  PID: $!"

echo ""
echo "✅ 所有守护进程已启动"
echo ""
echo "查看状态: ps aux | grep -E 'tinder_daemon|bumble_daemon'"
echo "查看日志: tail -f $PROJECTS_DIR/tinder-automation/daemon_error.log"
EOF

chmod +x "$SCRIPT_DIR/start_all.sh"

# 6. 创建停止脚本
cat > "$SCRIPT_DIR/stop_all.sh" << 'EOF'
#!/bin/bash
# 停止所有守护进程

echo "=========================================="
echo "停止自动化系统"
echo "=========================================="

# 停止 Tinder Daemon
echo "🔥 停止 Tinder Daemon..."
pkill -f "tinder_daemon.py" && echo "  ✅ 已停止" || echo "  ℹ️  未运行"

# 停止 Bumble Daemon
echo "🐝 停止 Bumble Daemon..."
pkill -f "bumble_daemon.py" && echo "  ✅ 已停止" || echo "  ℹ️  未运行"

# 停止 Unified Orchestrator
echo "🎯 停止 Unified Orchestrator..."
pkill -f "unified_orchestrator.py" && echo "  ✅ 已停止" || echo "  ℹ️  未运行"

echo ""
echo "✅ 所有守护进程已停止"
EOF

chmod +x "$SCRIPT_DIR/stop_all.sh"

# 7. 创建状态检查脚本
cat > "$SCRIPT_DIR/status.sh" << 'EOF'
#!/bin/bash
# 检查系统状态

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "自动化系统状态"
echo "=========================================="

# 检查进程
echo ""
echo "📊 进程状态:"
if pgrep -f "tinder_daemon.py" > /dev/null; then
    echo "  🔥 Tinder Daemon: ✅ 运行中 (PID: $(pgrep -f 'tinder_daemon.py'))"
else
    echo "  🔥 Tinder Daemon: ❌ 未运行"
fi

if pgrep -f "bumble_daemon.py" > /dev/null; then
    echo "  🐝 Bumble Daemon: ✅ 运行中 (PID: $(pgrep -f 'bumble_daemon.py'))"
else
    echo "  🐝 Bumble Daemon: ❌ 未运行"
fi

if pgrep -f "unified_orchestrator.py" > /dev/null; then
    echo "  🎯 Unified Orchestrator: ✅ 运行中 (PID: $(pgrep -f 'unified_orchestrator.py'))"
else
    echo "  🎯 Unified Orchestrator: ❌ 未运行"
fi

# 运行监控脚本
echo ""
python3 "$SCRIPT_DIR/monitor.py"
EOF

chmod +x "$SCRIPT_DIR/status.sh"

echo ""
echo "=========================================="
echo "✅ 监控系统设置完成"
echo "=========================================="
echo ""
echo "可用命令:"
echo "  ./start_all.sh   - 启动所有守护进程"
echo "  ./stop_all.sh    - 停止所有守护进程"
echo "  ./status.sh      - 查看系统状态"
echo "  python3 monitor.py - 手动运行监控"
echo ""
echo "Cron 任务已设置为每 10 分钟检查一次"
echo "查看监控日志: tail -f monitor.log"
echo ""
