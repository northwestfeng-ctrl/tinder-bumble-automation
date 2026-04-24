#!/usr/bin/env python3
"""调试 Bumble 等待"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"
bot = BumbleBot(profile_path=str(PROFILE_PATH))
bot.launch()
page = bot.page

# 直接进消息列表，不用 networkidle
page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(12)  # 足够长让 SPA 渲染

print(f"URL: {page.url}")

# 截图
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_v3_debug.png")

# locator 检查
for text in ["Your Move", "轮到您了", "Messages", "Messages Center"]:
    loc = page.locator(f'text="{text}"')
    cnt = loc.count()
    if cnt > 0:
        print(f"locator text='{text}': {cnt} 个")
        for i in range(min(cnt, 3)):
            el = loc.nth(i)
            try:
                vis = el.is_visible()
                txt = el.inner_text()
                print(f"  [{i}] visible={vis}: {txt[:60]}")
            except:
                pass

# 查找所有 div 的文本（包含 Your Move 的）
ym_in_page = page.evaluate("""
() => {
    const allText = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while (node = walker.nextNode()) {
        const t = node.textContent.trim();
        if ((t.includes('Your Move') || t.includes('轮到您了')) && t.length < 80) {
            allText.push({ text: t, parent: node.parentElement.tagName });
        }
    }
    return allText;
}
""")
print(f"\n页面文本中的 Your Move: {ym_in_page}")

# 尝试 locator 点击第一个
loc = page.locator('text="Your Move"')
if loc.count() > 0:
    print(f"\n尝试点击 Your Move...")
    try:
        loc.first.click(force=True, timeout=5000)
        time.sleep(4)
        print(f"点击后 URL: {page.url}")
    except Exception as e:
        print(f"点击失败: {e}")

bot.close()
