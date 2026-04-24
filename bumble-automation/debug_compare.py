#!/usr/bin/env python3
"""调试 Bumble - 对比截图和 DOM"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"
bot = BumbleBot(profile_path=str(PROFILE_PATH))
bot.launch()
page = bot.page

page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(8)

# 截图
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_v3_check.png")

# 用 locator 找"轮到您了"
ym_locator = page.locator('text="轮到您了"')
count = ym_locator.count()
print(f"locator text='轮到您了' count={count}")

# 用 locator 找"Your Move"
ym2 = page.locator('text="Your Move"')
count2 = ym2.count()
print(f"locator text='Your Move' count={count2}")

# 尝试直接用 locator first 点击
for i in range(min(count, 3)):
    try:
        el = ym_locator.nth(i)
        vis = el.is_visible()
        tag = el.evaluate("el => el.tagName")
        print(f"  [{i}] <{tag}> visible={vis} text={el.inner_text()[:50]}")
        if vis:
            # 尝试点击
            url_before = page.url
            el.click(force=True, timeout=5000)
            time.sleep(3)
            print(f"    点击后 URL: {page.url} (变化={page.url != url_before})")
            if page.url != url_before:
                print(f"    ✅ 成功进入对话")
                break
            else:
                # 回退
                page.goto("https://bumble.com/app/connections", timeout=20000, wait_until="domcontentloaded")
                time.sleep(5)
    except Exception as e:
        print(f"  [{i}] 错误: {e}")

# 也试试 locator 找 a 标签里的"轮到您了"
print("\n检查 locator('a:has-text(\"轮到您了\")')...")
a_ym = page.locator('a:has-text("轮到您了")')
print(f"  count={a_ym.count()}")
for i in range(min(a_ym.count(), 3)):
    el = a_ym.nth(i)
    print(f"  [{i}] visible={el.is_visible()} href={el.get_attribute('href')}")

bot.close()
