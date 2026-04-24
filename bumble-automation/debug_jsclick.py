#!/usr/bin/env python3
"""调试 Bumble - JS 上下文点击 + 等待对话页面"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"
bot = BumbleBot(profile_path=str(PROFILE_PATH))
bot.launch()
page = bot.page

page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(12)

# 获取第一个"轮到您了" span 的坐标并用 mouse.click
click_result = page.evaluate("""
() => {
    const spans = Array.from(document.querySelectorAll('span')).filter(el => {
        return (el.innerText || '').includes('轮到您了') || (el.innerText || '').includes('Your Move');
    });
    if (spans.length === 0) return { error: 'no spans found' };
    const span = spans[0];
    const rect = span.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, text: span.innerText };
}
""")

print(f"点击目标: {click_result}")

if 'error' not in click_result:
    print(f"用 mouse.click({click_result['x']}, {click_result['y']})...")
    page.mouse.click(click_result['x'], click_result['y'])
    time.sleep(4)

    # 检查 URL 变化
    print(f"点击后 URL: {page.url}")

    # 等待 textarea 出现（代表进入对话页面）
    try:
        page.wait_for_selector('textarea', timeout=8000)
        print("✅ textarea 出现，进入对话页面成功")
    except Exception as e:
        print(f"❌ textarea 未出现: {e}")

    # 截图
    page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_convo.png")

    # 也检查当前页面是否有消息气泡
    bubbles = page.evaluate("""
    () => {
        const bubbles = Array.from(document.querySelectorAll('[class*="bubble"], [class*="message"]'));
        return bubbles.slice(0, 3).map(b => b.innerText.trim().slice(0, 50));
    }
    """)
    print(f"消息气泡: {bubbles}")

bot.close()
