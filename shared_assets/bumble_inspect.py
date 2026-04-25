#!/usr/bin/env python3
"""
bumble_inspect.py — 由 unified_orchestrator 调用，subprocess 隔离规避 asyncio 冲突

重要约束
--------
- 回复生成、业务闸门、缓存、baseline 记忆 等回复逻辑必须与 Tinder 共享。
- 联系人搜寻/优先级保持 Bumble 平台原生策略：
  只基于 "Your Move / 轮到您了" 候选顺序遍历，
  不套用 Tinder 的 13 + 5 滚动窗口。
"""
import sys, time, random, json, logging, importlib.util, types
from datetime import datetime, timedelta
from pathlib import Path

# 路径推导（相对于本文件位置）
_SELF = Path(__file__).resolve()
SHARED_DIR  = _SELF.parent
TINDER_DIR   = SHARED_DIR.parent / "tinder-automation"
BUMBLE_DIR   = SHARED_DIR.parent / "bumble-automation"
sys.path.insert(0, str(SHARED_DIR))

from atomic_state import read_json_file, update_json_file, write_json_file
from queue_db import get_reply, confirm_sent
from unified_send_message import get_last_send_diagnostics
from unified_reply_engine import (
    generate_reply, load_strategy,
    classify_partner_followup_quality,
    is_fallback_reply,
    sanitize_messages_for_context,
    should_reply_to_messages,
    should_attempt_reactivation,
    click_contact, wait_for_chat_ready, back_to_list,
)
from runtime_feedback import record_runtime_feedback
from conversation_store import ConversationStore, MISSING_SNAPSHOT_KEY, outcome_from_partner_followup

log = logging.getLogger("BumbleInspect")


def _load_bumble_bot_class():
    """用独立模块名加载 Bumble Bot，避免与 Tinder 的 core 包共享命名空间。"""
    package_name = "bumble_core"
    module_name = f"{package_name}.bumble_bot"
    module_path = BUMBLE_DIR / "core" / "bumble_bot.py"
    if module_name in sys.modules:
        return sys.modules[module_name].BumbleBot

    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(BUMBLE_DIR / "core")]
        sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 BumbleBot: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.BumbleBot


BumbleBot = _load_bumble_bot_class()

BUMBLE_PROFILE   = str(Path.home() / ".bumble-automation" / "test-profile")
CORPUS_FILE      = BUMBLE_DIR / "pending_corpus.jsonl"
BASELINE_FILE    = BUMBLE_DIR / "history_baseline.json"
RUNTIME_STATE_FILE = BUMBLE_DIR / "bumble_runtime_state.json"
DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND = 3
DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND = 15
SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS = 24
CORPUS_STORE = ConversationStore()


def _persisted_snapshot_key(snapshot_key: str, *, context: str = "") -> str:
    key = str(snapshot_key or "").strip()
    if key:
        return key
    label = f"({context})" if context else ""
    log.warning(f"[Bumble][语料飞轮] 快照写入失败{label}，后续 outcome 将跳过精确关联")
    return MISSING_SNAPSHOT_KEY

BUMBLE_ENTRIES_JS = r"""
() => {
    const spans = Array.from(document.querySelectorAll('span')).filter(el => {
        const t = el.innerText || '';
        return (t.includes('Your Move') || t.includes('轮到您了')) && t.length < 60;
    });
    const seen = new Set();
    const results = [];
    for (const span of spans) {
        let contactEl = span;
        for (let i = 0; i < 8; i++) {
            contactEl = contactEl.parentElement;
            if (!contactEl) break;
            if (contactEl.classList && contactEl.classList.contains('contact')) break;
        }
        if (!contactEl || !contactEl.classList.contains('contact')) continue;
        const topKey = Math.round(contactEl.getBoundingClientRect().top);
        if (seen.has(topKey)) continue;
        seen.add(topKey);
        const nameEl = contactEl.querySelector('.contact__name');
        const name = nameEl ? nameEl.innerText.trim().split('\n')[0] : '?';
        const uid  = contactEl.getAttribute('data-qa-uid') || '';
        const rect = span.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5 || rect.top < 100) continue;
        results.push({
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
            name,
            text: span.innerText.trim(),
            uid,
        });
    }
    return results;
}
"""

BUMBLE_DORMANT_ENTRIES_JS = r"""
() => {
    const results = [];
    const seen = new Set();
    const contacts = Array.from(document.querySelectorAll('.contact'));
    for (const contactEl of contacts) {
        const rect = contactEl.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 5 || rect.top < 100) continue;
        const text = (contactEl.innerText || '').trim();
        if (!text) continue;
        if (text.includes('Your Move') || text.includes('轮到您了')) continue;
        const uid = contactEl.getAttribute('data-qa-uid') || '';
        const nameEl = contactEl.querySelector('.contact__name');
        const name = nameEl ? nameEl.innerText.trim().split('\\n')[0] : '?';
        const key = uid || `${name}:${Math.round(rect.top)}`;
        if (seen.has(key)) continue;
        seen.add(key);
        results.push({
            x: rect.left + rect.width / 2,
            y: rect.top + Math.min(rect.height / 2, 48),
            name,
            text,
            uid,
        });
    }
    return results.slice(0, 12);
}
"""

BUMBLE_SCROLL_CONTACTS_JS = r"""
() => {
    let sc = null;
    const contacts = Array.from(document.querySelectorAll('.contact'));
    for (const contact of contacts) {
        let node = contact.parentElement;
        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
            if (node.scrollHeight > node.clientHeight + 50) {
                sc = node;
                break;
            }
        }
        if (sc) break;
    }
    if (!sc) {
        const containers = document.querySelectorAll('[class*=list], [class*=panel], [class*=conversation], [class*=contacts]');
        for (const c of containers) {
            if (c.scrollHeight > c.clientHeight + 50) {
                sc = c;
                break;
            }
        }
    }
    const before = sc ? sc.scrollTop : window.scrollY;
    if (sc) {
        sc.scrollTop += Math.max(500, sc.clientHeight * 0.8);
        return sc.scrollTop > before + 5;
    }
    window.scrollBy(0, 500);
    return window.scrollY > before + 5;
}
"""


def _write_corpus(
    match_name: str,
    match_id: str,
    messages: list,
    reply: str,
    *,
    intent: str = "reply",
    outcome: float = 0.5,
    outcome_label: str | None = None,
) -> str:
    """将成功回复的对话上下文追加写入 pending_corpus.jsonl，返回 snapshot_key"""
    try:
        _, snapshot_key = CORPUS_STORE.store(
            match_id,
            match_name,
            messages,
            reply,
            intent=intent,
            outcome=outcome,
            outcome_label=outcome_label,
            platform="bumble",
        )
        result_key = snapshot_key
    except Exception:
        result_key = ""

    entry = {
        "record_type": "replied_conversation_snapshot",
        "platform": "bumble",
        "match_id": match_id,
        "match_name": match_name,
        "messages": messages,
        "reply": reply,
        "intent": intent,
        "outcome": outcome,
        "outcome_label": outcome_label,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if result_key:
        entry["snapshot_key"] = result_key
    try:
        with open(CORPUS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 落盘失败不影响主流程
    return result_key


def _trim_trailing_fallback_messages(messages: list) -> list:
    """尾部若是统一兜底回复，则视为待重试，不纳入本轮有效上下文。"""
    effective = list(messages or [])
    while effective:
        last = effective[-1] or {}
        if last.get("sender") != "me" and not last.get("is_mine"):
            break
        if not is_fallback_reply(last.get("text", "")):
            break
        effective.pop()
    return effective


def _conversation_key(match_id: str = "", match_name: str = "") -> str:
    if match_id:
        return f"match_id:{match_id}"
    if match_name:
        return f"name:{match_name}"
    return "unknown"


def _normalized_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _parse_baseline_timestamp(raw_value: str) -> datetime | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _inbound_signature(messages: list[dict]) -> tuple[str, ...]:
    inbound = []
    for msg in messages or []:
        sender = msg.get("sender", "")
        if sender == "me" or msg.get("is_mine") is True:
            continue
        text = _normalized_text(msg.get("text", ""))
        if text:
            inbound.append(text)
    return tuple(inbound)


def _restore_inbound_signature(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return tuple()
    restored = []
    for item in raw:
        text = _normalized_text(str(item or ""))
        if text:
            restored.append(text)
    return tuple(restored)


def _load_incremental_baseline() -> tuple[list, dict]:
    data = read_json_file(BASELINE_FILE, default=[])
    if isinstance(data, list):
        keyed = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            key = _conversation_key(
                str(item.get("match_id", "") or ""),
                str(item.get("name") or item.get("match_name") or ""),
            )
            keyed[key] = item
        return data, keyed
    return [], {}


def _update_incremental_baseline(
    match_id: str,
    match_name: str,
    messages: list,
    handled_inbound_signature: tuple[str, ...] | None = None,
    handled_inbound_reason: str = "",
    metadata: dict | None = None,
) -> None:
    try:
        def mutate(data):
            keyed = {}
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    item_key = _conversation_key(
                        str(item.get("match_id", "") or ""),
                        str(item.get("name") or item.get("match_name") or ""),
                    )
                    keyed[item_key] = item

            key = _conversation_key(match_id, match_name)
            existing = keyed.get(key) or {}
            entry = {
                "match_id": match_id,
                "match_name": match_name,
                "name": match_name,
                "messages": messages,
            }
            if handled_inbound_signature is not None:
                entry["last_handled_inbound_signature"] = list(handled_inbound_signature)
                entry["last_handled_inbound_reason"] = handled_inbound_reason
                entry["last_handled_inbound_text"] = handled_inbound_signature[-1] if handled_inbound_signature else ""
                entry["last_handled_inbound_at"] = datetime.now().isoformat()
            else:
                for preserved_key in (
                    "last_handled_inbound_signature",
                    "last_handled_inbound_reason",
                    "last_handled_inbound_text",
                    "last_handled_inbound_at",
                ):
                    if preserved_key in existing:
                        entry[preserved_key] = existing.get(preserved_key)

            for preserved_key in (
                "dormant_signature",
                "dormant_since",
                "last_reactivation_signature",
                "last_reactivation_at",
                "last_reactivation_reason",
                "reactivation_attempt_count",
            ):
                if preserved_key in existing:
                    entry[preserved_key] = existing.get(preserved_key)

            for meta_key, meta_value in (metadata or {}).items():
                if meta_value is None:
                    entry.pop(meta_key, None)
                else:
                    entry[meta_key] = meta_value
            keyed[key] = entry
            return list(keyed.values())

        update_json_file(BASELINE_FILE, mutate, default=[])
    except Exception as exc:
        log.warning(f"[Bumble] 更新 history_baseline 失败: {exc}")


def _record_partner_followup_if_needed(match_id: str, match_name: str, messages: list, prev_entry: dict | None = None) -> None:
    prev_entry = prev_entry or {}
    handled_reason = str(prev_entry.get("last_handled_inbound_reason", "") or "")
    if handled_reason not in {"replied", "opened", "reactivated"}:
        return

    current_inbound = _inbound_signature(messages)
    if not current_inbound:
        return

    recorded_signature = _restore_inbound_signature(prev_entry.get("last_partner_followup_signature"))
    if current_inbound == recorded_signature:
        return

    event, reason = classify_partner_followup_quality(messages, platform="bumble")
    record_runtime_feedback(
        "bumble",
        match_id,
        match_name,
        event,
        intent="partner_followup",
        reason=reason,
        messages=messages,
    )
    outcome_info = outcome_from_partner_followup(event)
    if outcome_info:
        outcome, outcome_label = outcome_info
        snapshot_key = str(prev_entry.get("last_snapshot_key", "") or "")
        if snapshot_key and snapshot_key != MISSING_SNAPSHOT_KEY:
            try:
                CORPUS_STORE.update_outcome(
                    match_id,
                    outcome,
                    outcome_label,
                    platform="bumble",
                    snapshot_key=snapshot_key,
                )
            except Exception as exc:
                log.warning(f"[Bumble][语料飞轮] 更新后续回应结果失败: {exc}")
        elif snapshot_key == MISSING_SNAPSHOT_KEY:
            log.warning("[Bumble][语料飞轮] 缺少有效 snapshot_key，跳过后续回应结果精确回写")
        else:
            log.info("[Bumble][语料飞轮] 历史 baseline 无 snapshot_key，跳过 outcome 精确回写")
        _append_feedback_snapshot(
            match_id,
            match_name,
            messages,
            outcome=outcome,
            outcome_label=outcome_label,
            feedback_event=event,
            feedback_reason=reason,
            snapshot_key=snapshot_key if snapshot_key and snapshot_key != MISSING_SNAPSHOT_KEY else "",
        )
    _update_incremental_baseline(
        match_id,
        match_name,
        messages,
        metadata={
            "last_partner_followup_signature": list(current_inbound),
            "last_partner_followup_event": event,
            "last_partner_followup_reason": reason,
        },
    )


def _append_feedback_snapshot(
    match_id: str,
    match_name: str,
    messages: list,
    *,
    outcome: float,
    outcome_label: str,
    feedback_event: str,
    feedback_reason: str,
    snapshot_key: str = "",
) -> None:
    entry = {
        "record_type": "conversation_feedback_snapshot",
        "platform": "bumble",
        "match_id": match_id,
        "match_name": match_name,
        "messages": messages,
        "outcome": outcome,
        "outcome_label": outcome_label,
        "feedback_event": feedback_event,
        "feedback_reason": feedback_reason,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if snapshot_key:
        entry["snapshot_key"] = snapshot_key
    try:
        with open(CORPUS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_runtime_state() -> dict:
    data = read_json_file(RUNTIME_STATE_FILE, default={})
    return data if isinstance(data, dict) else {}


def _save_runtime_state(state: dict) -> None:
    try:
        write_json_file(RUNTIME_STATE_FILE, state if isinstance(state, dict) else {})
    except Exception:
        pass


def _dormant_entry_key(entry: dict) -> str:
    return str(entry.get("uid") or "") or str(entry.get("name") or "")


def _rotate_dormant_entries(entries: list[dict]) -> tuple[list[dict], str]:
    total = len(entries)
    if total <= 1:
        return entries, ""
    state = _load_runtime_state()
    last_key = str(state.get("last_dormant_scan_key", "") or "")
    if not last_key:
        return entries, ""
    for index, entry in enumerate(entries):
        if _dormant_entry_key(entry) == last_key:
            next_index = (index + 1) % total
            if next_index == 0:
                return entries, last_key
            return entries[next_index:] + entries[:next_index], last_key
    return entries, ""


def _advance_dormant_scan_cursor(last_attempted_key: str) -> None:
    if not last_attempted_key:
        return

    def mutate(state):
        state = state if isinstance(state, dict) else {}
        state["last_dormant_scan_key"] = last_attempted_key
        state.pop("dormant_scan_cursor", None)
        return state

    try:
        update_json_file(RUNTIME_STATE_FILE, mutate, default={})
    except Exception:
        pass


def _is_new_messages(match_id: str, match_name: str, messages: list) -> bool:
    messages = sanitize_messages_for_context(_trim_trailing_fallback_messages(messages))
    if not messages:
        return False

    latest_text = _normalized_text(messages[-1].get("text", ""))
    if not latest_text:
        return False

    latest_sender = messages[-1].get("sender", "")
    last_recorded_text = None
    _, keyed = _load_incremental_baseline()
    prev_entry = keyed.get(_conversation_key(match_id, match_name))
    if prev_entry is None:
        return True

    prev_messages = sanitize_messages_for_context(
        _trim_trailing_fallback_messages((prev_entry or {}).get("messages", []))
    )
    if prev_messages:
        last_recorded_text = _normalized_text(prev_messages[-1].get("text", ""))

    current_inbound = _inbound_signature(messages)
    recorded_inbound = _inbound_signature(prev_messages)
    handled_inbound = _restore_inbound_signature((prev_entry or {}).get("last_handled_inbound_signature"))
    handled_reason = str((prev_entry or {}).get("last_handled_inbound_reason", "") or "")
    handled_text = _normalized_text((prev_entry or {}).get("last_handled_inbound_text", ""))
    if not handled_text and handled_inbound:
        handled_text = handled_inbound[-1]
    handled_at = _parse_baseline_timestamp((prev_entry or {}).get("last_handled_inbound_at", ""))

    if (
        latest_sender == "them"
        and handled_reason.startswith("skipped:")
        and handled_text
        and latest_text == handled_text
    ):
        if not handled_reason.startswith("skipped:no_safe"):
            should_send_now, updated_reason = should_reply_to_messages(messages, platform="bumble")
            if should_send_now:
                log.info(f"[Bumble] 旧跳过规则已失效，重新放行该入站: {latest_text[:30]}...")
                return True
            log.info(f"[Bumble] 同一条旧跳过入站复核后仍不应回复: {updated_reason}")
        if handled_at is None:
            log.info(f"[Bumble] 同一条跳过入站缺少时间戳，按已处理跳过重试: {latest_text[:30]}...")
            return False
        if (datetime.now() - handled_at) < timedelta(hours=SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS):
            remaining = timedelta(hours=SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS) - (datetime.now() - handled_at)
            remaining_hours = max(int(remaining.total_seconds() // 3600), 0)
            log.info(f"[Bumble] 同一条跳过入站仍在冷却中（{handled_reason}），约剩 {remaining_hours}h，跳过重试: {latest_text[:30]}...")
            return False
        if handled_reason.startswith("skipped:no_safe"):
            log.info(f"[Bumble] 无安全回复冷却已结束，重新放行该入站: {latest_text[:30]}...")
            return True

    if len(current_inbound) > len(recorded_inbound):
        _record_partner_followup_if_needed(match_id, match_name, messages, prev_entry)
        log.info(f"[Bumble] 检测到入站消息条数增加: {len(recorded_inbound)} -> {len(current_inbound)}")
        return True

    if handled_inbound and current_inbound == handled_inbound:
        log.info(f"[Bumble] 最近入站已处理，跳过重复放行: {handled_reason or (current_inbound[-1] if current_inbound else 'empty')}")
        return False

    def _tail_signature(items: list, size: int = 3) -> tuple:
        tail = items[-size:]
        normalized = []
        for item in tail:
            normalized.append((item.get("sender", ""), _normalized_text(item.get("text", ""))))
        return tuple(normalized)

    current_sig = _tail_signature(messages)
    recorded_sig = _tail_signature(prev_messages)
    if current_sig == recorded_sig:
        if latest_sender == "them":
            log.info(f"[Bumble] 最近消息签名未变，但当前仍是未回复入站消息，放行: {current_sig[-1] if current_sig else 'empty'}")
            return True
        log.info(f"[Bumble] 最近消息签名未变（已回复过），跳过: {current_sig[-1] if current_sig else 'empty'}")
        return False

    if latest_sender != "them" and current_inbound == recorded_inbound:
        log.info("[Bumble] 仅我方消息/历史污染发生变化，未检测到新的入站消息")
        return False

    if latest_text == last_recorded_text:
        if latest_sender == "them":
            log.info(f"[Bumble] 最新文本未变，但当前仍是未回复入站消息，放行: {latest_text[:30]}...")
            return True
        log.info(f"[Bumble] 最新消息未变（已回复过），跳过: {latest_text[:30]}...")
        return False

    return True


def _record_replied_conversation(match_name: str, match_id: str, messages: list, reply: str) -> str:
    return _write_corpus(match_name, match_id, messages, reply, intent="reply")


def _tail_signature(items: list, size: int = 3) -> tuple:
    tail = items[-size:]
    normalized = []
    for item in tail:
        normalized.append((item.get("sender", ""), _normalized_text(item.get("text", ""))))
    return tuple(normalized)


def _get_dormant_reactivation_candidate(match_id: str, match_name: str, messages: list) -> tuple[bool, str]:
    messages = sanitize_messages_for_context(_trim_trailing_fallback_messages(messages))
    _, keyed = _load_incremental_baseline()
    prev_entry = keyed.get(_conversation_key(match_id, match_name)) or {}
    current_sig = list(_tail_signature(messages))
    current_sig_tuple = tuple(current_sig)

    dormant_sig = tuple(prev_entry.get("dormant_signature", []) or [])
    dormant_since = str(prev_entry.get("dormant_since", "") or "")
    if not dormant_since or dormant_sig != current_sig_tuple:
        _update_incremental_baseline(
            match_id,
            match_name,
            messages,
            metadata={
                "dormant_signature": current_sig,
                "dormant_since": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
        return False, "沉睡计时开始，等待24h"

    should_activate, reason = should_attempt_reactivation(
        messages,
        dormant_since=dormant_since,
        last_reactivation_at=str(prev_entry.get("last_reactivation_at", "") or ""),
        reactivation_attempt_count=int(
            prev_entry.get(
                "reactivation_attempt_count",
                1 if str(prev_entry.get("last_reactivation_at", "") or "") else 0,
            ) or 0
        ),
    )
    if not should_activate:
        return False, reason

    last_reactivation_sig = prev_entry.get("last_reactivation_signature", [])
    last_reactivation_at = str(prev_entry.get("last_reactivation_at", "") or "")
    if current_sig == last_reactivation_sig and last_reactivation_at:
        return False, "相同沉睡对话已激活过，等待下次变化"

    return True, "允许沉睡激活"


def _mark_dormant_reactivation_sent(
    match_id: str,
    match_name: str,
    messages: list,
    reason: str = "reactivated",
    *,
    snapshot_key: str | None = None,
) -> None:
    messages = sanitize_messages_for_context(_trim_trailing_fallback_messages(messages))
    current_sig = list(_tail_signature(messages))
    _, keyed = _load_incremental_baseline()
    prev_entry = keyed.get(_conversation_key(match_id, match_name)) or {}
    current_attempt_count = int(
        prev_entry.get(
            "reactivation_attempt_count",
            1 if str(prev_entry.get("last_reactivation_at", "") or "") else 0,
        ) or 0
    )
    _update_incremental_baseline(
        match_id,
        match_name,
        messages,
        handled_inbound_signature=_inbound_signature(messages),
        handled_inbound_reason=reason,
        metadata={
            "dormant_signature": current_sig,
            "dormant_since": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_reactivation_signature": current_sig,
            "last_reactivation_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_reactivation_reason": reason,
            "reactivation_attempt_count": current_attempt_count + 1,
            "last_snapshot_key": (
                _persisted_snapshot_key(snapshot_key, context="bumble reactivation")
                if snapshot_key is not None
                else prev_entry.get("last_snapshot_key", "")
            ),
        },
    )


def _generate_or_fetch_reply(match_id: str, msgs: list, bio: str, match_name: str = "", intent: str = "reply") -> tuple[str | None, bool]:
    cached = get_reply("bumble", match_id)
    if cached:
        return cached, True
    reply = generate_reply(msgs, bio, platform="bumble", strategy=load_strategy(), intent=intent)
    if reply:
        return reply, False
    if not msgs and intent == "reply":
        return generate_reply([], bio, platform="bumble", strategy=load_strategy(), intent=intent), False
    return None, False


def _run_entries(bot: BumbleBot, page, entries: list[dict]) -> int:
    # 用户明确要求 Bumble 保持平台原生搜寻逻辑：
    # 只遍历 "Your Move / 轮到您了" 候选，不复用 Tinder 的 13 + 5 滚动窗口。
    # 这属于平台接入差异，不属于回复框架分叉。
    entries = list(entries or [])
    log.info(f"[Bumble] 共收集 {len(entries)} 个 Your Move 候选，按原生顺序遍历")

    seen = set()
    count = 0

    for index, e in enumerate(entries):
        seen_key = e.get("uid") or e.get("name") or f"idx:{index}"
        if seen_key in seen:
            continue
        seen.add(seen_key)

        match_id = str(e.get("uid") or e.get("name") or "")
        match_name = e.get("name", "unknown")

        if not click_contact(page, e, platform="bumble"):
            back_to_list(page, "bumble")
            continue
        if not wait_for_chat_ready(page, platform="bumble", timeout=15):
            back_to_list(page, "bumble")
            continue
        time.sleep(2)

        res = bot.extract_messages_from_chat_panel()
        msgs = sanitize_messages_for_context(
            _trim_trailing_fallback_messages(res.get("messages", []))
        )
        bio = res.get("match_bio", "")

        if not msgs:
            log.info(f"[Bumble] #{index + 1} 新配对无消息，生成开场白")
            reply, used_cache = _generate_or_fetch_reply(match_id, [], bio, match_name=match_name)
            if not reply:
                record_runtime_feedback(
                    "bumble",
                    match_id,
                    match_name,
                    "opener_no_safe_reply",
                    intent="opener",
                    reason="no_safe_opener",
                    messages=[],
                )
                log.warning(f"[Bumble] #{index + 1} 无法生成自然开场白，跳过发送")
                back_to_list(page, "bumble")
                continue
            ok = bot.send_message(reply, messages=[])
            if ok:
                if used_cache:
                    confirm_sent("bumble", match_id)
                count += 1
                opener_messages = [{"sender": "me", "text": reply, "is_mine": True}]
                snapshot_key = _write_corpus(match_name, match_id, [], reply, intent="opener")
                persisted_snapshot_key = _persisted_snapshot_key(snapshot_key, context="bumble opener")
                _update_incremental_baseline(
                    match_id,
                    match_name,
                    opener_messages,
                    handled_inbound_signature=tuple(),
                    handled_inbound_reason="opened",
                    metadata={"last_snapshot_key": persisted_snapshot_key},
                )
                record_runtime_feedback(
                    "bumble",
                    match_id,
                    match_name,
                    "opener_sent",
                    intent="opener",
                    reply=reply,
                    messages=[],
                    metadata={"used_cache": used_cache},
                )
                log.info(f"[Bumble] #{index + 1} ✓ 开场白发送成功")
            else:
                record_runtime_feedback(
                    "bumble",
                    match_id,
                    match_name,
                    "opener_send_failed",
                    intent="opener",
                    reply=reply,
                    messages=[],
                    metadata=get_last_send_diagnostics(page),
                )
                log.warning(f"[Bumble] #{index + 1} ❌ 开场白发送失败")
            back_to_list(page, "bumble")
            time.sleep(random.uniform(4, 7))
            continue

        last = msgs[-1]
        if last.get("sender") == "me" or last.get("is_mine"):
            back_to_list(page, "bumble")
            continue

        if not _is_new_messages(match_id, match_name, msgs):
            log.info(f"[Bumble] #{index + 1} 无新消息，跳过 {match_name}")
            back_to_list(page, "bumble")
            continue

        should_send, reason = should_reply_to_messages(msgs, platform="bumble")
        if not should_send:
            _update_incremental_baseline(
                match_id,
                match_name,
                msgs,
                handled_inbound_signature=_inbound_signature(msgs),
                handled_inbound_reason=f"skipped:{reason}",
            )
            record_runtime_feedback(
                "bumble",
                match_id,
                match_name,
                "reply_business_skipped",
                intent="reply",
                reason=reason,
                messages=msgs,
            )
            log.info(f"[Bumble] #{index + 1} 命中业务拦截，跳过回复 {match_name}: {reason}")
            back_to_list(page, "bumble")
            continue

        reply, used_cache = _generate_or_fetch_reply(match_id, msgs, bio, match_name=match_name)
        if not reply:
            _update_incremental_baseline(
                match_id,
                match_name,
                msgs,
                handled_inbound_signature=_inbound_signature(msgs),
                handled_inbound_reason="skipped:no_safe_reply",
            )
            record_runtime_feedback(
                "bumble",
                match_id,
                match_name,
                "reply_no_safe_reply",
                intent="reply",
                reason="no_safe_reply",
                messages=msgs,
            )
            log.info(f"[Bumble] #{index + 1} 无安全回复，跳过 {match_name}")
            back_to_list(page, "bumble")
            continue

        ok = bot.send_message(reply, messages=msgs)
        if ok:
            if used_cache:
                confirm_sent("bumble", match_id)
            count += 1
            snapshot_key = _record_replied_conversation(match_name, match_id, msgs, reply)
            persisted_snapshot_key = _persisted_snapshot_key(snapshot_key, context="bumble reply")
            _update_incremental_baseline(
                match_id,
                match_name,
                msgs,
                handled_inbound_signature=_inbound_signature(msgs),
                handled_inbound_reason="replied",
                metadata={"last_snapshot_key": persisted_snapshot_key},
            )
            record_runtime_feedback(
                "bumble",
                match_id,
                match_name,
                "reply_sent",
                intent="reply",
                reply=reply,
                messages=msgs,
                metadata={"used_cache": used_cache},
            )
            log.info(f"[Bumble] #{index + 1} ✓ 回复成功: {match_name}")
        else:
            record_runtime_feedback(
                "bumble",
                match_id,
                match_name,
                "reply_send_failed",
                intent="reply",
                reply=reply,
                messages=msgs,
                metadata=get_last_send_diagnostics(page),
            )
            log.warning(f"[Bumble] #{index + 1} ❌ 回复失败: {match_name}")

        back_to_list(page, "bumble")
        time.sleep(random.uniform(4, 7))

    return count


def _collect_all_dormant_entries(page, max_candidates: int = 200, max_scrolls: int = 30) -> list[dict]:
    collected: dict[str, dict] = {}
    stagnant_rounds = 0
    last_count = 0

    for _ in range(max_scrolls):
        raw_entries = page.evaluate(BUMBLE_DORMANT_ENTRIES_JS) or []
        for entry in raw_entries:
            key = str(entry.get("uid") or entry.get("name") or "")
            if not key:
                continue
            if key not in collected:
                collected[key] = entry

        current_count = len(collected)
        if current_count >= max_candidates:
            break

        moved = page.evaluate(BUMBLE_SCROLL_CONTACTS_JS)
        time.sleep(0.5)

        if current_count == last_count and not moved:
            stagnant_rounds += 1
        else:
            stagnant_rounds = 0
        last_count = current_count
        if stagnant_rounds >= 3:
            break

    return list(collected.values())[:max_candidates]


def _run_dormant_entries(bot: BumbleBot, page, entries: list[dict]) -> int:
    entries = list(entries or [])
    if not entries:
        return 0

    entries, _ = _rotate_dormant_entries(entries)

    log.info(
        "[Bumble][激活] 共收集 %s 个非 Your Move 候选，尝试沉睡联系人激活（每轮最多 %s 个 / %ss）",
        len(entries),
        DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND,
        DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND,
    )

    seen = set()
    round_started_at = time.time()
    attempts = 0
    last_attempted_key = ""
    for index, e in enumerate(entries):
        if attempts >= DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND:
            log.info(
                "[Bumble][激活] 已达到本轮沉睡激活尝试上限 %s，结束",
                DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND,
            )
            break
        if time.time() - round_started_at >= DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND:
            log.info(
                "[Bumble][激活] 已达到本轮沉睡激活时间预算 %ss，结束",
                DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND,
            )
            break

        seen_key = e.get("uid") or e.get("name") or f"idx:{index}"
        if seen_key in seen:
            continue
        seen.add(seen_key)

        match_id = str(e.get("uid") or e.get("name") or "")
        match_name = e.get("name", "unknown")
        attempts += 1
        last_attempted_key = _dormant_entry_key(e)

        if not click_contact(page, e, platform="bumble"):
            back_to_list(page, "bumble")
            continue
        if not wait_for_chat_ready(page, platform="bumble", timeout=15):
            back_to_list(page, "bumble")
            continue
        time.sleep(2)

        res = bot.extract_messages_from_chat_panel()
        msgs = sanitize_messages_for_context(
            _trim_trailing_fallback_messages(res.get("messages", []))
        )
        bio = res.get("match_bio", "")

        should_activate, reason = _get_dormant_reactivation_candidate(match_id, match_name, msgs)
        if not should_activate:
            log.info(f"[Bumble][激活] #{index + 1} 跳过 {match_name}: {reason}")
            back_to_list(page, "bumble")
            continue

        reply, used_cache = _generate_or_fetch_reply(match_id, msgs, bio, match_name=match_name, intent="reactivation")
        if not reply:
            _mark_dormant_reactivation_sent(
                match_id,
                match_name,
                msgs,
                reason="skipped:no_safe_reactivation",
            )
            record_runtime_feedback(
                "bumble",
                match_id,
                match_name,
                "reactivation_no_safe_reply",
                intent="reactivation",
                reason="no_safe_reactivation",
                messages=msgs,
            )
            log.info(f"[Bumble][激活] #{index + 1} 无安全激活回复，跳过 {match_name}")
            back_to_list(page, "bumble")
            continue

        ok = bot.send_message(reply, messages=msgs)
        if ok:
            if used_cache:
                confirm_sent("bumble", match_id)
            snapshot_key = _write_corpus(match_name, match_id, msgs, reply, intent="reactivation")
            _mark_dormant_reactivation_sent(match_id, match_name, msgs, snapshot_key=snapshot_key)
            record_runtime_feedback(
                "bumble",
                match_id,
                match_name,
                "reactivation_sent",
                intent="reactivation",
                reply=reply,
                messages=msgs,
                metadata={"used_cache": used_cache},
            )
            log.info(f"[Bumble][激活] #{index + 1} ✓ 激活成功: {match_name}")
            back_to_list(page, "bumble")
            time.sleep(random.uniform(4, 7))
            _advance_dormant_scan_cursor(last_attempted_key)
            return 1

        record_runtime_feedback(
            "bumble",
            match_id,
            match_name,
            "reactivation_send_failed",
            intent="reactivation",
            reply=reply,
            messages=msgs,
            metadata=get_last_send_diagnostics(page),
        )
        log.warning(f"[Bumble][激活] #{index + 1} ❌ 激活发送失败: {match_name}")
        back_to_list(page, "bumble")
        time.sleep(random.uniform(4, 7))

    _advance_dormant_scan_cursor(last_attempted_key)
    return 0


def main():
    result_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bumble_inspect_result.json"

    bot = BumbleBot(profile_path=BUMBLE_PROFILE)
    bot.launch()
    page = bot.page

    page.goto("https://eu1.bumble.com/app/connections",
               timeout=30000, wait_until="domcontentloaded")
    time.sleep(15)

    # ── 视觉熔断检测 ──────────────────────────────
    if bot._check_backend_error():
        bot.close()
        raise RuntimeError("BUMBLE_BACKEND_ERROR")

    entries = page.evaluate(BUMBLE_ENTRIES_JS)
    count = _run_entries(bot, page, entries) if entries else 0
    if count == 0:
        dormant_entries = _collect_all_dormant_entries(page, max_candidates=200, max_scrolls=30)
        count += _run_dormant_entries(bot, page, dormant_entries)
    bot.close()
    print(f"RESULT: {count}")


def run_inspect() -> int:
    """
    主进程直调入口。返回本轮回复数（0 表示无新消息，-1 表示后端异常）。
    由 unified_orchestrator.inspect_bumble() 直接调用。
    """
    bot = None
    try:
        bot = BumbleBot(profile_path=BUMBLE_PROFILE)
        bot.launch()
        page = bot.page

        page.goto("https://eu1.bumble.com/app/connections",
                  timeout=30000, wait_until="domcontentloaded")
        time.sleep(15)

        if bot._check_backend_error():
            log.warning("[Bumble] 后端异常")
            return -1

        entries = page.evaluate(BUMBLE_ENTRIES_JS)
        count = _run_entries(bot, page, entries) if entries else 0
        if count == 0:
            dormant_entries = _collect_all_dormant_entries(page, max_candidates=200, max_scrolls=30)
            count += _run_dormant_entries(bot, page, dormant_entries)
        return count

    except Exception as e:
        logging.error(f"[Bumble] 巡检异常: {e}")
        return -1
    finally:
        if bot is not None:
            try:
                bot.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
