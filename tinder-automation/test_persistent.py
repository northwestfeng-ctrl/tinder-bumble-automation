#!/usr/bin/env python3
"""
Tinder 持久化登录测试
使用 launch_persistent_context 保持登录状态
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

def main():
    print("=" * 50)
    print("Tinder 持久化登录测试")
    print("=" * 50)
    
    profile_path = str(Path.home() / ".tinder-automation" / "browser-profile")
    os.makedirs(profile_path, exist_ok=True)
    
    print(f"\n[1] Profile 目录: {profile_path}")
    
    with sync_playwright() as p:
        # 使用 launch_persistent_context
        context = p.chromium.launch_persistent_context(
            profile_path,
            headless=False,
            args=['--disable-blink-features=AutomationControlled'],
            viewport={'width': 390, 'height': 844},
        )
        
        # launch_persistent_context 返回 context，自动有默认 page
        page = context.pages[0] if context.pages else context.new_page()
        
        print("\n[2] 打开 Tinder...")
        page.goto("https://tinder.com", timeout=30000)
        page.wait_for_timeout(3000)
        
        url = page.url
        print(f"    URL: {url}")
        
        if "login" in url.lower():
            print("\n🔐 需要登录")
            print("请在浏览器中手动登录，完成后告诉我")
        
        print("\n浏览器保持打开中...")
        
        try:
            input()  # 等待用户按 Enter
        except EOFError:
            pass
        
        context.close()
    
    print("\n✅ 完成")

if __name__ == "__main__":
    main()
