#!/usr/bin/env python3
"""调试聊天页 DOM 结构，找到正确的 Profile 元素"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from core.tinder_bot import TinderBot, CONFIG

print("[*] 启动浏览器...")
bot = TinderBot(CONFIG)
bot.setup()

# 进入一个具体聊天
print("[*] 打开具体聊天页...")
# 用匹配列表中的第一个
bot.page.goto("https://tinder.com/app/matches", timeout=15000)
bot.page.wait_for_timeout(3000)

cards = bot.page.locator('a[href*="/app/messages/"]').all()
if cards:
    href = cards[0].get_attribute('href')
    url = href if href.startswith('http') else f'https://tinder.com{href}'
    print(f"[*] 点击: {url[-60:]}")
    cards[0].click()
    bot.page.wait_for_timeout(5000)

print(f"[*] 当前URL: {bot.page.url}")

# 打印页面中所有包含年龄数字的元素
print("\n[*] 扫描页面中所有年龄候选元素...")
elements = bot.page.evaluate("""
    () => {
        // 找所有包含 "岁" 或年龄数字的元素
        const all = document.querySelectorAll('*');
        const candidates = [];
        all.forEach(el => {
            const text = el.innerText || '';
            if (text.match(/\\d{2}\\s*岁/) || text.match(/^\\d{2}$/)) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    candidates.push({
                        tag: el.tagName,
                        text: text.substring(0, 100),
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height)
                    });
                }
            }
        });
        return JSON.stringify(candidates.slice(0, 20));
    }
""")
print(elements)

# 打印 main 区域下的直接子元素
print("\n[*] main 区域结构:")
main_struct = bot.page.evaluate("""
    () => {
        const main = document.querySelector('main');
        if (!main) return 'NO MAIN';
        const children = [];
        main.querySelectorAll(':scope > *').forEach((el, i) => {
            children.push({
                i,
                tag: el.tagName,
                id: el.id || '',
                cls: el.className ? el.className.substring(0, 80) : '',
                text: (el.innerText || '').substring(0, 60)
            });
        });
        return JSON.stringify(children, null, 2);
    }
""")
print(main_struct)
print("[*] 完成")