#!/usr/bin/env python3
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

profile_path = str(Path.home() / ".tinder-automation" / "browser-profile")
os.system(f'rm -f "{profile_path}/SingletonLock" 2>/dev/null')

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        profile_path, headless=False, viewport={'width': 390, 'height': 844}
    )
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://tinder.com")
    page.wait_for_timeout(5000)

    # 点击 Messages 入口
    try:
        page.locator('a[href*="/app/messages"]').first.click(timeout=10000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"点击消息入口: {e}")

    # 点击列表第一个对话
    try:
        page.locator('[class*="matchListItem"], [class*="conversation"]').first.click(timeout=10000)
    except Exception as e:
        print(f"点击第一个对话: {e}")
    page.wait_for_timeout(2000)

    # 导出核心聊天区域的 HTML
    html = page.locator('main').inner_html()
    with open("tinder_dom.txt", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ DOM 导出成功：{len(html)} 字符")
    browser.close()
