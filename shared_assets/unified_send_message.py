#!/usr/bin/env python3
"""
unified_send_message.py
========================
跨平台统一消息发送引擎（防截断版）

核心职责
--------
1. send_message_unified() — 统一发送入口（Tinder / Bumble / 任意 SPA 平台）
2. 自动处理多行拆分、输入框定位、发送验证
3. 优先 fill() 整段填充，失败则降级为 press_sequentially()
4. 支持 / 分隔符拆分（符合策略要求）

平台适配
--------
通过 platform 参数区分：
- "tinder": div[contenteditable="true"][role="textbox"]
- "bumble": textarea[placeholder*='message']
- 其他平台可扩展

使用示例
--------
from unified_send_message import send_message_unified

success = send_message_unified(
    page=page,
    message="你好 / 在吗",
    platform="tinder"
)
"""
from __future__ import annotations

import time
import random
import os
from dataclasses import dataclass
from typing import Callable, Optional

from unified_reply_engine import sanitize_reply_for_send

SEND_CONFIRM_TIMEOUT_SECONDS = 6.0


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


DEFAULT_SEND_MAX_RETRIES = _env_int("APP_SEND__MAX_RETRIES", 3, min_value=1, max_value=10)
SEND_FAILURE_SETTLE_MS = _env_int("APP_SEND__FAILURE_SETTLE_MS", 1600, min_value=300, max_value=5000)


@dataclass(frozen=True)
class PlatformDOMAdapter:
    name: str
    input_selectors: tuple[str, ...]
    outgoing_state_script: str | None
    is_chat_page_fn: Callable[[str], bool]
    send_key: str


TINDER_OUTGOING_STATE_SCRIPT = r"""
() => {
    const logs = Array.from(document.querySelectorAll('[role="log"]'));
    const chatLog = logs.find(l => l.id && l.id.includes('SC.chat')) || logs[0];
    if (!chatLog) return { count: 0, last_text: '' };

    const items = [];
    const spans = Array.from(chatLog.querySelectorAll('span.text'));
    spans.forEach(span => {
        const raw = (span.innerText || '').trim();
        const text = (!raw || raw.length > 500) ? '' : raw;
        if (!text || /^\d{1,2}:\d{2}/.test(text) || /发了张/.test(text)) return;

        let isMine = false;
        let current = span.parentElement;
        for (let i = 0; i < 7; i++) {
            if (!current) break;
            const style = window.getComputedStyle(current);
            if (style.justifyContent === 'flex-end' || style.alignItems === 'flex-end' || style.alignSelf === 'flex-end') {
                isMine = true;
                break;
            }
            current = current.parentElement;
        }
        if (!isMine) {
            const color = window.getComputedStyle(span).color || '';
            if (color.includes('255, 255, 255')) isMine = true;
        }
        if (isMine) items.push(text);
    });
    return { count: items.length, last_text: items.length ? items[items.length - 1] : '' };
}
"""


BUMBLE_OUTGOING_STATE_SCRIPT = r"""
() => {
    const pageChat = document.querySelector('.page--chat');
    if (!pageChat) return { count: 0, last_text: '' };
    const layout = pageChat.children[0];
    if (!layout) return { count: 0, last_text: '' };

    let chatPanel = null;
    for (const child of layout.children) {
        const cls = child.className || '';
        if (cls.includes('sidebar') || cls.includes('contact-tabs') || cls.includes('request-panel')) continue;
        if (child.querySelector('[class*="bubble"]')) {
            chatPanel = child;
            break;
        }
    }
    if (!chatPanel && layout.querySelector('[class*="bubble"]')) chatPanel = layout;
    if (!chatPanel) return { count: 0, last_text: '' };

    const items = [];
    const bubbles = Array.from(chatPanel.querySelectorAll('[class*="bubble"]'));
    bubbles.forEach(b => {
        const text = (b.innerText || '').trim();
        if (!text || text.length > 500 || /^\d{1,2}:\d{2}$/.test(text)) return;
        const rect = b.getBoundingClientRect();
        const isMine = rect.left > window.innerWidth / 2;
        if (isMine) items.push(text);
    });
    return { count: items.length, last_text: items.length ? items[items.length - 1] : '' };
}
"""


PLATFORM_DOM_ADAPTERS: dict[str, PlatformDOMAdapter] = {
    "tinder": PlatformDOMAdapter(
        name="tinder",
        input_selectors=(
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"]',
            'textarea',
            '[role="textbox"]',
        ),
        outgoing_state_script=TINDER_OUTGOING_STATE_SCRIPT,
        is_chat_page_fn=lambda url: '/messages/' in url,
        send_key="Enter",
    ),
    "bumble": PlatformDOMAdapter(
        name="bumble",
        input_selectors=(
            "textarea[placeholder*='聊天']",
            "textarea[placeholder*='message']",
            "textarea",
        ),
        outgoing_state_script=BUMBLE_OUTGOING_STATE_SCRIPT,
        is_chat_page_fn=lambda url: '/app/connections' in url or 'page--chat' in url,
        send_key="Enter",
    ),
    "default": PlatformDOMAdapter(
        name="default",
        input_selectors=('textarea', 'div[contenteditable="true"]'),
        outgoing_state_script=None,
        is_chat_page_fn=lambda url: True,
        send_key="Enter",
    ),
}


def _get_platform_adapter(platform: str) -> PlatformDOMAdapter:
    return PLATFORM_DOM_ADAPTERS.get(str(platform or "").strip(), PLATFORM_DOM_ADAPTERS["default"])


def _set_send_diagnostics(page, **payload) -> None:
    try:
        setattr(page, "_last_send_diagnostics", payload)
    except Exception:
        pass


def get_last_send_diagnostics(page) -> dict:
    try:
        value = getattr(page, "_last_send_diagnostics", {})
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def send_message_unified(
    page,
    message: str,
    platform: str = "tinder",
    max_retries: int | None = None,
    message_context: Optional[list[dict]] = None,
) -> bool:
    """
    统一消息发送函数（防截断版）
    
    参数
    ----
    page : Playwright Page 对象
    message : 要发送的消息文本
    platform : 平台标识 ("tinder" | "bumble")
    max_retries : 每条消息的最大重试次数
    
    返回
    ----
    bool : 是否全部发送成功
    """
    max_retries = int(max_retries or DEFAULT_SEND_MAX_RETRIES)
    message = sanitize_reply_for_send(message, max_len=50, messages=message_context)
    if not message or not message.strip():
        print("[Send] ⚠️ 文本为空，取消发送")
        _set_send_diagnostics(page, ok=False, stage="sanitize", reason="empty_after_sanitize", message=message)
        return False

    # ── Step 1: 拆分清洗 ──
    normalized = message.replace('\r\n', '\n').replace('\r', '\n')
    raw_lines = [line.strip() for line in normalized.split('\n') if line.strip()]
    lines = []
    for chunk in raw_lines:
        # 支持 / 分隔符拆分
        parts = [p.strip() for p in chunk.split('/') if p.strip()]
        lines.extend(parts if parts else [chunk])
    
    if not lines:
        print("[Send] ⚠️ 拆分后无有效文本")
        _set_send_diagnostics(page, ok=False, stage="split", reason="no_valid_lines", message=message)
        return False

    print(f"[Send] 拆分为 {len(lines)} 条发送: {[l[:20] for l in lines]}")

    # ── Step 2: 定位输入框 ──
    input_box = _locate_input_box(page, platform)
    if not input_box:
        print(f"[Send] ❌ 未找到可见输入框，当前页面: {page.url}")
        _set_send_diagnostics(page, ok=False, stage="locate_input", reason="input_not_found", page_url=page.url)
        return False

    # ── Step 3: 循环发送每一行 ──
    chat_url = page.url if '/messages/' in page.url or '/app/connections' in page.url else None
    
    for i, line in enumerate(lines):
        for attempt in range(max_retries):
            # 验证页面仍在聊天页
            if chat_url and not _is_chat_page(page.url, platform):
                print(f"[Send] ⚠️ 页面偏离，重新进入: {page.url}")
                page.goto(chat_url, timeout=15000)
                time.sleep(3)
                chat_url = page.url
            
            try:
                # 重新定位输入框（可能因页面刷新而失效）
                input_box = _locate_input_box(page, platform)
                if not input_box or not input_box.is_visible(timeout=3000):
                    print(f"[Send] ❌ 输入框消失，当前页面: {page.url}")
                    _set_send_diagnostics(page, ok=False, stage="locate_input", reason="input_disappeared", page_url=page.url)
                    return False

                page.wait_for_timeout(300)
                input_box.focus()
                page.wait_for_timeout(200)
                input_box.fill("")
                page.wait_for_timeout(200)
                before_state = _capture_outgoing_state(page, platform)

                # 先尝试整段填充
                input_box.fill(line)
                page.wait_for_timeout(300)
                current_text = _read_box_text(input_box).strip()
                
                if current_text != line:
                    print(f"[Send] ⚠️ fill 后文本不一致，改用逐字输入: want={line!r} got={current_text!r}")
                    input_box.fill("")
                    page.wait_for_timeout(200)
                    delay = random.randint(60, 110)
                    input_box.press_sequentially(line, delay=delay)
                    page.wait_for_timeout(500)
                    current_text = _read_box_text(input_box).strip()

                if current_text != line:
                    print(f"[Send] ⚠️ 第 {i+1} 条输入仍被截断，重试第 {attempt+2} 次: want={line!r} got={current_text!r}")
                    page.wait_for_timeout(800)
                    _set_send_diagnostics(
                        page,
                        ok=False,
                        stage="input",
                        reason="text_truncated",
                        attempt=attempt + 1,
                        line=line,
                        actual=current_text,
                    )
                    continue

                # 发送
                _send_message(page, platform)
                page.wait_for_timeout(600)

                # 验证发送成功
                verified, verify_reason = _verify_sent(page, input_box, line, platform, before_state)
                if verified:
                    print(f"[Send] 第 {i+1}/{len(lines)} 条发送成功: {line[:30]}...")
                    _set_send_diagnostics(
                        page,
                        ok=True,
                        stage="verify",
                        reason="sent_confirmed",
                        line=line,
                        line_index=i + 1,
                        line_count=len(lines),
                    )
                    break

                page.wait_for_timeout(800)
                late_verified, late_reason = _verify_sent_late(page, line, platform, before_state)
                if late_verified:
                    print(f"[Send] ℹ️ 第 {i+1}/{len(lines)} 条发送晚确认成功: {line[:30]}...")
                    _set_send_diagnostics(
                        page,
                        ok=True,
                        stage="verify_late",
                        reason=late_reason,
                        line=line,
                        line_index=i + 1,
                        line_count=len(lines),
                    )
                    break

                print(f"[Send] ⚠️ 第 {i+1} 条发送未确认({verify_reason})，重试第 {attempt+2} 次")
                _set_send_diagnostics(
                    page,
                    ok=False,
                    stage="verify",
                    reason=verify_reason,
                    attempt=attempt + 1,
                    line=line,
                    line_index=i + 1,
                    line_count=len(lines),
                )

            except Exception as e:
                print(f"[Send] ⚠️ 第 {i+1} 条异常: {e}, 重试第 {attempt+2} 次")
                page.wait_for_timeout(800)
                _set_send_diagnostics(
                    page,
                    ok=False,
                    stage="exception",
                    reason=str(e),
                    attempt=attempt + 1,
                    line=line,
                    line_index=i + 1,
                    line_count=len(lines),
                )
        else:
            print(f"[Send] ❌ 第 {i+1} 条发送失败（{max_retries}次重试）")
            _set_send_diagnostics(
                page,
                ok=False,
                stage="line_failed",
                reason="max_retries_exceeded",
                line=line,
                line_index=i + 1,
                line_count=len(lines),
            )
            return False

    # ── Step 4: 最终校验 ──
    try:
        final_val = _read_box_text(input_box).strip()
        if final_val:
            print(f"[Send] ⚠️ 输入框尚有残余: {final_val[:50]}")
            input_box.fill("")
            page.wait_for_timeout(500)
    except Exception:
        pass

    print(f"[Send] ✓ 全部 {len(lines)} 条发送完成")
    _set_send_diagnostics(page, ok=True, stage="complete", reason="all_lines_sent", line_count=len(lines))
    return True


def _locate_input_box(page, platform: str):
    """定位输入框"""
    adapter = _get_platform_adapter(platform)

    for sel in adapter.input_selectors:
        try:
            box = page.locator(sel).first
            if box.is_visible(timeout=3000):
                print(f"[Send] 输入框命中: {sel}")
                return box
        except Exception:
            continue
    return None


def _read_box_text(box) -> str:
    """读取输入框文本"""
    try:
        txt = box.input_value()
        if txt is not None:
            return txt
    except Exception:
        pass
    try:
        return box.inner_text()
    except Exception:
        return ""


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _capture_outgoing_state(page, platform: str) -> dict:
    """抓取当前聊天里我方消息的简化状态，用于发送后确认。"""
    adapter = _get_platform_adapter(platform)
    if not adapter.outgoing_state_script:
        return {"count": 0, "last_text": ""}
    try:
        result = page.evaluate(adapter.outgoing_state_script)
    except Exception:
        return {"count": 0, "last_text": ""}

    return {
        "count": int(result.get("count") or 0),
        "last_text": _normalize_text(result.get("last_text", "")),
    }


def _detect_send_failure_marker(page, platform: str) -> tuple[bool, str]:
    """Look for late optimistic-send failure markers before we mark DB state as replied."""
    del platform
    try:
        result = page.evaluate(r"""
            () => {
                const failureRe = /(failed|not sent|couldn.?t send|try again|retry|undelivered|发送失败|未发送|重试|重新发送|失败)/i;
                const nodes = Array.from(document.querySelectorAll('[class], [aria-label], [title], button, [role="button"]'));
                for (const node of nodes) {
                    const text = [
                        node.innerText || '',
                        node.getAttribute && node.getAttribute('aria-label') || '',
                        node.getAttribute && node.getAttribute('title') || '',
                        node.getAttribute && node.getAttribute('class') || '',
                        node.getAttribute && node.getAttribute('data-testid') || '',
                    ].join(' ').trim();
                    if (text && failureRe.test(text)) {
                        return { failed: true, marker: text.slice(0, 160) };
                    }
                }
                return { failed: false, marker: '' };
            }
        """)
        return bool(result.get("failed")), str(result.get("marker") or "")
    except Exception:
        return False, ""


def _wait_for_send_failure_marker(page, platform: str) -> tuple[bool, str]:
    """Give the SPA a short window to flip an optimistic bubble into failed state."""
    deadline = time.time() + (SEND_FAILURE_SETTLE_MS / 1000.0)
    while time.time() < deadline:
        failed, marker = _detect_send_failure_marker(page, platform)
        if failed:
            return True, marker
        page.wait_for_timeout(250)
    return False, ""


def _is_chat_page(url: str, platform: str) -> bool:
    """判断是否在聊天页"""
    adapter = _get_platform_adapter(platform)
    return adapter.is_chat_page_fn(url)


def _send_message(page, platform: str):
    """发送消息（按平台区分）"""
    adapter = _get_platform_adapter(platform)
    page.keyboard.press(adapter.send_key)


def _verify_sent(page, input_box, line: str, platform: str, before_state: Optional[dict] = None) -> tuple[bool, str]:
    """验证消息是否发送成功：必须看到新的我方气泡出现，不再只凭输入框清空判成功。"""
    expected = _normalize_text(line)
    prior = before_state or {"count": 0, "last_text": ""}
    prior_count = int(prior.get("count") or 0)
    prior_last = _normalize_text(prior.get("last_text", ""))
    deadline = time.time() + SEND_CONFIRM_TIMEOUT_SECONDS
    input_cleared_once = False

    while time.time() < deadline:
        try:
            val_after = _read_box_text(input_box).strip()
            if not val_after:
                input_cleared_once = True
        except Exception:
            pass

        current = _capture_outgoing_state(page, platform)
        current_count = int(current.get("count") or 0)
        current_last = _normalize_text(current.get("last_text", ""))
        count_increased = current_count > prior_count
        last_text_matches = current_last == expected
        new_tail_confirmed = last_text_matches and (count_increased or prior_last != expected)

        if new_tail_confirmed:
            if not input_cleared_once:
                print("[Send] ℹ️ 输入框未及时清空，但已确认新气泡落地")
            failed, marker = _wait_for_send_failure_marker(page, platform)
            if failed:
                return False, f"bubble_failed_after_optimistic_render:{marker[:80]}"
            return True, "bubble_confirmed"

        page.wait_for_timeout(300)

    if input_cleared_once:
        return False, "input_cleared_but_no_new_bubble"
    return False, "no_new_bubble"


def _verify_sent_late(page, line: str, platform: str, before_state: Optional[dict] = None) -> tuple[bool, str]:
    """发送确认超时后的幂等补确认，避免慢刷新导致重复发送。"""
    expected = _normalize_text(line)
    prior = before_state or {"count": 0, "last_text": ""}
    prior_count = int(prior.get("count") or 0)
    prior_last = _normalize_text(prior.get("last_text", ""))
    current = _capture_outgoing_state(page, platform)
    current_count = int(current.get("count") or 0)
    current_last = _normalize_text(current.get("last_text", ""))
    count_increased = current_count > prior_count
    last_text_matches = current_last == expected
    new_tail_confirmed = last_text_matches and (count_increased or prior_last != expected)
    if new_tail_confirmed:
        failed, marker = _wait_for_send_failure_marker(page, platform)
        if failed:
            return False, f"bubble_failed_after_late_confirm:{marker[:80]}"
        return True, "sent_confirmed_after_verify_timeout"
    return False, "late_bubble_missing"
