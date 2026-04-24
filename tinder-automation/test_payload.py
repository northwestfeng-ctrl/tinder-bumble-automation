#!/usr/bin/env python3
import sys
sys.path.insert(0, '/Users/chengang/.openclaw/workspace/projects/tinder-automation')
sys.path.insert(0, '/Users/chengang/.openclaw/workspace/projects/shared_assets')

from core.tinder_bot import TinderBot, CONFIG

bot = TinderBot(CONFIG)
bot.setup()
try:
    messages = [{"text": "What's up?", "sender": "them", "is_mine": False}]
    bio = "喜欢冲浪和咖啡"
    age = 25

    print("[TEST] 调用 generate_reply(dry_run=True)...")
    reply = bot.generate_reply(messages, bio=bio, age=age, dry_run=True)
    print(f"[TEST] 回复: {reply}")
finally:
    bot.cleanup()
