#!/usr/bin/env python3
"""
Tinder 自动化测试 - 验证登录状态和未读消息检测
"""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

def main():
    print("=" * 50)
    print("Tinder 自动化验证")
    print("=" * 50)
    
    user_data_dir = str(Path.home() / ".tinder-automation" / "browser-profile")
    storage_state = str(Path(user_data_dir) / "tinder_state.json")
    
    print(f"\n[1] 加载登录状态: {storage_state}")
    print(f"    存在: {Path(storage_state).exists()}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        context = browser.new_context(
            viewport={'width': 390, 'height': 844},
            storage_state=storage_state if Path(storage_state).exists() else None
        )
        
        page = context.new_page()
        
        print("\n[2] 打开 Tinder...")
        page.goto("https://tinder.com", timeout=30000)
        page.wait_for_timeout(3000)
        
        url = page.url
        print(f"    URL: {url}")
        
        # 截图
        page.screenshot(path="/tmp/tinder_main.png")
        print("    📸 截图: /tmp/tinder_main.png")
        
        if "login" in url.lower():
            print("\n❌ 未登录，需要重新登录")
        else:
            print("\n✅ 已登录")
            
            # 测试匹配列表
            print("\n[3] 测试匹配列表...")
            try:
                # 查找消息入口
                matches_link = page.wait_for_selector(
                    'a[href*="/messages"], button:has-text("Messages"), [class*="message"]',
                    timeout=5000
                )
                print("    ✅ 找到消息入口")
            except:
                print("    ⚠️ 未找到消息入口")
            
            # 截图看主界面
            page.screenshot(path="/tmp/tinder_home.png")
        
        print("\n[4] 保持浏览器打开，按 Enter 关闭...")
        try:
            input()
        except EOFError:
            pass
        
        context.close()
        browser.close()
    
    print("\n✅ 测试完成")

if __name__ == "__main__":
    main()
