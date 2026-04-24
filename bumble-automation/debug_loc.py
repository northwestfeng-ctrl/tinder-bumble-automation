#!/usr/bin/env python3
"""调试 Bumble Your Move - 改用 locator 点击"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"

bot = BumbleBot(profile_path=str(PROFILE_PATH))
print("[1] 启动浏览器...")
bot.launch()
page = bot.page

print("[2] 访问消息列表...")
page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(8)
print(f"    URL: {page.url}")

# 截图
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_debug2.png")
print("    截图: bumble_debug2.png")

# 尝试 locator 方式查找"轮到您了"的对话
print("\n[3] 用 locator 查找 Your Move 对话...")

# 方法1：包含"轮到您了"的 div
ym_locator = page.locator('div:has-text("轮到您了")')
count = ym_locator.count()
print(f"    div:has-text('轮到您了') 数量: {count}")

for i in range(min(count, 5)):
    try:
        el = ym_locator.nth(i)
        txt = el.inner_text()
        visible = el.is_visible()
        print(f"    [{i}] visible={visible} text={txt[:60]}")
    except Exception as e:
        print(f"    [{i}] 错误: {e}")

# 方法2：找包含"轮到您了"的最近父级 a/div
print("\n[4] 尝试直接点击 Your Move 条目...")
try:
    # 点击第一个可见的"轮到您了"元素
    first_ym = ym_locator.first
    if first_ym.is_visible():
        # 找可点击的父级
        parent = first_ym.locator('xpath=ancestor::a[1]').first
        if parent.count() > 0 and parent.is_visible():
            print(f"    点击父级 a: {parent.get_attribute('href')}")
            parent.click(force=True)
            time.sleep(4)
            print(f"    点击后 URL: {page.url}")
        else:
            # 直接点击
            print(f"    直接点击 div")
            first_ym.click(force=True)
            time.sleep(4)
            print(f"    点击后 URL: {page.url}")
    else:
        print(f"    首个元素不可见")
except Exception as e:
    print(f"    点击失败: {e}")

bot.close()