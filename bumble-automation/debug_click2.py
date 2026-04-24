#!/usr/bin/env python3
"""调试 Bumble - 用 Playwright locator 点击"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"
bot = BumbleBot(profile_path=str(PROFILE_PATH))
bot.launch()
page = bot.page

page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(12)

# 用 get_by_text 找"轮到您了"
ym = page.get_by_text("轮到您了", exact=False)
count = ym.count()
print(f"get_by_text count: {count}")

if count == 0:
    print("未找到，退出")
    bot.close()
    exit()

for i in range(count):
    el = ym.nth(i)
    try:
        vis = el.is_visible()
        txt = el.inner_text()
        tag = el.evaluate("el => el.tagName")
        print(f"  [{i}] <{tag}> visible={vis}: {txt[:50]}")
    except Exception as e:
        print(f"  [{i}] 错误: {e}")

# 尝试点击第一个
print("\n尝试点击第0个...")
try:
    url_before = page.url
    ym.first.click(timeout=5000, force=True)
    time.sleep(4)
    print(f"  点击后 URL: {page.url}")
    if page.url != url_before:
        print(f"  ✅ 进入对话")
    else:
        print(f"  URL 未变，尝试其他方式...")
        # 按名字找对话
        ayumi = page.get_by_text("Ayumi", exact=False).first
        if ayumi.is_visible():
            print(f"  找到 Ayumi，尝试点击")
            ayumi.click(force=True, timeout=5000)
            time.sleep(4)
            print(f"  Ayumi 点击后 URL: {page.url}")
except Exception as e:
    print(f"  点击失败: {e}")

# 截图
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_after_click.png")

bot.close()
