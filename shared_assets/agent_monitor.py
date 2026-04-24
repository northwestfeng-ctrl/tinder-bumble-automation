#!/usr/bin/env python3
"""
Agent 协作监控脚本
监控系统 → 发现问题 → 通知零号龙虾 → 零号龙虾转发给 Claude → Claude 修复
"""
import os
import sys
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime

# 配置
PROJECTS_DIR = Path("/Users/chengang/.openclaw/workspace/projects")
DRAGONB00_BOT_USERNAME = "@DragonB00_Bot"  # Telegram username
USER_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # 备用通知 chat_id

def check_process_running(process_name: str) -> bool:
    """检查进程是否运行"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        print(f"[错误] 检查进程失败: {e}")
        return False

def check_log_errors(log_file: Path, hours: int = 1) -> list:
    """检查日志中的错误"""
    if not log_file.exists():
        return []
    
    errors = []
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                if "ERROR" in line or "CRITICAL" in line:
                    errors.append(line.strip())
    except Exception as e:
        print(f"[错误] 读取日志失败: {e}")
    
    return errors[-10:]  # 只返回最近 10 条

def check_system_health() -> dict:
    """检查系统健康状态"""
    health = {
        "timestamp": datetime.now().isoformat(),
        "tinder_running": False,
        "bumble_running": False,
        "orchestrator_running": False,
        "errors": [],
    }
    
    # 检查进程
    health["tinder_running"] = check_process_running("tinder_daemon.py")
    health["bumble_running"] = check_process_running("bumble_daemon.py")
    health["orchestrator_running"] = check_process_running("unified_orchestrator.py")
    
    # 检查日志错误
    tinder_log = PROJECTS_DIR / "tinder-automation" / "daemon_error.log"
    bumble_log = PROJECTS_DIR / "bumble-automation" / "daemon_error.log"
    
    tinder_errors = check_log_errors(tinder_log)
    bumble_errors = check_log_errors(bumble_log)
    
    if tinder_errors:
        health["errors"].extend([f"[Tinder] {e}" for e in tinder_errors])
    if bumble_errors:
        health["errors"].extend([f"[Bumble] {e}" for e in bumble_errors])
    
    return health

def notify_dragonb00(message: str):
    """通过 Telegram 通知零号龙虾和用户"""
    # 方式 1：尝试通过 OpenClaw message 工具
    try:
        # 先通知用户（你），让你转发给零号龙虾
        cmd = [
            "openclaw", "message", "send",
            "--channel", "telegram",
            "--target", USER_CHAT_ID,
            "--message", f"🤖 自动化系统监控报告\n\n{message}\n\n请转发给 {DRAGONB00_BOT_USERNAME}"
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            print(f"[通知] 已发送通知")
            return
        else:
            print(f"[警告] OpenClaw 通知失败: {result.stderr}")
    
    except Exception as e:
        print(f"[警告] OpenClaw 通知失败: {e}")
    
    # 方式 2：备用 - 写入通知文件
    try:
        notification_file = PROJECTS_DIR / "shared_assets" / "notifications.log"
        with open(notification_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"时间: {datetime.now().isoformat()}\n")
            f.write(f"{message}\n")
        print(f"[通知] 已写入通知文件: {notification_file}")
    except Exception as e:
        print(f"[错误] 写入通知文件失败: {e}")

def format_health_report(health: dict) -> str:
    """格式化健康报告"""
    report = f"🤖 自动化系统健康检查\n"
    report += f"⏰ {health['timestamp']}\n\n"
    
    # 进程状态
    report += "进程状态:\n"
    report += f"  Tinder: {'✅ 运行中' if health['tinder_running'] else '❌ 未运行'}\n"
    report += f"  Bumble: {'✅ 运行中' if health['bumble_running'] else '❌ 未运行'}\n"
    report += f"  Orchestrator: {'✅ 运行中' if health['orchestrator_running'] else '❌ 未运行'}\n\n"
    
    # 错误
    if health["errors"]:
        report += f"⚠️ 发现 {len(health['errors'])} 个错误:\n"
        for error in health["errors"][:5]:
            report += f"  • {error[:100]}...\n"
    else:
        report += "✅ 无错误\n"
    
    return report

def main():
    print("=" * 60)
    print("Agent 协作监控脚本")
    print("=" * 60)
    
    # 检查健康状态
    health = check_system_health()
    
    # 生成报告
    report = format_health_report(health)
    print(report)
    
    # 判断是否需要通知
    should_notify = (
        not health["tinder_running"] or
        not health["bumble_running"] or
        len(health["errors"]) > 0
    )
    
    if should_notify:
        print("\n[警告] 发现问题，通知零号龙虾...")
        
        # 构造通知消息
        notification = (
            f"🚨 自动化系统异常报告\n\n"
            f"{report}\n\n"
            f"请转发给 Claude 进行诊断和修复。"
        )
        
        notify_dragonb00(notification)
    else:
        print("\n[正常] 系统运行正常")

if __name__ == "__main__":
    main()
