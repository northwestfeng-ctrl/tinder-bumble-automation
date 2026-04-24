#!/usr/bin/env python3
"""
测试抓取前5个对话，验证名字和消息能否正确抓取
"""
import os
import time
import json
import random
from pathlib import Path
from playwright.sync_api import sync_playwright


def get_name_from_page(page, target_href) -> str:
    """提取真实名字，使用 JS 原生正则解决换行符问题"""
    name = page.evaluate(r"""
        (href) => {
            const card = document.querySelector(`a[href="${href}"]`);
            if (card) {
                const cardText = card.innerText.trim();
                if (cardText) {
                    const lines = cardText.split(/\r?\n/);
                    for (let line of lines) {
                        let text = line.trim();
                        if (text && text.length > 0 && text.length < 20 && !text.includes('匹配')) {
                            return text;
                        }
                    }
                }
            }
            const profileLink = document.querySelector('a[href*="/app/profile/"]');
            if (profileLink && profileLink.innerText.trim()) {
                return profileLink.innerText.trim();
            }
            return '';
        }
    """, target_href)
    return name or f"unknown_{random.randint(1000,9999)}"


def extract_messages(page) -> list:
    """提取聊天消息，使用布局计算精准区分敌我，抗类名混淆"""
    time.sleep(2)
    return page.evaluate("""
        () => {
            const logs = Array.from(document.querySelectorAll('[role="log"]'));
            const chatLog = logs.find(l => l.id && l.id.includes('SC.chat')) || logs[0];
            if (!chatLog) return [];

            const spans = chatLog.querySelectorAll('span.text');
            const result = [];

            spans.forEach(span => {
                const text = span.innerText.trim();
                if (!text || text.length > 500) return;

                let is_mine = false;
                let current = span.parentElement;

                for (let i = 0; i < 7; i++) {
                    if (!current) break;
                    const style = window.getComputedStyle(current);
                    if (style.justifyContent === 'flex-end' || style.alignItems === 'flex-end' || style.alignSelf === 'flex-end') {
                        is_mine = true;
                        break;
                    }
                    current = current.parentElement;
                }

                if (!is_mine) {
                    const color = window.getComputedStyle(span).color;
                    if (color === 'rgb(255, 255, 255)') {
                        is_mine = true;
                    }
                }

                result.push({
                    text: text,
                    sender: is_mine ? 'me' : 'them',
                    is_mine: is_mine
                });
            });

            const unique = [];
            for (let i = 0; i < result.length; i++) {
                if (unique.length > 0 && unique[unique.length - 1].text === result[i].text) {
                    continue;
                }
                unique.push(result[i]);
            }
            return unique;
        }
    """)


def test_scrape_first_n(page, n=5):
    print(f"\n[Test] 开始测试抓取前 {n} 个对话...")

    print("[Wait] 等待对话列表...")
    try:
        page.wait_for_selector('a[href^="/app/messages/"]', timeout=12000)
        print("   [OK] 对话列表已出现")
    except:
        print("   [Warn] 超时，但仍继续...")

    seen = set()
    hrefs = []
    for card in page.locator('a[href^="/app/messages/"]').all():
        href = card.get_attribute('href') or ''
        if len(href) > 30 and href not in seen:
            seen.add(href)
            hrefs.append(href)
            if len(hrefs) >= n:
                break

    print(f"[Info] 获取到 {len(hrefs)} 个对话链接")

    results = []
    for idx, href in enumerate(hrefs[:n], 1):
        try:
            print(f"\n--- 对话 {idx}/{n} ---")
            print(f"[Click] {href}")
            page.locator(f'a[href="{href}"]').first.click(force=True)
            time.sleep(4)

            name = get_name_from_page(page, href)
            print(f"[Name] {name}")

            page.evaluate('() => { document.querySelectorAll("[role=log]").forEach(l => l.scrollTop = 0); }')
            time.sleep(1)

            messages = extract_messages(page)
            print(f"[Messages] {len(messages)} 条")
            if messages:
                print(f"   最新: {messages[-1]['text'][:50]}")

            results.append({
                "match_name": name,
                "match_index": idx,
                "match_url": href,
                "messages": messages,
            })

            time.sleep(random.uniform(2, 4))

        except Exception as e:
            print(f"[Error] {e}")
            continue

    return results


def main():
    profile_path = str(Path.home() / ".tinder-automation" / "browser-profile")
    lock = os.path.join(profile_path, "SingletonLock")
    os.system(f'rm -f "{lock}" 2>/dev/null')

    print("=" * 50)
    print("Tinder Scraper Test - First 5 Matches")
    print("=" * 50)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                profile_path,
                headless=False,
                viewport={'width': 1280, 'height': 800},
                args=['--disable-blink-features=AutomationControlled'],
            )
            page = context.pages[0] if context.pages else context.new_page()

            print("\n[Step 1] 导航到 Tinder...")
            page.goto("https://tinder.com/app/recs", timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(5)
            print(f"   [OK] URL: {page.url}")

            print("[Step 2] 切换到消息...")
            clicked = False
            for sel in [
                '[aria-label="消息"]', '[aria-label="Messages"]',
                'button:has-text("消息")', 'button:has-text("Messages ")',
                'button:has-text("Messages")',
                'a:has-text("消息")', 'a:has-text("Messages")',
                'div[role="tab"]:has-text("消息")', 'div[role="tab"]:has-text("Messages")',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click(force=True)
                        time.sleep(3)
                        print(f"   [Click] 命中: {sel}")
                        clicked = True
                        break
                except:
                    continue

            if not clicked:
                print("   [Warn] 未找到消息入口，直接导航...")
                page.goto("https://tinder.com/app/messages", timeout=20000)
                time.sleep(8)

            print(f"   [OK] URL: {page.url}")

            results = test_scrape_first_n(page, n=5)

            print(f"\n{'='*50}")
            print(f"[Result] 成功抓取 {len(results)} 个对话")
            for r in results:
                print(f"  - {r['match_name']}: {len(r['messages'])} 条消息")

            out_path = Path(__file__).parent / "test_output.json"
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"[Saved] -> {out_path}")

            context.close()

    except Exception as e:
        print(f"[Error] {e}")


if __name__ == "__main__":
    main()