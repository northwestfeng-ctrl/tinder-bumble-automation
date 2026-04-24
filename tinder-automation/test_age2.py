#!/usr/bin/env python3
"""直接测试 _extract_match_age DOM 逻辑"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from core.tinder_bot import TinderBot, CONFIG

print("[*] 启动浏览器...")
bot = TinderBot(CONFIG)
bot.setup()

# 直接打开一个已知聊天 URL（用历史记录里的）
print("[*] 直接打开聊天页...")
bot.page.goto("https://tinder.com/app/messages/6492010f95232e0100bfd56d6961ca8ef3e6038ddc58aedb", timeout=15000)
bot.page.wait_for_timeout(5000)

url = bot.page.url
print(f"[*] 当前URL: {url}")

# 提取 age
age = bot._extract_match_age()
print(f"[*] age = {age}")

# 提取 bio
bio = bot._extract_profile_bio()
print(f"[*] bio = {bio[:200]}")

# 打印页面中 aside 的文本
aside_text = bot.page.evaluate("""
    () => {
        const aside = document.querySelector('aside');
        return aside ? aside.innerText.substring(0, 500) : 'NO ASIDE FOUND';
    }
""")
print(f"[*] aside内容: {aside_text}")

print("[*] 完成")