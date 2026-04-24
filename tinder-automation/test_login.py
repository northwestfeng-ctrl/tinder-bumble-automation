#!/usr/bin/env python3
"""
Tinder Bot - 登录测试
处理 Cookie 弹窗，等待手动登录
"""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright

def main():
    print("=" * 50)
    print("Tinder 登录测试")
    print("=" * 50)
    
    user_data_dir = str(Path.home() / ".tinder-automation" / "browser-profile")
    os.makedirs(user_data_dir, exist_ok=True)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        context = browser.new_context(
            viewport={'width': 390, 'height': 844},
            storage_state=None  # 不加载之前的登录状态
        )
        
        page = context.new_page()
        
        print("\n[1] 打开 Tinder...")
        page.goto("https://tinder.com", timeout=30000)
        print(f"    ✅ 页面加载: {page.title()}")
        
        # 处理 Cookie 弹窗
        print("\n[2] 处理 Cookie 弹窗...")
        try:
            # 尝试点击"我拒绝"按钮（最小化追踪）
            decline_button = page.wait_for_selector(
                'button:has-text("我拒绝"), button:has-text("I Decline"), button[aria-label*="Decline"]',
                timeout=5000
            )
            decline_button.click()
            print("    ✅ 已点击拒绝 Cookie")
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"    ⚠️ Cookie 弹窗处理: {e}")
        
        # 截图确认当前状态
        page.screenshot(path="/tmp/tinder_cookies.png")
        print("\n[3] 截图已保存: /tmp/tinder_cookies.png")
        
        # 检查是否需要登录
        url = page.url
        print(f"\n[4] 当前 URL: {url}")
        
        if "login" in url.lower() or "auth" in url.lower() or "account" in url.lower():
            print("\n🔐 需要登录")
            print("\n请在浏览器中完成以下操作：")
            print("  1. 点击'用手机号码登录'或'Google登录'")
            print("  2. 输入你的账号信息")
            print("  3. 完成验证")
            print("\n登录后按 Enter 继续...")
            input()
        
        # 保存登录状态
        print("\n[5] 保存登录状态...")
        storage_path = str(Path(user_data_dir) / "tinder_state.json")
        context.storage_state(path=storage_path)
        print(f"    ✅ 登录状态已保存: {storage_path}")
        
        # 最终截图
        page.screenshot(path="/tmp/tinder_logged_in.png")
        print("    📸 截图: /tmp/tinder_logged_in.png")
        
        print("\n" + "=" * 50)
        print("✅ 登录完成！下次运行将自动加载登录状态")
        print("=" * 50)
        
        print("\n浏览器保持打开中，按 Enter 关闭...")
        try:
            input()
        except EOFError:
            pass
        
        context.close()
        browser.close()

if __name__ == "__main__":
    main()
