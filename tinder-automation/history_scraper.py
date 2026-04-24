#!/usr/bin/env python3
"""
Tinder History Scraper (强制深度遍历版)
- 无限滚动直到侧边栏完全加载
- 等待对话容器渲染后再提取
- 滚动聊天到顶部加载历史
- 与现有 corpus_history.json 合并去重
"""
import os
import time
import json
import random
import sqlite3
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))

from project_config import DEFAULT_PROFILE_DIR

STATE_FILE = Path(__file__).parent / "sync_state.json"
HISTORY_FILE = Path(__file__).parent / "corpus_history.json"
BASELINE_FILE = Path(__file__).parent / "history_baseline.json"
PENDING_FILE = Path(__file__).parent / "pending_corpus.jsonl"
DB_FILE = Path(__file__).parent / "conversation_log.db"


def conversation_key(item: dict) -> str:
    match_id = (item or {}).get("match_id", "") or ""
    if match_id:
        return f"match_id:{match_id}"

    match_name = (item or {}).get("match_name", "") or ""
    match_index = (item or {}).get("match_index", "") or ""
    if match_name and match_index != "":
        return f"name_index:{match_name}:{match_index}"
    if match_name:
        return f"name:{match_name}"
    return "unknown"


def _normalized_messages(messages: list) -> list[tuple[str, str]]:
    normalized = []
    for item in messages or []:
        sender = item.get("sender", "them")
        text = (item.get("text", "") or "").strip()
        normalized.append((sender, text))
    return normalized


def _same_conversation(left: dict, right: dict) -> bool:
    left_id = (left or {}).get("match_id", "") or ""
    right_id = (right or {}).get("match_id", "") or ""
    if left_id and right_id:
        return left_id == right_id

    left_name = (left or {}).get("match_name", "") or ""
    right_name = (right or {}).get("match_name", "") or ""
    left_index = (left or {}).get("match_index", "")
    right_index = (right or {}).get("match_index", "")
    if left_name and right_name and left_name == right_name and left_index == right_index:
        return _normalized_messages(left.get("messages", [])) == _normalized_messages(right.get("messages", []))

    return False


def _merge_conversation_entry(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key == "messages":
            if _normalized_messages(value) and (
                not _normalized_messages(merged.get("messages", []))
                or len(value) >= len(merged.get("messages", []))
            ):
                merged[key] = value
            continue
        if value not in ("", None, []):
            merged[key] = value
    return merged


def _dedupe_conversations(conversations: list) -> list:
    deduped: list[dict] = []
    for item in conversations or []:
        if not isinstance(item, dict):
            continue
        for idx, existing in enumerate(deduped):
            if _same_conversation(existing, item):
                deduped[idx] = _merge_conversation_entry(existing, item)
                break
        else:
            deduped.append(dict(item))
    return deduped


def is_logged_in(page) -> bool:
    """通过 URL + 页面内容判断是否已登录，排除 Javascript Disabled 等降级页。"""
    try:
        text = page.evaluate(
            """() => ((document.body && (document.body.innerText || document.body.textContent)) || '')
            .replace(/\\s+/g, ' ').trim().slice(0, 500)"""
        )
    except Exception:
        text = ""

    invalid_markers = (
        "Javascript is Disabled",
        "JavaScript is Disabled",
        "enable javascript",
        "please enable javascript",
        "Something went wrong",
        "Oops",
    )
    if any(marker.lower() in text.lower() for marker in invalid_markers):
        return False
    return "/app/" in page.url and bool(text)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_history() -> dict:
    """加载已有历史库，优先按稳定 match_id 建索引。"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = _dedupe_conversations(json.load(f))
        return {conversation_key(c): c for c in data}
    return {}


def save_history(conversations: list):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(_dedupe_conversations(conversations), f, ensure_ascii=False, indent=2)


def load_baseline() -> dict:
    if BASELINE_FILE.exists():
        with open(BASELINE_FILE, 'r', encoding='utf-8') as f:
            data = _dedupe_conversations(json.load(f))
        return {conversation_key(c): c for c in data}
    return {}


def save_baseline(conversations: list) -> None:
    with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
        json.dump(_dedupe_conversations(conversations), f, ensure_ascii=False, indent=2)


def compute_incremental_messages(previous: list, current: list) -> list:
    prev_norm = _normalized_messages(previous)
    curr_norm = _normalized_messages(current)

    if not prev_norm:
        return current
    if curr_norm == prev_norm:
        return []
    if len(curr_norm) >= len(prev_norm) and curr_norm[:len(prev_norm)] == prev_norm:
        return current[len(prev_norm):]
    return current


def append_pending_increment(match_id: str, match_name: str, messages: list, match_index: int) -> None:
    if not messages:
        return
    payload = {
        "record_type": "incremental_messages",
        "match_id": match_id,
        "match_name": match_name,
        "match_index": match_index,
        "messages": messages,
    }
    with open(PENDING_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_recent_match_ids(limit: int = 20) -> list[str]:
    """从本地会话库读取最近使用过的 match_id，作为消息页引导入口。"""
    if not DB_FILE.exists():
        return []

    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            """
            SELECT match_id
            FROM conversations
            WHERE match_id IS NOT NULL
              AND match_id != ''
              AND match_id NOT LIKE 'test_%'
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        return []

    seen = set()
    result = []
    for (match_id,) in rows:
        if match_id and match_id not in seen:
            seen.add(match_id)
            result.append(match_id)
    return result


def get_name_from_page(page, target_href) -> str:
    name = page.evaluate(r"""
        (href) => {
            const card = document.querySelector(`a[href="${href}"]`);
            if (card) {
                const cardText = card.innerText.trim();
                if (cardText) {
                    const lines = cardText.split(/\r?\n/);
                    for (let line of lines) {
                        let text = line.trim();
                        if (text && text.length > 0 && text.length < 20 && !text.includes('匹配') && !text.includes('Match')) {
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
    """提取聊天消息，布局计算精准区分敌我"""
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

            // 相邻去重
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


def scroll_sidebar_to_bottom(page, max_attempts=30):
    """
    无限滚动侧边栏，直到滚动位置不再变化（完全加载）
    返回本次获取到的卡片 href 列表
    """
    print("[Scroll] 开始无限滚动侧边栏...")
    last_count = 0
    stable_count = 0

    for attempt in range(max_attempts):
        # 滚动：优先滚动容器，否则滚动窗口
        page.evaluate("""
            () => {
                var sc = null;
                var cs = document.querySelectorAll('[class*=list], [class*=conversation], [class*=message], [class*=panel], [class*=item]');
                for (var i=0; i<cs.length; i++) {
                    var c = cs[i];
                    if (c.scrollHeight > c.clientHeight + 50) { sc = c; break; }
                }
                if (sc) sc.scrollTop += 600;
                else window.scrollBy(0, 600);
            }
        """)
        time.sleep(1.5)

        # 收集当前所有卡片（两种 href 格式都收集）
        raw = page.locator('a[href*="/app/messages/"]').all()
        current_count = len(raw)

        print(f"   [{attempt+1}] 检测到 {current_count} 个卡片 (上次 {last_count})")

        if current_count == last_count:
            stable_count += 1
            if stable_count >= 3:
                print(f"[Scroll] 侧边栏已到底，共 {current_count} 个卡片")
                break
        else:
            stable_count = 0

        last_count = current_count

    # 最后一次收集
    hrefs = []
    seen = set()
    for card in page.locator('a[href*="/app/messages/"]').all():
        href = card.get_attribute('href') or ''
        if len(href) > 30 and href not in seen:
            seen.add(href)
            hrefs.append(href)

    print(f"[Cards] 共 {len(hrefs)} 个对话卡片")
    return hrefs


def wait_for_chat_container(page, timeout=10):
    """等待右侧对话容器渲染完成"""
    selectors = [
        '[role="log"]',
        '[class*="messageContainer"]',
        '[class*="chatMessage"]',
        '[class*="conversationView"]',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=timeout * 1000):
                return True
        except:
            continue
    return False


def scroll_chat_to_top(page):
    """滚动聊天到最顶部（加载历史消息）"""
    page.evaluate("""
        () => {
            const logs = document.querySelectorAll('[role="log"]');
            logs.forEach(l => { l.scrollTop = 0; });
        }
    """)
    time.sleep(1)


def scroll_chat_load_history(page, max_scrolls=10):
    """
    滚动聊天容器向上加载历史，直到滚动位置不再变化
    """
    last_pos = -1
    for _ in range(max_scrolls):
        page.evaluate("""
            () => {
                const logs = document.querySelectorAll('[role="log"]');
                logs.forEach(l => { l.scrollTop -= 500; });
            }
        """)
        time.sleep(2)

        # 检测滚动位置是否到顶
        pos = page.evaluate("""
            () => {
                const log = document.querySelector('[role="log"]');
                return log ? log.scrollTop : 0;
            }
        """)
        if pos == last_pos or pos == 0:
            break
        last_pos = pos


def enter_messages_view(page) -> bool:
    """进入可见消息列表；优先走 /app/matches + Messages tab。"""
    try:
        if page.locator('a[href*="/app/messages/"]').count() > 0:
            return True
    except Exception:
        pass

    try:
        page.goto("https://tinder.com/app/matches", timeout=30000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)
        for sel in [
            'button[role="tab"]:has-text("Messages")',
            'button[role="tab"]:has-text("消息")',
            'button:has-text("Messages")',
            'button:has-text("消息")',
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click(force=True)
                    time.sleep(4)
                    if page.locator('a[href*="/app/messages/"]').count() > 0:
                        return True
            except Exception:
                continue
    except Exception:
        pass

    # 兜底：仅当 tab 入口失效时，才尝试本地已知 match_id 直达
    candidate_ids = load_recent_match_ids()
    for match_id in candidate_ids:
        try:
            msg_url = f"https://tinder.com/app/messages/{match_id}"
            page.goto(msg_url, timeout=20000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(4)
            link_count = page.locator('a[href*="/app/messages/"]').count()
            if link_count > 0:
                print(f"[Step 2] 使用本地 match_id 直达消息页成功: {match_id} (links={link_count})")
                return True
        except Exception:
            continue

    try:
        page.goto("https://tinder.com/app/matches", timeout=30000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
    except Exception:
        pass

    return False


def scrape_all_matches(page):
    """全量强制深度抓取"""
    print("\n[Step 1] 导航到 Tinder...")
    direct_bootstrap_ok = enter_messages_view(page)

    if not direct_bootstrap_ok:
        page.goto("https://tinder.com/app/recs", timeout=30000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(5)

        # ── URL-based login check ──
        if not is_logged_in(page):
            print(f"[Error] 未登录！当前 URL: {page.url}")
            return []

    print("[Step 2] 进入消息列表...")
    if not direct_bootstrap_ok and not enter_messages_view(page):
        print(f"[Warn] 无法稳定进入消息列表，当前 URL: {page.url}")
        try:
            body_preview = page.evaluate(
                """() => ((document.body && (document.body.innerText || document.body.textContent)) || '')
                .replace(/\\s+/g, ' ').trim().slice(0, 500)"""
            )
            print(f"[Warn] 页面摘要: {body_preview}")
        except Exception:
            pass

    # 无限滚动侧边栏直到完全加载
    hrefs = scroll_sidebar_to_bottom(page)

    if not hrefs:
        print("[Error] 未找到任何对话卡片")
        return []

    print(f"[Info] 共 {len(hrefs)} 个对话待抓取")

    # 加载历史库
    history = load_history()
    baseline = load_baseline()
    print(f"[History] 已加载 {len(history)} 条历史记录")
    print(f"[Baseline] 已加载 {len(baseline)} 条增量基线")

    # 抓取循环
    new_conversations = []
    for idx, href in enumerate(hrefs, 1):
        try:
            name = get_name_from_page(page, href)
            match_id = href.split("/app/messages/")[-1].split("?", 1)[0].strip("/")
            print(f"\n[{idx}/{len(hrefs)}] -> {name}")

            # 直接通过 URL 导航到对话（更可靠）
            msg_url = f"https://tinder.com{href}"
            page.goto(msg_url, timeout=20000)
            time.sleep(3)

            # 等待对话容器渲染
            if not wait_for_chat_container(page, timeout=15):
                print(f"   [Warn] 对话容器未出现，跳过")
                time.sleep(2)
                continue

            # 先滚动到顶部触发历史消息加载
            scroll_chat_to_top(page)
            time.sleep(2)
            scroll_chat_load_history(page, max_scrolls=10)

            # 提取消息
            messages = extract_messages(page)
            print(f"   [OK] {len(messages)} 条消息")

            if messages:
                new_conversations.append({
                    "match_id": match_id,
                    "match_name": name,
                    "match_index": idx,
                    "messages": messages,
                })

            time.sleep(random.uniform(2, 4))

        except Exception as e:
            print(f"   [Error] {e}")
            continue

    # ========== 合并去重 ==========
    # 用 match_name 匹配：新的覆盖旧的，新的追加
    merged = dict(history)  # copy existing
    incremental_count = 0
    for conv in new_conversations:
        conv_key = conversation_key(conv)
        prev = baseline.get(conv_key, {})
        delta_messages = compute_incremental_messages(prev.get("messages", []), conv.get("messages", []))
        if delta_messages:
            append_pending_increment(
                conv.get("match_id", ""),
                conv['match_name'],
                delta_messages,
                conv.get("match_index", 0),
            )
            incremental_count += 1
        merged[conv_key] = conv

    result = list(merged.values())
    save_history(result)
    save_baseline(result)

    print(f"\n[DONE] 抓取 {len(new_conversations)} 个对话（合并后共 {len(result)} 个）")
    print(f"[Incremental] 产出 {incremental_count} 个增量对话")
    total = sum(len(c['messages']) for c in result)
    print(f"[Stats] 总消息数: {total}")
    return result


def main():
    profile_path = str(DEFAULT_PROFILE_DIR)
    lock = os.path.join(profile_path, "SingletonLock")
    os.system(f'rm -f "{lock}" 2>/dev/null')

    print("=" * 50)
    print("Tinder History Scraper (强制深度遍历版)")
    print("=" * 50)

    try:
        with sync_playwright() as p:
            launch_options = {
                "headless": True,
                "viewport": {"width": 1280, "height": 800},
                "args": [
                    "--headless=new",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            context = p.chromium.launch_persistent_context(profile_path, **launch_options)
            page = context.pages[0] if context.pages else context.new_page()

            # 等待页面完全加载
            page.wait_for_load_state('domcontentloaded')
            time.sleep(3)

            scrape_all_matches(page)

            context.close()

    except KeyboardInterrupt:
        print("\n[Abort] 用户中断")
    except Exception as e:
        print(f"\n[Error] {e}")


if __name__ == "__main__":
    main()
