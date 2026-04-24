#!/usr/bin/env python3
"""
自动化系统监控脚本
定期检查系统健康状态，发现问题时通过 Telegram 通知
"""
import os
import sys
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime

# 配置
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # 你的 chat_id
PROJECTS_DIR = Path("/Users/chengang/.openclaw/workspace/projects")

def send_telegram_message(message: str):
    """发送 Telegram 消息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[警告] Telegram 未配置，跳过通知: {message}")
        return
    
    import urllib.request
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }).encode()
    
    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        print(f"[通知] 已发送 Telegram 消息")
    except Exception as e:
        print(f"[错误] Telegram 发送失败: {e}")

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

def check_log_errors(log_file: Path, last_check_time: float) -> list:
    """检查日志中的错误"""
    if not log_file.exists():
        return []
    
    errors = []
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                # 简单的时间戳解析（假设日志格式为 "YYYY-MM-DD HH:MM:SS ..."）
                if "ERROR" in line or "CRITICAL" in line:
                    errors.append(line.strip())
    except Exception as e:
        print(f"[错误] 读取日志失败: {e}")
    
    return errors[-10:]  # 只返回最近 10 条错误

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
    
    tinder_errors = check_log_errors(tinder_log, time.time() - 3600)  # 最近 1 小时
    bumble_errors = check_log_errors(bumble_log, time.time() - 3600)
    
    if tinder_errors:
        health["errors"].extend([f"[Tinder] {e}" for e in tinder_errors])
    if bumble_errors:
        health["errors"].extend([f"[Bumble] {e}" for e in bumble_errors])
    
    return health

def format_health_report(health: dict) -> str:
    """格式化健康报告"""
    report = f"🤖 *自动化系统健康检查*\n"
    report += f"⏰ 时间: {health['timestamp']}\n\n"
    
    # 进程状态
    report += "*进程状态:*\n"
    report += f"  Tinder: {'✅ 运行中' if health['tinder_running'] else '❌ 未运行'}\n"
    report += f"  Bumble: {'✅ 运行中' if health['bumble_running'] else '❌ 未运行'}\n"
    report += f"  Orchestrator: {'✅ 运行中' if health['orchestrator_running'] else '❌ 未运行'}\n\n"
    
    # 错误
    if health["errors"]:
        report += f"*⚠️ 发现 {len(health['errors'])} 个错误:*\n"
        for error in health["errors"][:5]:  # 只显示前 5 个
            report += f"  • {error[:100]}...\n"
    else:
        report += "*✅ 无错误*\n"
    
    return report

def main():
    print("=" * 60)
    print("自动化系统监控脚本")
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
        print("\n[警告] 发现问题，发送通知...")
        send_telegram_message(report)
    else:
        print("\n[正常] 系统运行正常")

if __name__ == "__main__":
    main()
