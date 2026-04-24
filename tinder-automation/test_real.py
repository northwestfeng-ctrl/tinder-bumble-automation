#!/usr/bin/env python3
"""
Tinder Bot 真实环境测试
直接打开 Tinder.com
"""
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.sync_api import sync_playwright
from core.stealth_browser import StealthBrowser

def test_tinder_access():
    """测试 Tinder 是否可访问"""
    print("=" * 50)
    print("Tinder 真实环境测试")
    print("=" * 50)
    
    # 检查 VPN 状态
    print("\n[1] 检查网络状态...")
    import subprocess
    try:
        result = subprocess.run(['curl', '-sI', 'https://tinder.com', '--max-time', '10'], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            print("    ✅ Tinder 可访问")
        else:
            print("    ⚠️ Tinder 可能需要 VPN")
    except Exception as e:
        print(f"    ⚠️ 网络检查失败: {e}")
    
    print("\n[2] 启动浏览器...")
    user_data_dir = str(Path.home() / ".tinder-automation" / "browser-profile")
    os.makedirs(user_data_dir, exist_ok=True)
    
    try:
        with sync_playwright() as p:
            # 启动 Chromium
            browser = p.chromium.launch(
                headless=False,  # 显示浏览器窗口
                args=[
                    '--disable-blink-features=AutomationControlled',
                ]
            )
            
            # 创建上下文（带指纹保护）
            context = browser.new_context(
                viewport={'width': 390, 'height': 844},  # iPhone 尺寸
            )
            
            page = context.new_page()
            
            print("\n[3] 打开 Tinder...")
            page.goto("https://tinder.com", timeout=30000)
            print(f"    ✅ 页面加载: {page.title()}")
            
            # 等待一下让页面稳定
            page.wait_for_timeout(3000)
            
            # 截图
            screenshot_path = "/tmp/tinder_test.png"
            page.screenshot(path=screenshot_path)
            print(f"    📸 截图保存: {screenshot_path}")
            
            # 检查是否需要登录
            url = page.url
            if "login" in url.lower() or "auth" in url.lower():
                print("\n    ⚠️ 需要登录")
                print("    请在浏览器窗口中扫码登录")
                print("    登录后按 Enter 继续...")
                input()
                
                # 再次截图确认
                page.screenshot(path=screenshot_path)
                print(f"    📸 登录后截图: {screenshot_path}")
            else:
                print("\n    ✅ 已登录状态")
            
            # 测试基本交互
            print("\n[4] 测试页面交互...")
            try:
                # 等待主内容加载
                page.wait_for_timeout(2000)
                
                # 截图看看当前状态
                page.screenshot(path="/tmp/tinder_after_load.png")
                print("    ✅ 页面截图完成")
                
            except Exception as e:
                print(f"    ⚠️ 交互测试跳过: {e}")
            
            print("\n" + "=" * 50)
            print("测试完成")
            print("=" * 50)
            print(f"\n截图查看: open {screenshot_path}")
            
            # 保持浏览器打开
            print("\n浏览器保持打开中，按 Enter 关闭...")
            input()
            
            context.close()
            browser.close()
            
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_tinder_access()
