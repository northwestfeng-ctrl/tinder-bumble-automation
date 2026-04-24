#!/usr/bin/env python3
"""调试 Bumble - 改用 locator 按文本点击"""
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
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_debug3.png")

# 列出所有侧边栏对话条目（按名字）
print("\n[3] 扫描侧边栏对话列表...")
convo_selectors = [
    "[class*='conversationItem']",
    "[class*='sidebarItem']",
    "[data-testid*='conversation']",
    "a[href*='/user/']",
]

all_names = set()
for sel in convo_selectors:
    items = page.locator(sel).all()
    for item in items:
        try:
            txt = item.inner_text()
            if txt and len(txt) > 0:
                first_line = txt.split('\n')[0].strip()
                if first_line and len(first_line) < 50:
                    all_names.add(first_line)
        except:
            pass

print(f"    侧边栏对话({len(all_names)}个):")
for name in sorted(all_names)[:15]:
    print(f"      {name}")

# 尝试点击某个对话
test_names = ["Ayumi", "Fecility", "Lois", "Zoe", "Kim"]
for name in test_names:
    print(f"\n[4] 尝试点击对话: {name}")
    try:
        # 方法1: locator by text
        loc = page.get_by_text(name, exact=False)
        if loc.count() > 0:
            print(f"    get_by_text('{name}') 找到 {loc.count()} 个")
            for i in range(min(loc.count(), 3)):
                el = loc.nth(i)
                tag = el.evaluate("el => el.tagName")
                vis = el.is_visible()
                print(f"    [{i}] <{tag}> visible={vis} text={el.inner_text()[:40]}")
                if vis:
                    href = el.get_attribute('href') or 'no-href'
                    print(f"        href={href}")
                    el.click(force=True)
                    time.sleep(4)
                    print(f"        点击后 URL: {page.url}")
                    if 'messages' in page.url or 'chat' in page.url:
                        print(f"        ✅ 进入对话")
                        break
                    else:
                        # 返回重试
                        page.goto("https://bumble.com/app/connections", timeout=20000, wait_until="domcontentloaded")
                        time.sleep(5)
        else:
            print(f"    get_by_text('{name}') 没找到")
    except Exception as e:
        print(f"    错误: {e}")

bot.close()