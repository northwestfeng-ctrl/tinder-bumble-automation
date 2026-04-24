#!/usr/bin/env python3
"""
Tinder Bot - 验证持久化登录
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

profile_path = str(Path.home() / ".tinder-automation" / "browser-profile")

print("=" * 50)
print("Tinder 持久化验证")
print("=" * 50)

print(f"\n[1] 加载 Profile: {profile_path}")

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        profile_path,
        headless=False,
        args=['--disable-blink-features=AutomationControlled'],
        viewport={'width': 390, 'height': 844},
    )
    
    page = context.pages[0] if context.pages else context.new_page()
    
    print("\n[2] 打开 Tinder...")
    page.goto("https://tinder.com", timeout=30000)
    page.wait_for_timeout(3000)
    
    url = page.url
    print(f"    URL: {url}")
    
    if "login" not in url.lower():
        print("\n✅ 已登录！持久化成功")
    else:
        print("\n⚠️ 未登录，需要重新登录")
    
    page.screenshot(path="/tmp/tinder_verify.png")
    print("\n📸 截图: /tmp/tinder_verify.png")
    
    print("\n保持打开中，按 Enter 关闭...")
    try:
        input()
    except EOFError:
        pass
    
    context.close()

print("\n✅ 完成")
