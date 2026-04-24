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
