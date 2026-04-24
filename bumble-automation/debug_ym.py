#!/usr/bin/env python3
"""调试 Bumble Your Move 检测"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"

bot = BumbleBot(profile_path=str(PROFILE_PATH))
print("[1] 启动浏览器...")
bot.launch()
page = bot.page

print("[2] 访问消息列表...")
page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(10)
print(f"    URL: {page.url}")

# 截图
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_debug.png")
print("    截图: bumble_debug.png")

# 直接打印页面所有文本（debug）
all_text = page.evaluate("""
    () => {
        return Array.from(document.querySelectorAll('div, span, a, button'))
            .map(el => el.innerText.trim())
            .filter(t => t.length > 0 && t.length < 100)
            .filter((v, i, arr) => arr.indexOf(v) === i)
            .slice(0, 50);
    }
""")
print(f"\n[3] 页面文本(前50不重复):")
for t in all_text:
    print(f"    {t[:80]}")

# 扫描 Your Move
your_move = page.evaluate(r"""
    () => {
        const nodes = Array.from(document.querySelectorAll('*')).filter(el => {
            const text = el.innerText || '';
            return text.includes('Your Move') || text.includes('轮到您了');
        });
        return nodes.map(n => {
            const rect = n.getBoundingClientRect();
            return { text: n.innerText.trim().slice(0,50), x: rect.left, y: rect.top };
        });
    }
""")
print(f"\n[4] Your Move 节点: {len(your_move)}")
for n in your_move[:5]:
    print(f"    [{n['x']:.0f},{n['y']:.0f}] {n['text']}")

# 检查是否有遮罩层
overlays = page.evaluate(r"""
    () => {
        const modals = Array.from(document.querySelectorAll('[role="dialog"], .modal, .overlay'));
        return modals.map(m => ({ cls: m.className.slice(0,40), visible: m.offsetParent !== null }));
    }
""")
print(f"\n[5] 弹窗遮罩: {overlays}")

bot.close()