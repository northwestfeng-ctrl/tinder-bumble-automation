#!/usr/bin/env python3
"""调试 Bumble - 找 SPAN 的可点击父元素"""
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

# 找所有 SPAN 包含"轮到您了"的父级 a
result = page.evaluate("""
() => {
    const spans = Array.from(document.querySelectorAll('span')).filter(el => {
        const t = el.innerText || '';
        return t.includes('轮到您了') || t.includes('Your Move');
    });
    const results = [];
    for (const span of spans) {
        let parent = span;
        for (let i = 0; i < 10; i++) {
            parent = parent.parentElement;
            if (!parent) break;
            const tag = parent.tagName;
            if (tag === 'A') {
                const rect = parent.getBoundingClientRect();
                results.push({
                    href: parent.href,
                    text: parent.innerText.trim().slice(0, 60),
                    x: rect.left, y: rect.top,
                    w: rect.width, h: rect.height,
                    visible: rect.width > 10 && rect.height > 10
                });
                break;
            }
        }
    }
    return results;
}
""")

print(f"找到 {len(result)} 个 <a> 父元素:")
for i, r in enumerate(result):
    print(f"  [{i}] visible={r['visible']} ({r['x']:.0f},{r['y']:.0f}) {r['w']:.0f}x{r['h']:.0f}")
    print(f"      href={r['href']}")
    print(f"      text={r['text']}")

# 尝试点击第一个可见的 a
for r in result:
    if r['visible'] and r['href']:
        print(f"\n点击 href={r['href']}")
        try:
            # 直接用 page.goto 会触发路由，但可能需要先初始化 app
            page.goto(r['href'], timeout=15000, wait_until='domcontentloaded')
            time.sleep(5)
            print(f"  导航后 URL: {page.url}")
            if 'messages' in page.url or 'chat' in page.url:
                print(f"  ✅ 进入对话!")
                break
        except Exception as e:
            print(f"  失败: {e}")

bot.close()
