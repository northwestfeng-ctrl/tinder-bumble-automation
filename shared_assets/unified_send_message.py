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
import re
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
SEND_FAILURE_SETTLE_MS = _env_int("APP_SEND__FAILURE_SETTLE_MS", 9000, min_value=1000, max_value=30000)
SEND_NETWORK_ACK_MS = _env_int("APP_SEND__NETWORK_ACK_MS", 10000, min_value=1000, max_value=30000)


def _env_patterns(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _env_regexes(name: str, default: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    raw = os.getenv(name, "")
    patterns = tuple(part.strip() for part in raw.split(",") if part.strip()) if raw.strip() else default
    compiled: list[re.Pattern] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return tuple(compiled)


SEND_ENDPOINT_REGEXES = {
    "tinder": _env_regexes(
        "APP_SEND__TINDER_ENDPOINT_REGEXES",
        (
            r"/v2/matches/[^/?#]+/messages(?:[/?#]|$)",
            r"/v3/matches/[^/?#]+/messages(?:[/?#]|$)",
        ),
    ),
    "bumble": _env_regexes(
        "APP_SEND__BUMBLE_ENDPOINT_REGEXES",
        (
            r"/m/api/message(?:[/?#]|$)",
            r"/mwebapi\.phtml.*(?:SERVER_(?:POST|SEND)_CHAT_MESSAGE|message)",
        ),
    ),
}


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
            network_probe = None
            verify_reason = "not_started"
            # 验证页面仍在聊天页
            if chat_url and not _is_chat_page(page.url, platform):
                print(f"[Send] ⚠️ 页面偏离，重新进入: {page.url}")
                page.goto(chat_url, timeout=15000)
                time.sleep(3)
                chat_url = page.url
	            
            try:
                _clear_interaction_blockers(page, platform)

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

                try:
                    # 发送，并监听可能的底层发信 API 失败响应。
                    _clear_interaction_blockers(page, platform)
                    network_probe = _start_send_network_probe(page, platform, sent_text=line)
                    _send_message(page, platform)
                    page.wait_for_timeout(600)

                    # 验证发送成功
                    verified, verify_reason = _verify_sent(
                        page,
                        input_box,
                        line,
                        platform,
                        before_state,
                        network_probe,
                    )
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
                    late_verified, late_reason = _verify_sent_late(
                        page,
                        line,
                        platform,
                        before_state,
                        network_probe,
                    )
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
                finally:
                    _stop_send_network_probe(page, network_probe)

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
                _stop_send_network_probe(page, network_probe)
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


def _start_send_network_probe(page, platform: str, sent_text: str = "") -> dict:
    """Capture likely send API responses; endpoint patterns can be tuned via env."""
    regexes = SEND_ENDPOINT_REGEXES.get(str(platform or "").lower(), ())
    probe = {
        "matches": [],
        "regexes": regexes,
        "handler": None,
        "removed": False,
        "sent_text": _normalize_text(sent_text),
    }

    def _handler(response):
        try:
            method = str(response.request.method or "").upper()
            if method not in {"POST", "PUT", "PATCH"}:
                return
            url = str(response.url or "").lower()
            if not any(regex.search(url) for regex in regexes):
                return
            business_failure = _response_business_failure(response, probe.get("sent_text", ""))
            probe["matches"].append({
                "url": response.url,
                "status": int(response.status),
                "method": method,
                "business_failure": business_failure,
            })
        except Exception:
            return

    probe["handler"] = _handler
    try:
        page.on("response", _handler)
    except Exception:
        probe["handler"] = None
    return probe


def _stop_send_network_probe(page, probe: Optional[dict]) -> None:
    if not probe or not probe.get("handler") or probe.get("removed"):
        return
    probe["removed"] = True
    try:
        page.remove_listener("response", probe["handler"])
    except Exception:
        pass


def _network_probe_failure(probe: Optional[dict]) -> tuple[bool, str]:
    for item in list((probe or {}).get("matches", [])):
        status = int(item.get("status") or 0)
        if status >= 400:
            return True, f"{item.get('method', '')} {status} {item.get('url', '')}"[:160]
        business_failure = str(item.get("business_failure") or "")
        if business_failure:
            return True, f"{item.get('method', '')} business_error {business_failure}"[:160]
    return False, ""


def _response_business_failure(response, sent_text: str = "") -> str:
    """Detect send failures that are returned as HTTP 200 JSON bodies."""
    try:
        payload = response.json()
    except Exception:
        try:
            payload = response.text()
        except Exception:
            return ""

    marker = _extract_business_failure_marker(payload, sent_text=sent_text)
    return marker[:160] if marker else ""


def _extract_business_failure_marker(payload, source_key: str = "", sent_text: str = "") -> str:
    if payload in (None, "", [], {}):
        return ""
    source = str(source_key or "").strip()
    sent = _normalize_text(sent_text)
    if isinstance(payload, dict):
        for key in ("success", "ok"):
            if key in payload and payload.get(key) is False:
                return f"{key}=False"
        for key in ("error", "errors", "error_code", "errorCode", "error_message", "errorMessage"):
            if key in payload and _truthy_error_value(payload.get(key)):
                return f"{key}={payload.get(key)}"
        status_value = payload.get("status") or payload.get("result")
        if isinstance(status_value, (int, float)) and int(status_value) >= 400:
            return f"status={status_value}"
        if isinstance(status_value, str) and re.search(r"(error|fail|denied|blocked|ban|limit)", status_value, re.I):
            return f"status={status_value}"
        for key, value in payload.items():
            marker = _extract_business_failure_marker(value, str(key), sent)
            if marker:
                return marker
        return ""
    if isinstance(payload, list):
        for item in payload[:5]:
            marker = _extract_business_failure_marker(item, source, sent)
            if marker:
                return marker
        return ""
    if sent and _normalize_text(str(payload)) == sent:
        return ""
    error_like_source = re.search(r"(error|status|result|reason|code|failure|fail)", source, re.I)
    if not error_like_source:
        return ""
    text = str(payload)
    if re.search(r"(shadow.?ban|rate.?limit|too many|blocked|forbidden|policy|not sent|send failed|发送失败|风控|封禁|限制)", text, re.I):
        return text[:160]
    return ""


def _clear_interaction_blockers(page, platform: str) -> None:
    """Best-effort cleanup for modal overlays and full-page loading masks before actions."""
    if str(platform or "").lower() != "tinder":
        return
    try:
        page.evaluate(r"""
            () => {
                const closeTextRe = /^(no thanks|maybe later|not now|skip|取消|稍后|以后再说|不用|关闭)$/i;
                const candidates = Array.from(document.querySelectorAll(
                    'button, [role="button"], [aria-label], [title], [data-testid], [class*="close"], [class*="dismiss"]'
                ));
                for (const node of candidates.slice(0, 80)) {
                    const text = [
                        node.innerText || '',
                        node.getAttribute && node.getAttribute('aria-label') || '',
                        node.getAttribute && node.getAttribute('title') || '',
                        node.getAttribute && node.getAttribute('data-testid') || '',
                    ].join(' ').replace(/\s+/g, ' ').trim();
                    if (!text) continue;
                    if (closeTextRe.test(text) || /close|dismiss|modal-close|no_thanks|maybe_later/i.test(text)) {
                        try { node.click(); return true; } catch (e) {}
                    }
                }
                return false;
            }
        """)
    except Exception:
        pass
    try:
        page.locator(
            '[aria-label*="Loading"], [aria-label*="loading"], [role="progressbar"], '
            '[class*="loading"], [class*="spinner"], [class*="loader"]'
        ).first.wait_for(state="hidden", timeout=2500)
    except Exception:
        pass


def _truthy_error_value(value) -> bool:
    if value in (None, False, 0, "", [], {}):
        return False
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "none", "null", "ok", "success"}:
        return False
    return True


def _wait_for_network_probe_failure(page, probe: Optional[dict]) -> tuple[bool, str]:
    deadline = time.time() + (SEND_NETWORK_ACK_MS / 1000.0)
    while time.time() < deadline:
        failed, marker = _network_probe_failure(probe)
        if failed:
            return True, marker
        page.wait_for_timeout(250)
    return False, ""


def _send_message(page, platform: str):
    """发送消息（按平台区分）"""
    adapter = _get_platform_adapter(platform)
    page.keyboard.press(adapter.send_key)


def _verify_sent(
    page,
    input_box,
    line: str,
    platform: str,
    before_state: Optional[dict] = None,
    network_probe: Optional[dict] = None,
) -> tuple[bool, str]:
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
            failed, marker = _wait_for_network_probe_failure(page, network_probe)
            if failed:
                return False, f"network_send_failed_after_optimistic_render:{marker[:80]}"
            failed, marker = _wait_for_send_failure_marker(page, platform)
            if failed:
                return False, f"bubble_failed_after_optimistic_render:{marker[:80]}"
            return True, "bubble_confirmed"

        page.wait_for_timeout(300)

    if input_cleared_once:
        return False, "input_cleared_but_no_new_bubble"
    return False, "no_new_bubble"


def _verify_sent_late(
    page,
    line: str,
    platform: str,
    before_state: Optional[dict] = None,
    network_probe: Optional[dict] = None,
) -> tuple[bool, str]:
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
        failed, marker = _wait_for_network_probe_failure(page, network_probe)
        if failed:
            return False, f"network_send_failed_after_late_confirm:{marker[:80]}"
        failed, marker = _wait_for_send_failure_marker(page, platform)
        if failed:
            return False, f"bubble_failed_after_late_confirm:{marker[:80]}"
        return True, "sent_confirmed_after_verify_timeout"
    return False, "late_bubble_missing"
