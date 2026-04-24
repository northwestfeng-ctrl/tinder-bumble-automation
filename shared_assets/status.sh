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
python3 "$SCRIPT_DIR/agent_monitor.py"
