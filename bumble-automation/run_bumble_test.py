#!/usr/bin/env python3
"""
Bumble 隔离测试入口
- 独立 Profile：~/.bumble-automation/test-profile
- 首次运行等待人工授权（短信/扫码）
- 登录成功后探测 DOM 并打印结构
"""
import os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"
os.makedirs(PROFILE_PATH, exist_ok=True)

BUMBLE_MAIN = "https://bumble.com/app"


def main():
    print("=" * 50)
    print("Bumble 隔离测试工程 (纯被动提取模式)")
    print("=" * 50)

    bot = BumbleBot(profile_path=str(PROFILE_PATH))

    # ── 1. 启动浏览器 ──────────────────────────────
    print("\n[Step 1] 启动 Chromium (stealth)...")
    bot.launch()
    page = bot.page

    # ── 2. 访问 Bumble，检测 Cloudflare 拦截 ───────
    print("\n[Step 2] 访问 Bumble.com，检测反爬...")
    page.goto(BUMBLE_MAIN, timeout=30000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(3)

    title = page.title()
    print(f"  页面标题: {title}")

    cloudflare_blocked = (
        "Just a moment" in page.content()
        or "Checking your browser" in page.content()
        or page.url.startswith("https://www.cloudflare.com")
    )
    if cloudflare_blocked:
        print("  ⚠️ Cloudflare 拦截检测！请换用 VPN 或改用 App 抓包方案")

    # ── 3. 检测登录态，未登录则等待人工授权 ───────
    print("\n[Step 3] 检测登录态...")
    not_authenticated = any(
        page.url.startswith(need_url)
        for need_url in ["https://bumble.com/authentication", "https://bumble.com/get-started"]
    )

    if not_authenticated:
        print(f"  未登录（当前: {page.url}）→ 等待人工完成验证...")
        print("  请在浏览器中完成以下步骤:")
        print("    1. 选择国家代码")
        print("    2. 输入手机号并接收验证码")
        print("    3. 输入验证码完成验证")
        print("  完成后回到终端按 ENTER 继续")
        input("  [按 ENTER 继续] ")

    page.goto(BUMBLE_MAIN, timeout=20000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(5)
    print(f"  ✅ 已登录，当前 URL: {page.url}")

    # ── 4. SPA 渲染缓冲：等待侧边栏完全加载 ───────
    print("\n[Step 4] 等待 SPA 渲染...")

    try:
        page.wait_for_selector("main", timeout=15000)
        print("  main 容器已出现")
    except Exception:
        pass

    time.sleep(5)

    for label in ["Conversations", "Messages", "Chats"]:
        try:
            page.wait_for_selector(f"[aria-label='{label}']", timeout=8000)
            print(f"  ✅ 找到侧边栏容器: [aria-label='{label}']")
            break
        except Exception:
            continue

    # ── 5. 进入聊天视图（被动模式）─────────────
    print("\n[Step 5] 进入聊天视图 (仅历史对话)...")

    entered = False

    # 1. 优先：历史列表中的"轮到您了"
    if bot.click_conversation(your_move_only=True):
        entered = True
        print(f"  ✅ 进入 Your Move 对话，当前 URL: {page.url}")

    # 2. 兜底：任意最新对话
    if not entered:
        bot.click_conversation()
        print(f"  ✅ 进入对话，当前 URL: {page.url}")

    # ── 6. 等待聊天视图渲染 ──────────────────────
    print("\n[Step 6] 等待聊天视图 DOM...")
    time.sleep(4)

    print("\n--- 输入框探测 ---")
    input_selectors = [
        "textarea[placeholder*='发消息']",
        "textarea[placeholder*='message']",
        "textarea",
        "div[contenteditable='true'][role='textbox']",
        "[data-qa='chat-input']",
        "div[contenteditable='true']",
    ]
    for sel in input_selectors:
        count = page.locator(sel).count()
        if count:
            print(f"  [{sel}] → {count} 个")
            try:
                placeholder = page.locator(sel).first.get_attribute('placeholder')
                if placeholder:
                    print(f"    placeholder: {placeholder}")
            except Exception:
                pass

    print("\n--- 消息容器探测 ---")
    msg_selectors = [
        "[role='log']",
        "[class*='messageList']",
        "[class*='chatLog']",
        "[class*='conversationView']",
        "[class*='messagesContainer']",
    ]
    for sel in msg_selectors:
        count = page.locator(sel).count()
        if count:
            print(f"  [{sel}] → {count} 个")

    # ── 7. 结构化提取（聊天+资料分离） ──────────────
    print("\n[Step 7] 结构化提取（聊天+资料分离）...")
    messages, profile_bio = bot.extract_and_separate()

    print(f"\n  聊天消息: {len(messages)} 条")
    me_count = sum(1 for m in messages if m['sender'] == 'me')
    them_count = len(messages) - me_count
    print(f"  其中 me: {me_count} / them: {them_count}")
    if messages:
        print(f"  前5条:")
        for m in messages[:5]:
            print(f"    [{m['sender']}] {m['text'][:60]}")

    if profile_bio:
        print(f"\n  对方资料: {profile_bio[:200]}")
    else:
        print("\n  对方资料: 未找到")

    print(f"\n  当前 URL: {page.url}")

    print("\n[Done] 探测完成，浏览器保持开启以便人工审查")
    input("  按 ENTER 关闭浏览器...")
    bot.close()


if __name__ == "__main__":
    main()
