#!/usr/bin/env python3
"""诊断 Bumble DOM 结构"""
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

# 截图
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_dom.png")

# 打印"轮到您了" span 周围完整 DOM 树（向上5层）
result = page.evaluate("""
() => {
    const spans = Array.from(document.querySelectorAll('span')).filter(el => {
        const t = el.innerText || '';
        return t.includes('轮到您了') || t.includes('Your Move');
    });

    if (spans.length === 0) return { error: 'no spans' };

    // 从第一个 span 向上找5层，打印每层 tag/id/class
    const result = [];
    for (const span of spans.slice(0, 3)) {
        const layers = [];
        let current = span;
        for (let i = 0; i < 8; i++) {
            if (!current) break;
            layers.push({
                level: i,
                tag: current.tagName,
                id: current.id,
                cls: (current.className || '').slice(0, 50),
                text: (current.innerText || '').slice(0, 40),
                rect: (() => {
                    const r = current.getBoundingClientRect();
                    return { x: r.left, y: r.top, w: r.width, h: r.height };
                })()
            });
            current = current.parentElement;
        }
        result.push(layers);
    }
    return result;
}
""")

for i, layers in enumerate(result):
    print(f"\n=== Span {i} DOM 层级 ===")
    for layer in layers:
        print(f"  L{layer['level']} <{layer['tag']}> id={layer['id']} cls={layer['cls']}")
        print(f"       text={layer['text']}")
        print(f"       rect=({layer['rect']['x']:.0f},{layer['rect']['y']:.0f}) {layer['rect']['w']:.0f}x{layer['rect']['h']:.0f}")

bot.close()
