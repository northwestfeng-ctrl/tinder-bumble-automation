#!/usr/bin/env python3
"""检查 Bumble 登录状态和未读消息"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot

PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"

bot = BumbleBot(profile_path=str(PROFILE_PATH))
print("[1] 启动浏览器...")
bot.launch()
page = bot.page

print(f"\n[2] 当前 URL: {page.url}")

print("\n[3] 访问消息列表...")
page.goto("https://bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
time.sleep(6)
print(f"    URL: {page.url}")

# 打印页面标题
title = page.title()
print(f"    标题: {title}")

# 打印所有 conversation 相关的文本
print("\n[4] 扫描未读/Your Move 入口...")
your_move_texts = page.evaluate(r"""
    () => {
        const nodes = Array.from(document.querySelectorAll('*')).filter(el => {
            const t = el.innerText || '';
            return (t.includes('Your Move') || t.includes('轮到您了')) && t.length < 60;
        });
        return nodes.map(n => {
            const rect = n.getBoundingClientRect();
            return { text: n.innerText.trim().slice(0, 50), x: rect.left, y: rect.top, w: rect.width, h: rect.height };
        });
    }
""")
print(f"    Your Move 条目: {len(your_move_texts)}")
for t in your_move_texts[:5]:
    print(f"      [{t['x']:.0f},{t['y']:.0f}] {t['text']}")

# 打印侧边栏所有对话名称
all_convos = page.evaluate(r"""
    () => {
        const items = Array.from(document.querySelectorAll('[class*="conversationItem"], [class*="sidebarItem"], [class*="conversations"] div[role]'));
        const results = [];
        items.forEach(el => {
            const t = el.innerText.trim();
            if (t && t.length > 0 && t.length < 100) {
                const rect = el.getBoundingClientRect();
                results.push({ text: t.replace(/\n/g,' | ').slice(0,60), visible: rect.width > 0 });
            }
        });
        return results.filter(r => r.visible).slice(0, 20);
    }
""")
print(f"\n[5] 侧边栏对话({len(all_convos)}个可见):")
for c in all_convos[:10]:
    print(f"      {c['text']}")

# 检查是否有未读小红点
unread = page.evaluate(r"""
    () => {
        const dots = Array.from(document.querySelectorAll('[class*="unread"], [class*="notification-dot"], span[class*="count"]'));
        return dots.map(d => ({ text: d.innerText.trim(), cls: d.className.slice(0,40) })).filter(x => x.text);
    }
""")
print(f"\n[6] 未读标记: {unread[:5]}")

print("\n[7] 截图保存...")
page.screenshot(path="/Users/chengang/.openclaw/workspace/bumble_check.png", full_page=False)
print("    截图: /Users/chengang/.openclaw/workspace/bumble_check.png")

bot.close()
print("\n完成")