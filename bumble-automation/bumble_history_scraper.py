#!/usr/bin/env python3
"""
Bumble History Scraper
- 基于已登录的 Bumble Web Profile 全量遍历左侧对话列表
- 逐个点击对话，提取历史消息
- 与现有 bumble_corpus_history.json 合并去重
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.bumble_bot import BumbleBot


PROFILE_PATH = Path.home() / ".bumble-automation" / "test-profile"
HISTORY_FILE = Path(__file__).parent / "bumble_corpus_history.json"
BASELINE_FILE = Path(__file__).parent / "history_baseline.json"
PENDING_FILE = Path(__file__).parent / "pending_corpus.jsonl"


def conversation_key(item: dict) -> str:
    match_id = (item or {}).get("match_id", "") or ""
    if match_id:
        return f"match_id:{match_id}"

    name = (item or {}).get("name", "") or (item or {}).get("match_name", "") or ""
    preview = (item or {}).get("preview", "") or ""
    if name and preview:
        return f"name_preview:{name}|{preview}"
    if name:
        return f"name:{name}"
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

    left_name = (left or {}).get("name", "") or (left or {}).get("match_name", "") or ""
    right_name = (right or {}).get("name", "") or (right or {}).get("match_name", "") or ""
    left_preview = (left or {}).get("preview", "") or ""
    right_preview = (right or {}).get("preview", "") or ""
    if left_name and right_name and left_name == right_name:
        if left_preview and right_preview and left_preview != right_preview:
            return False
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


def load_history() -> dict:
    if HISTORY_FILE.exists():
        data = _dedupe_conversations(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
        return {conversation_key(item): item for item in data}
    return {}


def save_history(conversations: list) -> None:
    HISTORY_FILE.write_text(
        json.dumps(_dedupe_conversations(conversations), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_baseline() -> dict:
    if BASELINE_FILE.exists():
        data = _dedupe_conversations(json.loads(BASELINE_FILE.read_text(encoding="utf-8")))
        return {conversation_key(item): item for item in data}
    return {}


def save_baseline(conversations: list) -> None:
    BASELINE_FILE.write_text(
        json.dumps(_dedupe_conversations(conversations), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def append_pending_increment(match_id: str, name: str, preview: str, bio: str, messages: list) -> None:
    if not messages:
        return
    payload = {
        "record_type": "incremental_messages",
        "match_id": match_id,
        "match_name": name,
        "name": name,
        "preview": preview,
        "bio": bio,
        "messages": messages,
    }
    with open(PENDING_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _js_collect_contacts() -> str:
    return r"""
    () => {
        const contacts = Array.from(document.querySelectorAll('.contact'))
            .filter(el => !el.innerText.includes('Beeline'))
            .map((el, idx) => {
                const rect = el.getBoundingClientRect();
                const nameEl = el.querySelector('.contact__name-text, .contact__name');
                const name = nameEl ? nameEl.innerText.trim() : '';
                const uid = el.getAttribute('data-qa-uid') || '';
                const lines = (el.innerText || '')
                    .split(/\n+/)
                    .map(x => x.trim())
                    .filter(Boolean);
                const preview = lines.slice(1).join(' | ');
                return {
                    idx,
                    uid,
                    name,
                    preview,
                    text: lines.join(' | '),
                    y: rect.top,
                    visible: rect.width > 0 && rect.height > 0,
                };
            })
            .filter(item => item.visible && item.name);
        return contacts;
    }
    """


def collect_all_contacts(page, max_rounds: int = 20) -> list[dict]:
    print("[Collect] 开始滚动左栏收集 Bumble 对话...")
    seen_keys = set()
    collected: list[dict] = []
    stable_rounds = 0
    last_count = 0

    for round_idx in range(max_rounds):
        batch = page.evaluate(_js_collect_contacts())
        for item in batch:
            key = f"{item['name']}|{item['preview']}"
            if key not in seen_keys:
                seen_keys.add(key)
                collected.append(item)

        print(f"  [滚动 {round_idx+1}] 当前可见 {len(batch)} 个，累计 {len(collected)} 个")

        if len(collected) == last_count:
            stable_rounds += 1
            if stable_rounds >= 3:
                print(f"[Collect] 左栏触底，共收集 {len(collected)} 个对话")
                break
        else:
            stable_rounds = 0
        last_count = len(collected)

        page.evaluate(
            r"""
            () => {
                const container =
                    document.querySelector('.sidebar__content .scrollbar__content') ||
                    document.querySelector('.sidebar__content') ||
                    document.querySelector('.contact-tabs') ||
                    document.querySelector('.contacts-list');
                if (container) {
                    container.scrollTop += 900;
                } else {
                    window.scrollBy(0, 900);
                }
            }
            """
        )
        time.sleep(1.5)

    return collected


def open_contact(page, name: str, preview: str) -> bool:
    payload = {"name": name, "preview": preview}
    return bool(page.evaluate(
        r"""
        ({ name, preview }) => {
            const contacts = Array.from(document.querySelectorAll('.contact'));
            const target = contacts.find(el => {
                const nameEl = el.querySelector('.contact__name-text, .contact__name');
                const currentName = nameEl ? nameEl.innerText.trim() : '';
                const lines = (el.innerText || '').split(/\n+/).map(x => x.trim()).filter(Boolean);
                const currentPreview = lines.slice(1).join(' | ');
                return currentName === name && currentPreview === preview;
            });
            if (!target) return false;
            target.scrollIntoView({ block: 'center' });
            target.click();
            return true;
        }
        """,
        payload,
    ))


def wait_for_messages(page, timeout_seconds: int = 12) -> bool:
    for _ in range(timeout_seconds):
        result = page.evaluate(
            r"""
            () => ({
                bubbles: document.querySelectorAll('.page--chat [class*="message-bubble"]').length,
                header: !!document.querySelector('.messages-header__name, .messages-header')
            })
            """
        )
        if result.get("bubbles", 0) > 0 or result.get("header"):
            return True
        time.sleep(1)
    return False


def scrape_all(page, bot: BumbleBot) -> list[dict]:
    print("[Step 1] 进入 Bumble 对话页...")
    page.goto("https://eu1.bumble.com/app/connections", timeout=30000, wait_until="domcontentloaded")
    time.sleep(6)
    print(f"[Step 1] 当前 URL: {page.url}")

    contacts = collect_all_contacts(page)
    if not contacts:
        print("[Error] 未找到任何 Bumble 对话项")
        return []

    print(f"[Step 2] 共 {len(contacts)} 个对话待抓取")
    history = load_history()
    baseline = load_baseline()
    print(f"[History] 已加载 {len(history)} 条历史记录")
    print(f"[Baseline] 已加载 {len(baseline)} 条增量基线")

    new_conversations = []
    for idx, item in enumerate(contacts, 1):
        name = item["name"]
        preview = item["preview"]
        match_id = item.get("uid") or f"{name}|{preview}"
        print(f"\n[{idx}/{len(contacts)}] -> {name}")

        if not open_contact(page, name, preview):
            print("   [Warn] 未能点击对话，跳过")
            continue

        if not wait_for_messages(page):
            print("   [Warn] 聊天面板未出现，跳过")
            continue

        time.sleep(2)
        result = bot.extract_messages_from_chat_panel()
        messages = result.get("messages", [])
        bio = result.get("match_bio", "")
        print(f"   [OK] {len(messages)} 条消息")

        if messages:
            new_conversations.append({
                "match_id": match_id,
                "name": name,
                "preview": preview,
                "bio": bio,
                "messages": messages,
            })

        time.sleep(1.5)

    merged = dict(history)
    incremental_count = 0
    for conv in new_conversations:
        conv_key = conversation_key(conv)
        prev = baseline.get(conv_key, {})
        delta_messages = compute_incremental_messages(prev.get("messages", []), conv.get("messages", []))
        if delta_messages:
            append_pending_increment(
                conv.get("match_id", ""),
                conv["name"],
                conv.get("preview", ""),
                conv.get("bio", ""),
                delta_messages,
            )
            incremental_count += 1
        merged[conv_key] = conv

    result = list(merged.values())
    save_history(result)
    save_baseline(result)
    print(f"\n[DONE] 抓取 {len(new_conversations)} 个对话（合并后共 {len(result)} 个）")
    print(f"[Incremental] 产出 {incremental_count} 个增量对话")
    total = sum(len(item.get("messages", [])) for item in result)
    print(f"[Stats] 总消息数: {total}")
    return result


def main() -> None:
    print("=" * 50)
    print("Bumble History Scraper")
    print("=" * 50)

    bot = BumbleBot(profile_path=str(PROFILE_PATH))
    try:
        print("[Launch] 启动 Bumble 浏览器上下文...")
        bot.launch()
        scrape_all(bot.page, bot)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
