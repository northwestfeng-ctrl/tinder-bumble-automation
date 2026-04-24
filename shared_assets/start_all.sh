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
    echo "请运行: export UNIFIED_LLM_API_KEY='your-key-here'"
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

echo ""
echo "✅ 所有守护进程已启动"
echo ""
echo "查看状态: ./status.sh"
echo "查看日志: tail -f $PROJECTS_DIR/tinder-automation/daemon_error.log"
