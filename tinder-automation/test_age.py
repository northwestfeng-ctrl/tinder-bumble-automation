#!/usr/bin/env python3
"""快速测试 age 提取"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from core.tinder_bot import TinderBot, CONFIG

print("[*] 启动浏览器...")
bot = TinderBot(CONFIG)
bot.setup()

print("[*] 打开 matches 页面...")
bot.page.goto("https://tinder.com/app/matches", timeout=15000)
bot.page.wait_for_timeout(3000)

# 收集前3个对话卡片
cards = bot.page.locator('a[href*="/app/messages/"]').all()[:3]
print(f"[*] 找到 {len(cards)} 个对话卡片")

for i, card in enumerate(cards):
    href = card.get_attribute('href') or ''
    if len(href) < 30:
        continue
    url = href if href.startswith('http') else f'https://tinder.com{href}'
    print(f"\n[*] 进入第 {i+1} 个对话: {url[-50:]}")
    bot.page.goto(url, timeout=15000)
    bot.page.wait_for_timeout(3000)

    age = bot._extract_match_age()
    bio = bot._extract_profile_bio()
    print(f"    age = {age}")
    print(f"    bio = {bio[:150]}")
    break

print("\n[*] 测试完成")