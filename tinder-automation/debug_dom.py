#!/usr/bin/env python3
"""Tinder DOM 诊断 - 不走完整 Bot，直接打开面板打印结构"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.stealth_browser import StealthBrowser

with StealthBrowser() as page:
    print("🌐 打开 Tinder...")
    page.goto("https://tinder.com", wait_until="domcontentloaded")
    time.sleep(5)

    # 点击 Matches
    try:
        page.click('a[href*="/app/messages/"]', timeout=10000)
        print("✅ 进入消息列表")
    except Exception as e:
        print(f"❌ 进入消息列表失败: {e}")
        sys.exit(1)

    time.sleep(3)

    # 点击第一个未读卡片
    try:
        cards = page.locator('[class*="matchListItem"], [class*="conversation"]').all()
        print(f"📋 找到 {len(cards)} 个会话卡片")
        if cards:
            cards[0].click(force=True)
            print("✅ 点击了第一个卡片")
    except Exception as e:
        print(f"⚠️ 点击卡片: {e}")

    time.sleep(5)

    # 打印可见的候选容器
    print("\n=== DOM 结构诊断 ===")
    selectors_to_try = [
        'textarea', 'div[role="log"]', 'main', '[aria-live="polite"]',
        '[class*="messageList"]', '[class*="conversationView"]',
        '[class*="chatView"]', '[class*="message"]'
    ]
    for sel in selectors_to_try:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                tag = el.evaluate("node => node.tagName")
                cls = el.get_attribute("class") or ""
                role = el.get_attribute("role") or ""
                text = (el.inner_text() or "")[:80].replace("\n", " ")
                print(f"✅ [{sel}] tag={tag} role={role} class={cls[:60]} text={text}")
            else:
                print(f"⏳ [{sel}] 存在但不可见")
        except Exception as e:
            print(f"❌ [{sel}] 未找到: {type(e).__name__}")

    # 打印 main 的 innerHTML
    print("\n=== main innerHTML (前2000字符) ===")
    try:
        main_html = page.locator('main').first.inner_html()[:2000]
        print(main_html)
    except Exception as e:
        print(f"main 提取失败: {e}")

    print("\n=== textarea innerHTML (前1000字符) ===")
    try:
        txt = page.locator('textarea').first.inner_html()[:1000]
        print(txt)
    except Exception as e:
        print(f"textarea 提取失败: {e}")
