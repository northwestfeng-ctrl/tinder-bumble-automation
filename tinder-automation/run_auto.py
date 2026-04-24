#!/usr/bin/env python3
"""
Tinder Bot 自动化测试 - 真实环境
与 tinder_daemon.py 共用同一套 check_all_contacts 全自动遍历逻辑
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.tinder_bot import TinderBot, CONFIG

def main():
    print("=" * 50)
    print("Tinder Bot 真实自动化测试")
    print("=" * 50)

    bot = TinderBot(CONFIG)

    try:
        print("\n[1] 初始化浏览器...")
        bot.setup()

        print("\n[2] 打开 Tinder...")
        bot.page.goto("https://tinder.com", timeout=30000)
        bot.page.wait_for_timeout(3000)

        url = bot.page.url
        print(f"    URL: {url}")

        if "login" in url.lower():
            print("\n❌ 未登录，测试终止")
            return

        print("\n✅ 已登录")

        # 导航到消息页面
        print("\n[3] 导航到消息页面...")
        bot.navigate_to_messages()
        bot.page.wait_for_timeout(2000)
        bot.page.screenshot(path="/tmp/tinder_messages.png")
        print("    📸 截图: /tmp/tinder_messages.png")

        # 全自动分段遍历检测 + 发送（与 daemon 同一套逻辑）
        print("\n[4] 执行分段式遍历检测（检测未回复立刻发送）...")
        reply_count = bot.check_all_contacts()
        print(f"\n    本轮共触发 {reply_count} 次回复")

        if reply_count == 0:
            print("\n    无未回复消息，进入休眠等待")
        else:
            print(f"\n    ✅ 成功回复 {reply_count} 条消息")

        print("\n" + "=" * 50)
        print("✅ 测试完成")
        print("=" * 50)

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\n浏览器保持打开...")
        try:
            input("按 Enter 关闭...")
        except EOFError:
            pass

        bot.cleanup()

if __name__ == "__main__":
    main()
