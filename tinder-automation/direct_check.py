#!/usr/bin/env python3
"""
直接巡检脚本 — 跳过 cooldown，立刻检查并回复所有未回复消息
"""
import sys, os, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "shared_assets"))

from config import get_config
from core.tinder_bot import TinderBot, CONFIG

config = get_config()

print("=" * 50)
print("直接巡检模式（跳过 cooldown）")
print("=" * 50)

bot = TinderBot(CONFIG)
try:
    bot.setup()
    print("[Bot] 开始巡检...")
    count = bot.check_all_contacts()
    print(f"[Bot] ✅ 本轮回复 {count} 条")
finally:
    bot.cleanup()
