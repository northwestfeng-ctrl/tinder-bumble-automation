#!/usr/bin/env python3
"""
Tinder 自动化 - 主Bot v2
整合所有模块:指纹混淆 + 事件模拟 + 轨迹 + 代理 + 熔断 + LLM回复生成
"""
import sys
import os
import time
import json
import re
import random
import sqlite3
import logging
import importlib.util
import types
from datetime import datetime, timedelta
from pathlib import Path

def _load_project_config_module():
    module_name = "tinder_project_config"
    module_path = Path(__file__).parent.parent / "project_config.py"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 Tinder project_config: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_tinder_core_package() -> None:
    package_name = "tinder_core"
    package_dir = Path(__file__).parent
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(package_dir)]
        package.__file__ = str(package_dir / "__init__.py")
        sys.modules[package_name] = package


_project_config = _load_project_config_module()
SHARED_ASSETS_ROOT = _project_config.SHARED_ASSETS_ROOT
build_browser_launch_options = _project_config.build_browser_launch_options
build_tinder_config = _project_config.build_tinder_config

_ensure_tinder_core_package()
if str(SHARED_ASSETS_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ASSETS_ROOT))

from atomic_state import read_json_file, update_json_file, write_json_file
from queue_db import get_reply, confirm_sent

from tinder_core.cdp_events import HumanClicker, HumanTyper, HumanScroller, HumanHover
from tinder_core.human_behavior import HumanTrajectory, HumanDelay, ActionRhythm, SwipeSimulator
from tinder_core.network_isolation import ProxyRotator
from tinder_core.strategy_loader import load_strategy
from tinder_core.lifecycle_guard import LifecycleGuard, ActionCooldown
from tinder_core.corpus_feedback import ConversationStore
from conversation_store import MISSING_SNAPSHOT_KEY, outcome_from_partner_followup
from unified_send_message import send_message_unified, get_last_send_diagnostics
from unified_reply_engine import (
    generate_reply as ure_generate_reply,
    build_contextual_fallback_reply,
    classify_partner_followup_quality,
    is_fallback_reply,
    is_like_reaction_message,
    sanitize_reply_for_send,
    sanitize_messages_for_context,
    should_reply_to_messages,
    should_attempt_reactivation,
)
from runtime_feedback import record_runtime_feedback
from playwright_stealth.stealth import Stealth

# ============ 自定义异常 ============
class TinderBackendError(Exception):
    """Tinder 后端加载失败（如消息列表转圈、Oops 页面等）"""
    pass


# ============ 配置 ============

CONFIG = build_tinder_config(load_strategy())
logger = logging.getLogger("tinder_bot")
TINDER_BASELINE_FILE = Path(__file__).parent.parent / "history_baseline.json"
TINDER_RUNTIME_STATE_FILE = Path(__file__).parent.parent / "tinder_runtime_state.json"
DOM_RULES_FILE = Path(os.getenv("APP_DOM_RULES_FILE", str(SHARED_ASSETS_ROOT / "dom_rules.json")))
LOCAL_DOM_RULES_FILE = Path(
    os.getenv("APP_DOM_RULES_LOCAL_FILE", str(Path.home() / ".openclaw" / "private" / "dom_rules.local.json"))
)

DEFAULT_TINDER_PROFILE_DOM_RULES = {
    "selectors": [
        '[class*="profileCard"]',
        '[class*="profile-card"]',
        '[class*="matchProfile"]',
        '[data-testid*="profile"]',
        '[class*="infoCard"]',
        '[class*="userInfo"]',
    ],
    "noise_fragments": [
        "Boost",
        "工作模式",
        "安全工具包",
        "看谁点了赞",
        "近期活跃",
        "马上配对",
        "LIKES YOU",
        "Messages",
        "消息",
    ],
    "own_profile_fragments": [],
}


def _load_dom_rule_section(section: str) -> dict:
    defaults = DEFAULT_TINDER_PROFILE_DOM_RULES if section == "tinder_profile" else {}
    rules = {key: list(value) if isinstance(value, list) else value for key, value in defaults.items()}

    def merge_from(path: Path, *, allow_missing: bool = True) -> None:
        if not path.exists():
            if not allow_missing:
                logger.warning(f"DOM 规则文件不存在: {path}")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            section_data = data.get(section, {}) if isinstance(data, dict) else {}
            if not isinstance(section_data, dict):
                return
            for key, value in section_data.items():
                if isinstance(value, list):
                    rules[key] = value
                elif value is not None:
                    rules[key] = value
        except Exception as exc:
            logger.warning(f"DOM 规则读取失败 {path}: {exc}")

    merge_from(DOM_RULES_FILE)
    merge_from(LOCAL_DOM_RULES_FILE)

    privacy_words = [
        item.strip()
        for item in re.split(r"[,，|;；\n]+", os.getenv("APP_PRIVACY_MASK_WORDS", ""))
        if item.strip()
    ]
    if privacy_words:
        existing = list(rules.get("own_profile_fragments", []) or [])
        rules["own_profile_fragments"] = existing + [
            item for item in privacy_words if item not in existing
        ]
    return rules


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS = _env_int("TINDER_SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS", 24)
DORMANT_REACTIVATION_MIN_DORMANT_HOURS = _env_int(
    "TINDER_DORMANT_REACTIVATION_MIN_DORMANT_HOURS",
    _env_int("APP_REACTIVATION__MIN_DORMANT_HOURS", 24),
)
DORMANT_REACTIVATION_CONTACT_COOLDOWN_HOURS = _env_int(
    "TINDER_DORMANT_REACTIVATION_CONTACT_COOLDOWN_HOURS",
    _env_int("APP_REACTIVATION__MIN_REACTIVATION_GAP_HOURS", 72),
)
DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_CONTACT = _env_int("TINDER_DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_CONTACT", 2)
DORMANT_REACTIVATION_ROUND_COOLDOWN_HOURS = _env_int("TINDER_DORMANT_REACTIVATION_ROUND_COOLDOWN_HOURS", 3)
DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND = _env_int("TINDER_DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND", 3)
DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND = _env_int("TINDER_DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND", 15)
DORMANT_REACTIVATION_MESSAGE_LIST_MAX_SCROLLS = _env_int("TINDER_DORMANT_REACTIVATION_MESSAGE_LIST_MAX_SCROLLS", 30)
MESSAGE_SURFACE_BOOTSTRAP_MATCH_LIMIT = _env_int("TINDER_MESSAGE_SURFACE_BOOTSTRAP_MATCH_LIMIT", 8)
MESSAGE_ANCHOR_EVALUATE_ATTEMPTS = _env_int("TINDER_MESSAGE_ANCHOR_EVALUATE_ATTEMPTS", 3, max_value=5)
MESSAGE_ANCHOR_MIN_EXPECTED_COUNT = _env_int("TINDER_MESSAGE_ANCHOR_MIN_EXPECTED_COUNT", 10, max_value=80)
MESSAGE_ANCHOR_RETRY_WAIT_MS = _env_int("TINDER_MESSAGE_ANCHOR_RETRY_WAIT_MS", 2000, min_value=100, max_value=10000)


class TinderBot:
    """Tinder 自动化主类"""

    def __init__(self, config: dict = None):
        self.config = config or CONFIG
        self.browser = None
        self.page = None
        self.context = None
        self.playwright = None
        self.browser_manager = None
        self.guard = LifecycleGuard(self.config["account_id"])
        self.cooldown = ActionCooldown()
        self.rhythm = None
        self.user_data_dir = self.config.get("user_data_dir") or str(
            Path.home() / ".tinder-automation" / "browser-profile"
        )

        # 代理会话粘性
        self.current_proxy = None
        self.proxy_session_start = 0
        self.proxy_rotator = ProxyRotator([])

        # 初始化各模块
        self.clicker = None
        self.typer = None
        self.scroller = None
        self.hover = None
        self.trajectory = None
        self.swiper = None
        self.error_log = []

        # 语料飞轮存储
        self.corpus_store = ConversationStore()

    def _bind_page_helpers(self) -> None:
        self.clicker = HumanClicker(self.page)
        self.typer = HumanTyper(self.page)
        self.scroller = HumanScroller(self.page)
        self.hover = HumanHover(self.page)
        self.trajectory = HumanTrajectory(self.page)
        self.swiper = SwipeSimulator(self.page)
        self.rhythm = ActionRhythm(self.page)

    def _rebuild_browser_instance(self, reason: str = "") -> bool:
        try:
            if not self.browser_manager:
                return False
            self._log("warning", f"[Tinder] 重建浏览器实例: {reason or 'unknown'}")
            self.browser_manager.cleanup()
            instance = self.browser_manager.get_instance()
            self.playwright = instance.playwright
            self.context = instance.context
            self.page = instance.page
            self._bind_page_helpers()
            return True
        except Exception as e:
            self._log("warning", f"[Tinder] 重建浏览器实例失败: {e}")
            return False

    @staticmethod
    def _is_target_closed_error(exc: Exception) -> bool:
        exc_type = type(exc).__name__.lower()
        message = str(exc).lower()
        return (
            "targetclosed" in exc_type
            or "target page" in message and "closed" in message
            or "browser has been closed" in message
            or "context has been closed" in message
        )

    def _handle_webdriver_exception(self, exc: Exception, context: str) -> bool:
        """浏览器/页面对象失效时立刻重建，避免普通 except 把真实故障吞掉。"""
        if not self._is_target_closed_error(exc):
            return False
        self._log("warning", f"[Tinder] Playwright target 已关闭，触发重建: {context} | {exc}")
        rebuilt = self._rebuild_browser_instance(context)
        if rebuilt:
            raise TinderBackendError(f"浏览器 target 关闭，已重建: {context}") from exc
        raise TinderBackendError(f"浏览器 target 关闭且重建失败: {context}") from exc

    def _recover_message_surface(self) -> bool:
        """消息页自愈：刷新 -> 直达 messages -> tab deep link -> 最近会话深链 -> 重建浏览器。"""
        direct_message_url = "https://tinder.com/app/messages"
        tab_message_url = "https://tinder.com/app/matches?tab=messages"
        current_url = self.page.url or ""

        def _ready() -> bool:
            if self.is_message_list_loading():
                return False
            if self._wait_for_message_cards(timeout=6000):
                return True
            return self._conversation_anchor_count() > 0

        recover_steps = [
            ("reload-current", lambda: self.page.reload(wait_until="domcontentloaded", timeout=15000)),
            ("goto-direct-messages-route", lambda: self.page.goto(direct_message_url, timeout=15000)),
            ("goto-messages-tab", lambda: self.page.goto(tab_message_url, timeout=15000)),
            ("bootstrap-recent-message-chat", lambda: self._bootstrap_message_surface_from_recent_chats()),
            ("rebuild-browser", lambda: self._rebuild_browser_instance("message surface recovery")),
        ]

        for step_name, step in recover_steps:
            try:
                self._log("warning", f"[Tinder] 尝试恢复消息页: {step_name}")
                result = step()
                if step_name == "rebuild-browser" and result is False:
                    continue
                if step_name == "rebuild-browser":
                    self.page.goto(direct_message_url, timeout=15000)
                elif step_name == "bootstrap-recent-message-chat":
                    if result and _ready():
                        self._log("info", f"[Tinder] 消息页恢复成功: {step_name}")
                        return True
                    if result:
                        self.navigate_to_messages()
                elif "tinder.com/app/" not in (self.page.url or "") and current_url:
                    self.page.goto(current_url, timeout=15000)
                time.sleep(4)
                self.navigate_to_messages()
                time.sleep(2)
                if _ready():
                    self._log("info", f"[Tinder] 消息页恢复成功: {step_name}")
                    return True
            except Exception as e:
                self._handle_webdriver_exception(e, f"recover_message_surface {step_name}")
                self._log("warning", f"[Tinder] 恢复步骤失败 {step_name}: {e}")

        return False

    @staticmethod
    def _conversation_key(match_id: str = "", match_name: str = "", match_index: int | str = "") -> str:
        if match_id:
            return f"match_id:{match_id}"
        if match_name and match_index != "":
            return f"name_index:{match_name}:{match_index}"
        if match_name:
            return f"name:{match_name}"
        return "unknown"

    def _load_incremental_baseline(self) -> tuple[list, dict]:
        data = read_json_file(TINDER_BASELINE_FILE, default=[])
        if not isinstance(data, list):
            return [], {}

        keyed = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            key = self._conversation_key(
                item.get("match_id", ""),
                item.get("match_name", ""),
                item.get("match_index", ""),
            )
            keyed[key] = item
        return data, keyed

    @staticmethod
    def _inbound_signature(messages: list) -> tuple[str, ...]:
        normalized = []
        for item in messages or []:
            sender = item.get("sender", "")
            text = " ".join((item.get("text", "") or "").split())
            if sender == "them" and text:
                normalized.append(text)
        return tuple(normalized)

    @staticmethod
    def _restore_inbound_signature(raw_value) -> tuple[str, ...]:
        if not isinstance(raw_value, (list, tuple)):
            return ()
        restored = []
        for item in raw_value:
            text = " ".join(str(item or "").split())
            if text:
                restored.append(text)
        return tuple(restored)

    @staticmethod
    def _parse_baseline_timestamp(raw_value: str) -> datetime | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    def _update_incremental_baseline(
        self,
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
                        item_key = self._conversation_key(
                            item.get("match_id", ""),
                            item.get("match_name", ""),
                            item.get("match_index", ""),
                        )
                        keyed[item_key] = item

                key = self._conversation_key(match_id, match_name, 0)
                existing = keyed.get(key) or {}
                entry = {
                    "match_id": match_id,
                    "match_name": match_name,
                    "match_index": 0,
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

            update_json_file(TINDER_BASELINE_FILE, mutate, default=[])
        except Exception as e:
            self._log("warning", f"更新 history_baseline 失败: {e}")

    def _persisted_snapshot_key(self, snapshot_key: str, *, context: str = "") -> str:
        key = str(snapshot_key or "").strip()
        if key:
            return key
        label = f"({context})" if context else ""
        self._log("warning", f"[语料飞轮] 快照写入失败{label}，后续 outcome 将跳过精确关联")
        return MISSING_SNAPSHOT_KEY

    def _record_partner_followup_if_needed(self, match_id: str, match_name: str, messages: list, prev_entry: dict | None = None) -> None:
        prev_entry = prev_entry or {}
        handled_reason = str(prev_entry.get("last_handled_inbound_reason", "") or "")
        if handled_reason not in {"replied", "opened", "reactivated"}:
            return

        current_inbound = self._inbound_signature(messages)
        if not current_inbound:
            return

        recorded_signature = self._restore_inbound_signature(prev_entry.get("last_partner_followup_signature"))
        if current_inbound == recorded_signature:
            return

        event, reason = classify_partner_followup_quality(messages)
        record_runtime_feedback(
            "tinder",
            match_id,
            match_name or "Unknown",
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
                    self.corpus_store.update_outcome(
                        match_id,
                        outcome,
                        outcome_label,
                        platform="tinder",
                        snapshot_key=snapshot_key,
                    )
                except Exception as exc:
                    self._log("warning", f"[语料飞轮] 更新后续回应结果失败: {exc}")
            elif snapshot_key == MISSING_SNAPSHOT_KEY:
                self._log("warning", "[语料飞轮] 缺少有效 snapshot_key，跳过后续回应结果精确回写")
            else:
                self._log("info", "[语料飞轮] 历史 baseline 无 snapshot_key，跳过 outcome 精确回写")
            self._append_pending_feedback_snapshot(
                match_id,
                match_name or "Unknown",
                messages,
                outcome=outcome,
                outcome_label=outcome_label,
                feedback_event=event,
                feedback_reason=reason,
                snapshot_key=snapshot_key if snapshot_key and snapshot_key != MISSING_SNAPSHOT_KEY else "",
            )
        self._update_incremental_baseline(
            match_id,
            match_name or "Unknown",
            messages,
            metadata={
                "last_partner_followup_signature": list(current_inbound),
                "last_partner_followup_event": event,
                "last_partner_followup_reason": reason,
            },
        )

    def _append_pending_feedback_snapshot(
        self,
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
            "platform": "tinder",
            "match_id": match_id,
            "match_name": match_name,
            "match_index": 0,
            "messages": messages,
            "outcome": outcome,
            "outcome_label": outcome_label,
            "feedback_event": feedback_event,
            "feedback_reason": feedback_reason,
            "timestamp": datetime.now().isoformat(),
        }
        if snapshot_key:
            entry["snapshot_key"] = snapshot_key
        try:
            queue_file = Path(__file__).parent.parent / "pending_corpus.jsonl"
            with open(queue_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log("warning", f"pending_corpus 反馈快照写入失败: {e}")

    @staticmethod
    def _load_runtime_state() -> dict:
        data = read_json_file(TINDER_RUNTIME_STATE_FILE, default={})
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _save_runtime_state(state: dict) -> None:
        try:
            write_json_file(TINDER_RUNTIME_STATE_FILE, state if isinstance(state, dict) else {})
        except Exception:
            pass

    def _remember_last_message_match_id(self, match_id: str) -> None:
        normalized = str(match_id or "").strip()
        if not normalized:
            return

        def mutate(state):
            state = state if isinstance(state, dict) else {}
            state["last_message_match_id"] = normalized
            return state

        try:
            update_json_file(TINDER_RUNTIME_STATE_FILE, mutate, default={})
        except Exception:
            pass

    def _load_recent_message_match_ids(self, limit: int = 20) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []

        def _push(match_id: str) -> None:
            normalized = str(match_id or "").strip()
            if (
                not normalized
                or normalized in seen
                or normalized.startswith("test_")
            ):
                return
            seen.add(normalized)
            result.append(normalized)

        state = self._load_runtime_state()
        _push(state.get("last_message_match_id", ""))

        try:
            _, keyed = self._load_incremental_baseline()
            for entry in keyed.values():
                _push(entry.get("match_id", ""))
                if len(result) >= limit:
                    return result[:limit]
        except Exception:
            pass

        db_path = getattr(self.corpus_store, "db_path", None)
        if db_path and Path(db_path).exists():
            conn = None
            try:
                conn = sqlite3.connect(db_path, timeout=30)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")
                rows = conn.execute(
                    """
                    SELECT match_id
                    FROM conversations
                    WHERE platform = 'tinder'
                      AND match_id IS NOT NULL
                      AND match_id != ''
                      AND match_id NOT LIKE 'test_%'
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                for (match_id,) in rows:
                    _push(match_id)
                    if len(result) >= limit:
                        break
            except Exception as exc:
                self._log("warning", f"[Tinder] 读取近期 match_id 失败: {exc}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        return result[:limit]

    def _bootstrap_message_surface_from_recent_chats(self, limit: int = MESSAGE_SURFACE_BOOTSTRAP_MATCH_LIMIT) -> bool:
        candidate_ids = self._load_recent_message_match_ids(limit=max(limit * 2, 20))
        if not candidate_ids:
            self._log("warning", "[Tinder] 无可用近期 match_id，无法执行消息页深链引导")
            return False

        for match_id in candidate_ids[:limit]:
            msg_url = f"https://tinder.com/app/messages/{match_id}"
            try:
                self._log("warning", f"[Tinder] 尝试近期会话深链引导: {match_id}")
                self.page.goto(msg_url, timeout=15000)
                time.sleep(4)
                cards = self._conversation_anchor_count()
                if cards > 0:
                    self._remember_last_message_match_id(match_id)
                    self._log("info", f"[Tinder] 深链引导成功: {match_id} (conversation_cards={cards})")
                    return True
            except Exception as exc:
                self._log("warning", f"[Tinder] 深链引导失败 {match_id}: {exc}")

        return False

    def _should_run_dormant_reactivation_round(self) -> tuple[bool, str]:
        state = self._load_runtime_state()
        raw_ts = str(state.get("last_dormant_reactivation_round_at", "") or "")
        if not raw_ts:
            return True, "首次沉睡激活轮次"
        try:
            last_run_at = datetime.fromisoformat(raw_ts)
        except Exception:
            return True, "沉睡激活轮次时间损坏，允许重置"
        gap = datetime.now() - last_run_at
        if gap < timedelta(hours=DORMANT_REACTIVATION_ROUND_COOLDOWN_HOURS):
            hours_left = max(
                1,
                int(
                    (
                        timedelta(hours=DORMANT_REACTIVATION_ROUND_COOLDOWN_HOURS) - gap
                    ).total_seconds() // 3600
                ),
            )
            return False, f"距离上次沉睡激活轮次不足{DORMANT_REACTIVATION_ROUND_COOLDOWN_HOURS}h，还需约{hours_left}h"
        return True, "允许进入沉睡激活轮次"

    def _mark_dormant_reactivation_round_started(self) -> None:
        def mutate(state):
            state = state if isinstance(state, dict) else {}
            state["last_dormant_reactivation_round_at"] = datetime.now().isoformat()
            return state

        try:
            update_json_file(TINDER_RUNTIME_STATE_FILE, mutate, default={})
        except Exception:
            pass

    @staticmethod
    def _dormant_candidate_key(candidate: dict) -> str:
        entry = candidate.get("entry", {}) or {}
        return (
            str(candidate.get("match_id") or "")
            or str(entry.get("match_id") or "")
            or str(entry.get("href") or "")
            or str(candidate.get("match_name") or "")
        )

    def _rotate_dormant_candidates(self, candidates: list[dict]) -> tuple[list[dict], str]:
        total = len(candidates)
        if total <= 1:
            return candidates, ""
        state = self._load_runtime_state()
        last_key = str(state.get("last_dormant_scan_key", "") or "")
        if not last_key:
            return candidates, ""
        for index, candidate in enumerate(candidates):
            if self._dormant_candidate_key(candidate) == last_key:
                next_index = (index + 1) % total
                if next_index == 0:
                    return candidates, last_key
                return candidates[next_index:] + candidates[:next_index], last_key
        return candidates, ""

    def _advance_dormant_scan_cursor(self, last_attempted_key: str) -> None:
        if not last_attempted_key:
            return

        def mutate(state):
            state = state if isinstance(state, dict) else {}
            state["last_dormant_scan_key"] = last_attempted_key
            state.pop("dormant_scan_cursor", None)
            return state

        try:
            update_json_file(TINDER_RUNTIME_STATE_FILE, mutate, default={})
        except Exception:
            pass

    @staticmethod
    def _tail_signature(items: list, size: int = 3) -> tuple:
        tail = items[-size:]
        normalized = []
        for item in tail:
            sender = item.get("sender", "")
            text = " ".join((item.get("text", "") or "").split())
            cursor = (
                item.get("message_key")
                or item.get("timestamp")
                or item.get("datetime")
                or item.get("id")
                or text
            )
            normalized.append((sender, str(cursor), text))
        return tuple(normalized)

    def _get_dormant_reactivation_candidate(self, match_id: str, match_name: str, messages: list) -> tuple[bool, str]:
        messages = sanitize_messages_for_context(self._trim_trailing_fallback_messages(messages))
        _, keyed = self._load_incremental_baseline()
        prev_entry = keyed.get(self._conversation_key(match_id, match_name, 0)) or {}
        current_sig = list(self._tail_signature(messages))
        current_sig_tuple = tuple(current_sig)

        dormant_sig = tuple(prev_entry.get("dormant_signature", []) or [])
        dormant_since = str(prev_entry.get("dormant_since", "") or "")
        if not dormant_since or dormant_sig != current_sig_tuple:
            self._update_incremental_baseline(
                match_id,
                match_name,
                messages,
                metadata={
                    "dormant_signature": current_sig,
                    "dormant_since": datetime.now().isoformat(),
                },
            )
            return False, f"沉睡计时开始，等待{DORMANT_REACTIVATION_MIN_DORMANT_HOURS}h"

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
            min_dormant_hours=DORMANT_REACTIVATION_MIN_DORMANT_HOURS,
            min_reactivation_gap_hours=DORMANT_REACTIVATION_CONTACT_COOLDOWN_HOURS,
            max_reactivation_attempts=DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_CONTACT,
        )
        if not should_activate:
            return False, reason

        last_reactivation_sig = prev_entry.get("last_reactivation_signature", [])
        last_reactivation_at = str(prev_entry.get("last_reactivation_at", "") or "")
        if current_sig == last_reactivation_sig and last_reactivation_at:
            return False, "相同沉睡对话已激活过，等待下次变化"

        return True, "允许沉睡激活"

    def _mark_dormant_reactivation_sent(self, match_id: str, match_name: str, messages: list, reason: str = "reactivated") -> None:
        messages = sanitize_messages_for_context(self._trim_trailing_fallback_messages(messages))
        current_sig = list(self._tail_signature(messages))
        _, keyed = self._load_incremental_baseline()
        prev_entry = keyed.get(self._conversation_key(match_id, match_name, 0)) or {}
        current_attempt_count = int(
            prev_entry.get(
                "reactivation_attempt_count",
                1 if str(prev_entry.get("last_reactivation_at", "") or "") else 0,
            ) or 0
        )
        self._update_incremental_baseline(
            match_id,
            match_name,
            messages,
            handled_inbound_signature=self._inbound_signature(messages),
            handled_inbound_reason=reason,
            metadata={
                "dormant_signature": current_sig,
                "dormant_since": datetime.now().isoformat(),
                "last_reactivation_signature": current_sig,
                "last_reactivation_at": datetime.now().isoformat(),
                "last_reactivation_reason": reason,
                "reactivation_attempt_count": current_attempt_count + 1,
            },
        )

    def _log(self, level: str, message: str) -> None:
        formatted = f"[Bot] {message}"
        print(formatted)
        log_fn = getattr(logger, level, logger.info)
        log_fn(message)
        if level in {"warning", "error"}:
            self.error_log.append({
                "time": datetime.now().isoformat(),
                "level": level,
                "error": message,
            })

    def _wait_for_message_cards(self, timeout: int = 10000) -> bool:
        try:
            self.page.wait_for_selector('a[href*="/app/messages/"], [role="tab"]', state="attached", timeout=timeout)
            time.sleep(2)
            return True
        except Exception:
            return False

    def _list_message_anchor_cards(self, limit: int = 80) -> list[dict[str, str]]:
        expected_min = min(max(1, limit), MESSAGE_ANCHOR_MIN_EXPECTED_COUNT)
        script = """(limit) => Array.from(document.querySelectorAll('a[href*="/app/messages/"]'))
            .slice(0, limit)
            .map(a => {
                const parts = (a.innerText || '')
                    .split('\\n')
                    .map(x => x.trim())
                    .filter(Boolean);
                const rect = a.getBoundingClientRect();
                const hasNewMatchBadge =
                    !!a.querySelector('div[role="img"][aria-label*="新的配对"]') ||
                    !!a.querySelector('div[role="img"][aria-label*="新配对"]') ||
                    !!a.querySelector('div[role="img"][aria-label*="new match"]');
                return {
                    href: a.href || '',
                    name: parts[0] || '',
                    preview: parts.slice(1).join(' ').trim(),
                    y: Number.isFinite(rect.y) ? rect.y : 9999,
                    viewport_height: window.innerHeight || 900,
                    has_new_match_badge: hasNewMatchBadge,
                };
            })"""
        best_items: list[dict[str, str]] = []
        for attempt in range(1, MESSAGE_ANCHOR_EVALUATE_ATTEMPTS + 1):
            try:
                raw_items = self.page.evaluate(script, limit)
                items = raw_items if isinstance(raw_items, list) else []
                if len(items) > len(best_items):
                    best_items = items
                if len(items) >= expected_min:
                    return items
                self._log(
                    "warning" if attempt == MESSAGE_ANCHOR_EVALUATE_ATTEMPTS else "info",
                    f"[Tinder] 消息列表链接数量偏低 "
                    f"(attempt={attempt}/{MESSAGE_ANCHOR_EVALUATE_ATTEMPTS}, "
                    f"count={len(items)}, expected_min={expected_min}, limit={limit})",
                )
            except Exception as exc:
                self._log(
                    "warning",
                    f"[Tinder] 消息列表链接 evaluate 失败 "
                    f"(attempt={attempt}/{MESSAGE_ANCHOR_EVALUATE_ATTEMPTS}): {exc}",
                )
            if attempt < MESSAGE_ANCHOR_EVALUATE_ATTEMPTS:
                try:
                    self.page.wait_for_timeout(MESSAGE_ANCHOR_RETRY_WAIT_MS)
                except Exception:
                    time.sleep(MESSAGE_ANCHOR_RETRY_WAIT_MS / 1000)

        if len(best_items) < expected_min:
            self._log(
                "warning",
                f"[Tinder] 消息列表收集数量异常低: {len(best_items)}，"
                "可能页面未完全加载或消息列表未挂载",
            )
        return best_items

    @staticmethod
    def _looks_like_new_match_anchor(item: dict[str, str]) -> bool:
        y = float(item.get("y") or 9999)
        viewport_height = max(float(item.get("viewport_height") or 900), 1.0)
        has_badge = bool(item.get("has_new_match_badge"))
        preview = str(item.get("preview", "") or "").strip()
        # /app/matches 顶部新配对卡经常也带 /messages/ 链接；它们通常位于顶部且没有正常 preview。
        return has_badge or ((y / viewport_height) < 0.3 and not preview)

    def _conversation_anchor_count(self) -> int:
        try:
            raw_items = self._list_message_anchor_cards(limit=80)
            return sum(1 for item in raw_items if not self._looks_like_new_match_anchor(item))
        except Exception:
            return 0

    def _message_list_has_native_empty_state(self) -> bool:
        """区分真实空列表和 Tinder 前端/后端空载故障。"""
        try:
            text = self.page.evaluate(
                """() => {
                    const root = document.querySelector('[class*=sidebar]') || document.body;
                    return (root && root.innerText || '').replace(/\\s+/g, ' ').trim();
                }"""
            )
        except Exception:
            return False
        candidate = str(text or "")
        if not candidate:
            return False
        empty_markers = (
            "开始滑动", "暂无消息", "没有消息", "还没有配对", "没有配对",
            "Start swiping", "No messages", "No Matches", "No matches",
            "Matches will appear", "New matches will appear",
        )
        return any(marker in candidate for marker in empty_markers)

    def _messages_tab_is_selected(self) -> bool:
        try:
            selected = self.page.evaluate(
                """() => {
                    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
                    const selectedTab = tabs.find(tab =>
                        tab.getAttribute('aria-selected') === 'true' ||
                        tab.getAttribute('aria-current') === 'page' ||
                        tab.dataset?.selected === 'true'
                    );
                    if (!selectedTab) return false;
                    const text = ((selectedTab.innerText || selectedTab.textContent || '') + '').trim().toLowerCase();
                    return text === 'messages' || text === '消息';
                }"""
            )
            return bool(selected)
        except Exception:
            return False

    def _message_surface_state(self) -> dict[str, object]:
        try:
            return {
                "url": self.page.url or "",
                "messages_tab_selected": self._messages_tab_is_selected(),
                "conversation_cards": self._conversation_anchor_count(),
                "tab_count": self.page.locator('[role="tab"]').count(),
                "chat_log_count": self.page.locator('[role="log"]').count(),
                "input_count": self.page.locator('textarea, div[contenteditable]').count(),
            }
        except Exception:
            return {
                "url": self.page.url or "",
                "messages_tab_selected": False,
                "conversation_cards": 0,
                "tab_count": 0,
                "chat_log_count": 0,
                "input_count": 0,
            }

    def _classify_message_surface_issue(self) -> str:
        if self.is_message_list_loading():
            return "loading_spinner"

        state = self._message_surface_state()
        cards = int(state.get("conversation_cards") or 0)
        tab_count = int(state.get("tab_count") or 0)
        tab_selected = bool(state.get("messages_tab_selected"))
        chat_log_count = int(state.get("chat_log_count") or 0)
        input_count = int(state.get("input_count") or 0)

        if cards > 0:
            return "healthy"
        if tab_count == 0 and chat_log_count == 0 and input_count == 0:
            return "blank_or_unmounted"
        if tab_count == 0 and (chat_log_count > 0 or input_count > 0):
            return "chat_only_no_sidebar"
        if tab_count > 0 and not tab_selected:
            return "messages_tab_unselected"
        if tab_selected and cards == 0 and (chat_log_count > 0 or input_count > 0):
            return "sidebar_missing"
        return "messages_surface_unready"

    def _collect_message_cards(self, limit: int = 13) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        seen = set()

        raw_limit = max(limit * 4, 40)
        raw_items = self._list_message_anchor_cards(limit=raw_limit)
        self._log("info", f"收集到 {len(raw_items)} 个原始 /messages/ 链接元素")
        if len(raw_items) < min(MESSAGE_ANCHOR_MIN_EXPECTED_COUNT, raw_limit):
            self._log(
                "warning",
                f"消息列表原始链接数量低于预期: {len(raw_items)}/{raw_limit}，"
                "本轮可能只覆盖已渲染联系人",
            )

        filtered_items = [item for item in raw_items if not self._looks_like_new_match_anchor(item)]
        if not filtered_items and raw_items and self._messages_tab_is_selected():
            filtered_items = raw_items
            self._log("warning", "新配对过滤后无候选，但当前已在消息标签，回退使用原始链接列表")
        self._log("info", f"过滤新配对卡后保留 {len(filtered_items)} 个真实对话候选")

        for item in filtered_items[:limit]:
            href = item.get("href", "")
            name = item.get("name", "")
            preview = item.get("preview", "")
            if len(href) > 30 and href not in seen:
                seen.add(href)
                match_id = href.split('/messages/')[-1] if '/messages/' in href else ''
                entries.append({
                    "href": href,
                    "name": name,
                    "match_id": match_id,
                    "preview": preview,
                })

        return entries

    def _collect_all_message_cards(
        self,
        max_cards: int = 200,
        max_scrolls: int = DORMANT_REACTIVATION_MESSAGE_LIST_MAX_SCROLLS,
    ) -> list[dict[str, str]]:
        collected: dict[str, dict[str, str]] = {}
        stagnant_rounds = 0
        last_count = 0

        for _ in range(max_scrolls):
            for item in self._collect_message_cards(limit=max_cards):
                href = item.get("href", "")
                if href and href not in collected:
                    collected[href] = item

            current_count = len(collected)
            if current_count >= max_cards:
                break

            moved = self.page.evaluate(
                """() => {
                    let sc = null;
                    const anchors = Array.from(document.querySelectorAll('a[href*="/app/messages/"]'));
                    for (const anchor of anchors) {
                        let node = anchor.parentElement;
                        for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                            if (node.scrollHeight > node.clientHeight + 50) {
                                sc = node;
                                break;
                            }
                        }
                        if (sc) break;
                    }
                    if (!sc) {
                        const cs = document.querySelectorAll('[class*=list], [class*=conversation], [class*=message], [class*=panel], [class*=item]');
                        for (let i = 0; i < cs.length; i++) {
                            const c = cs[i];
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
                }"""
            )
            time.sleep(0.5)

            if current_count == last_count and not moved:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            last_count = current_count
            if stagnant_rounds >= 3:
                break

        entries = list(collected.values())[:max_cards]
        self._log("info", f"[激活] 扩展收集到 {len(entries)} 个联系人供沉睡激活轮转")
        return entries

    @staticmethod
    def _is_like_preview(preview: str) -> bool:
        text = re.sub(r"\s+", " ", (preview or "")).strip().lower()
        return any(
            phrase in text
            for phrase in (
                "赞了你的消息",
                "liked your message",
                "liked one of your messages",
                "reacted to your message",
            )
        )

    def _append_preview_reaction_event(self, messages: list, entry: dict[str, str]) -> list:
        preview = entry.get("preview", "")
        if not self._is_like_preview(preview):
            return messages

        normalized = sanitize_messages_for_context(
            self._trim_trailing_fallback_messages(messages)
        )
        if normalized and is_like_reaction_message(normalized[-1]):
            return normalized

        enriched = list(normalized)
        enriched.append({
            "sender": "them",
            "is_mine": False,
            "text": "[liked your message]",
            "meta_type": "reaction_like",
            "raw_preview": preview,
        })
        self._log("info", f"检测到点赞类侧栏预览，补充特殊入站事件: {preview[:30]}")
        return enriched

    def _open_chat(self, entry: dict[str, str]) -> bool:
        absolute_url = entry.get("href", "")
        match_id = entry.get("match_id", "")
        for _ in range(2):
            if match_id:
                try:
                    sidebar_link = self.page.locator(f'a[href*="/app/messages/{match_id}"]').first
                    if sidebar_link.count() > 0 and sidebar_link.is_visible(timeout=2000):
                        sidebar_link.click(force=True, timeout=5000)
                        time.sleep(2)
                        current_url = self.page.url
                        if match_id in current_url:
                            self._remember_last_message_match_id(match_id)
                            return True
                    else:
                        self._log("info", f"侧栏未命中 match_id={match_id}，回退 goto")
                except Exception as click_err:
                    self._log("warning", f"侧栏点击失败，回退 goto: {click_err}")

            self.page.goto(absolute_url, timeout=15000)
            time.sleep(5)
            current_url = self.page.url
            if '/messages/' in current_url and len(current_url.split('/messages/')[-1]) > 10:
                self._remember_last_message_match_id(current_url.split('/messages/')[-1])
                return True
            self._log("warning", f"URL 重定向到 {current_url}，重新进入消息列表")
            self.page.goto("https://tinder.com/app/matches", timeout=15000)
            time.sleep(3)
        return False

    def _fallback_opening_line(self, match_name: str = "", bio: str = "", age: int = 0) -> str:
        shared = build_contextual_fallback_reply([], bio=bio, age=age, platform="tinder")
        if shared:
            return shared
        clean_name = (match_name or "").strip()
        if clean_name:
            return f"嗨{clean_name} 先打个招呼"
        return "先打个招呼"

    def _generate_or_fetch_reply(self, match_id: str, messages: list, bio: str, age: int, match_name: str = "", intent: str = "reply"):
        cached = get_reply("tinder", match_id)
        if cached:
            return cached, True
        reply = ure_generate_reply(messages, bio=bio, age=age, platform="tinder", intent=intent)
        if not reply and not messages and intent == "reply":
            reply = self._fallback_opening_line(match_name, bio=bio, age=age)
        return reply, False

    def generate_reply(self, messages: list, bio: str = "", age: int = 0) -> str:
        """
        兼容旧调用方的包装函数。
        当前底层统一走 shared_assets.unified_reply_engine。
        """
        return ure_generate_reply(messages, bio=bio, age=age, platform="tinder")

    def _ensure_proxy_sticky(self):
        """确保代理会话粘性(5分钟内保持同一IP)"""
        elapsed = time.time() - self.proxy_session_start

        if self.current_proxy is None or elapsed > self.config.get("proxy_sticky_duration", 300):
            # 获取新代理或保持当前
            new_proxy = self.proxy_rotator.get_next_proxy()
            if new_proxy:
                self.current_proxy = new_proxy
                self.proxy_session_start = time.time()
                self._log("info", f"[Proxy] 切换新代理: {new_proxy.get('server', 'none')}")

    def _open_new_matches_surface(self) -> bool:
        """优先打开新配对弹层所在页面，失败时回退到 /app/matches。"""
        current_url = self.page.url or ""
        candidate_urls = []
        if "tinder.com/app/" in current_url:
            candidate_urls.append(current_url)
        candidate_urls.extend([
            "https://tinder.com/app/matches",
            "https://tinder.com/app/connections",
        ])

        seen = set()
        for url in candidate_urls:
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                if self.page.url != url:
                    self.page.goto(url, timeout=15000)
                    self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                    time.sleep(2)
                self._log("info", f"[Tinder] 新配对入口页面: {self.page.url}")
                return True
            except Exception as exc:
                self._log("warning", f"[Tinder] 打开 {url} 失败: {exc}")
        return False

    def _has_new_match_modal_cta(self) -> bool:
        selectors = [
            'button:has-text("轻按与")',
            '[role="button"]:has-text("轻按与")',
            'a:has-text("轻按与")',
            '[aria-label="match"] button',
            '[aria-label="match"] [role="button"]',
        ]
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                if locator.count() > 0 and locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _extract_match_name_from_cta(self, cta_text: str) -> str:
        cleaned = (cta_text or "").strip().replace("\n", " ")
        if "轻按与" in cleaned and "聊天" in cleaned:
            return cleaned.split("轻按与", 1)[-1].split("聊天", 1)[0].strip("！! ")
        return cleaned[:30]

    def _collect_new_match_modal_ctas(self) -> list[dict[str, str]]:
        selectors = [
            'button:has-text("轻按与")',
            '[role="button"]:has-text("轻按与")',
            'a:has-text("轻按与")',
        ]
        seen = set()
        entries: list[dict[str, str]] = []
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                for idx in range(locator.count()):
                    text = (locator.nth(idx).inner_text() or "").strip()
                    if not text or text in seen:
                        continue
                    seen.add(text)
                    entries.append(
                        {
                            "text": text,
                            "name": self._extract_match_name_from_cta(text),
                        }
                    )
            except Exception:
                continue
        return entries

    def _click_next_new_match_entry(self, processed_targets: set[str]) -> tuple[bool, str, str]:
        """点击下一个新配对入口，优先处理弹层 CTA，失败时回退旧卡片路径。"""
        for entry in self._collect_new_match_modal_ctas():
            target_key = f"modal:{entry['text']}"
            if target_key in processed_targets:
                continue
            modal_selectors = [
                f'button:has-text("{entry["text"]}")',
                f'[role="button"]:has-text("{entry["text"]}")',
                f'a:has-text("{entry["text"]}")',
            ]
            for selector in modal_selectors:
                try:
                    cta = self.page.locator(selector).first
                    if cta.count() == 0:
                        continue
                    cta.click(force=True, timeout=5000)
                    return True, entry["name"], target_key
                except Exception:
                    continue

        cards = [card for card in self._collect_new_match_cards() if card["href"] not in processed_targets]
        if not cards:
            return False, "", ""

        current = cards[0]
        href = current["href"]
        match_name = current["name"]
        try:
            card = self.page.locator(f'a.matchListItem.focus-button-style[href="{href}"]').first
            card.click(force=True, timeout=5000)
            return True, match_name, href
        except Exception as exc:
            self._log("warning", f"[Tinder] 点击新配对卡片失败: {exc}")
            return False, match_name, href

    def _count_pending_new_match_entries(self, processed_targets: set[str]) -> int:
        pending_ctas = [
            entry for entry in self._collect_new_match_modal_ctas()
            if f"modal:{entry['text']}" not in processed_targets
        ]
        pending_cards = [
            card for card in self._collect_new_match_cards()
            if card["href"] not in processed_targets
        ]
        return len(pending_ctas) + len(pending_cards)

    def _has_new_matches_modal(self) -> bool:
        """检查 /app/matches 顶部是否存在可处理的新配对卡片。"""
        try:
            if not self._open_new_matches_surface():
                return False
            if self._has_new_match_modal_cta():
                self._log("info", "[Tinder] 检测到“新的配对”弹层 CTA")
                return True
            cards = self._collect_new_match_cards()
            self._log("info", f"[Tinder] /app/matches 顶部候选新配对卡片数: {len(cards)}")
            return len(cards) > 0
        except Exception:
            return False

    def _collect_new_match_cards(self):
        selector = 'a.matchListItem.focus-button-style[href*="/app/messages/"]'
        candidates = self.page.locator(selector).all()
        unique_cards = []
        seen_hrefs = set()

        for card in candidates:
            try:
                href = (card.get_attribute("href") or "").strip()
                if not href or href in seen_hrefs:
                    continue

                badge_count = card.locator('div[role="img"][aria-label="新的配对"]').count()
                if badge_count == 0:
                    card_box = card.bounding_box()
                    if not card_box or card_box.get("y", 9999) > 260:
                        continue

                name = ""
                try:
                    name = (card.inner_text() or "").split("\n")[0].strip()
                except Exception:
                    name = ""

                seen_hrefs.add(href)
                unique_cards.append(
                    {
                        "href": href,
                        "name": name,
                        "has_badge": badge_count > 0,
                    }
                )
            except Exception:
                continue

        return unique_cards

    def _page_text_preview(self, limit: int = 400) -> str:
        try:
            text = self.page.evaluate(
                """() => ((document.body && (document.body.innerText || document.body.textContent)) || '')
                .replace(/\\s+/g, ' ')
                .trim()"""
            )
            return text[:limit]
        except Exception:
            return ""

    def check_new_matches(self) -> int:
        """
        处理 /app/matches 顶部“新的配对”卡片。
        进入聊天面板后生成开场白并发送，再按 Escape 返回列表。
        """
        sent_count = 0
        processed_targets = set()
        try:
            if not self._open_new_matches_surface():
                return sent_count

            round_index = 0
            while True:
                pending_count = self._count_pending_new_match_entries(processed_targets)
                self._log("info", f"[Tinder] 本轮待处理新匹配入口数: {pending_count}")
                if pending_count == 0:
                    if round_index == 0:
                        self._log("info", "[Tinder] 未检测到可处理的新配对卡片")
                    break

                round_index += 1
                try:
                    clicked, match_name, target_key = self._click_next_new_match_entry(processed_targets)
                    if not clicked:
                        break
                    self._log("info", f"[Tinder] 处理新匹配入口: {match_name or 'unknown'}")
                    self._log("info", f"[Tinder] 点击后 URL: {self.page.url}")

                    try:
                        self.page.wait_for_selector(
                            'textarea, div[contenteditable="true"][role="textbox"], [role="textbox"], div[role="log"]',
                            timeout=10000,
                        )
                        self._log("info", f"[Tinder] 聊天面板已出现，当前 URL: {self.page.url}")
                    except Exception as e:
                        self._handle_webdriver_exception(e, "check_new_matches wait_chat")
                        self._log(
                            "warning",
                            f"[Tinder] 等待聊天面板超时: {e}; 页面摘要: {self._page_text_preview()}",
                        )
                        continue

                    bio = self._extract_profile_bio()
                    age = self._extract_match_age()
                    self._log("info", f"[Tinder] 新匹配资料: age={age}, bio={bio[:80] if bio else 'N/A'}")
                    current_url = self.page.url
                    match_id = current_url.split('/messages/')[-1] if '/messages/' in current_url else current_url
                    reply, used_cache = self._generate_or_fetch_reply(match_id, [], bio, age, match_name=match_name)
                    send_ok = False
                    if reply and reply.strip():
                        self._log("info", f"[Tinder] 开场白长度: {len(reply)} 内容: {reply[:80]}")
                        send_ok = self.send_reply(reply, messages=[])
                        self._log("info", f"[Tinder] send_message 返回: {send_ok}")
                        if send_ok:
                            if used_cache:
                                confirm_sent("tinder", match_id)
                            snapshot_key = self.record_conversation(
                                match_id,
                                match_name or "Unknown",
                                [],
                                reply,
                                intent="opener",
                            )
                            persisted_snapshot_key = self._persisted_snapshot_key(snapshot_key, context="tinder opener")
                            self._update_incremental_baseline(
                                match_id,
                                match_name or "Unknown",
                                [{"sender": "me", "text": reply, "is_mine": True}],
                                handled_inbound_signature=tuple(),
                                handled_inbound_reason="opened",
                                metadata={"last_snapshot_key": persisted_snapshot_key},
                            )
                            record_runtime_feedback(
                                "tinder",
                                match_id,
                                match_name or "Unknown",
                                "opener_sent",
                                intent="opener",
                                reply=reply,
                                messages=[],
                                metadata={"used_cache": used_cache},
                            )
                            sent_count += 1
                        else:
                            record_runtime_feedback(
                                "tinder",
                                match_id,
                                match_name or "Unknown",
                                "opener_send_failed",
                                intent="opener",
                                reply=reply,
                                messages=[],
                                metadata=get_last_send_diagnostics(self.page),
                            )
                            self._log("warning", f"[Tinder] 开场白发送失败，页面摘要: {self._page_text_preview()}")
                    else:
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "opener_no_safe_reply",
                            intent="opener",
                            reason="no_safe_opener",
                            messages=[],
                        )
                        self._log("warning", "[Tinder] generate_reply 返回空内容")

                    processed_targets.add(target_key or match_id)
                    if not self._open_new_matches_surface():
                        break
                    self._log("info", f"[Tinder] 已返回新配对入口页，当前 URL: {self.page.url}")

                except Exception as e:
                    self._handle_webdriver_exception(e, "check_new_matches item")
                    self._log("warning", f"处理新匹配卡片失败: {e}")
                    if not self._open_new_matches_surface():
                        break
                    continue

            return sent_count
        except Exception as e:
            self._handle_webdriver_exception(e, "check_new_matches")
            self._log("warning", f"[Tinder] 检查新配对失败: {e}")
            return sent_count

    def check_error_state(self) -> str:
        """检查异常状态"""
        try:
            error_patterns = [
                ("div:has-text('已注销')", "对方已注销"),
                ("div:has-text('账号已被注销')", "对方已注销"),
                ("div:has-text('真人验证码')", "需要真人验证码"),
                ("div:has-text('验证')", "需要验证"),
                ("div:has-text('过于频繁')", "操作过于频繁"),
            ]

            for pattern, label in error_patterns:
                if self.page.query_selector(pattern):
                    return label

            try:
                input_box = self.page.query_selector('textarea, div[contenteditable]')
                if input_box:
                    is_disabled = input_box.get_attribute('disabled') or input_box.get_attribute('aria-disabled')
                    if is_disabled:
                        return "输入框不可交互"
            except Exception:
                pass

        except Exception:
            pass

        return None

    def is_message_list_loading(self) -> bool:
        """检测消息列表是否还在转圈加载中（Tinder 后端问题）"""
        try:
            # 查找加载 spinner（圆形旋转图标）
            spinners = self.page.query_selector_all(
                "[class*=spinner], [class*=loading], [class*=loader], [class*=splash], [aria-label*=loading], [role=progressbar]"
            )
            for s in spinners:
                rect = s.bounding_box()
                # 过滤掉视口外或太小的元素（可能是页面其他位置的 loading）
                if rect and rect.get('width', 0) > 20 and rect.get('height', 0) > 20:
                    return True
            # 备选：检查 DOM 里是否有 "加载中" / "loading" / "刷新" 相关文本
            body_text = self.page.inner_text("[class*=sidebar]") or ""
            loading_texts = ["加载中", "loading", "刷新", "稍等", "Loading"]
            if any(t in body_text for t in loading_texts):
                # 但如果 sidebar 有 conversation 链接就不是 loading
                if self._conversation_anchor_count() == 0:
                    return True
            return False
        except Exception:
            return False

    def log_error(self, error: str):
        """记录错误日志"""
        self._log("error", f"📝 错误记录: {error}")

    def setup(self):
        """初始化浏览器和模块"""
        self._log("info", "初始化中...")

        if not self.guard.wait_if_needed():
            raise Exception("生命周期守卫阻止启动")

        # 【代理会话粘性】确保IP稳定
        self._ensure_proxy_sticky()

        # 【改进5】登录态保持:使用 Chromium Profile 持久化
        os.makedirs(self.user_data_dir, exist_ok=True)

        # ── 浏览器生命周期上交 BrowserManager 单例 ──────────────────
        from config import get_config
        from browser_manager import get_browser_manager

        manager = get_browser_manager("tinder", get_config())
        self.browser_manager = manager
        instance = manager.get_instance()
        self.playwright = instance.playwright
        self.context = instance.context
        self.page = instance.page

        # 保留原有的防风控 session 启动
        self.guard.start_session()

        # ── 防风控 session 启动后立即注入反检测脚本 ─────────────
        if not getattr(self.page, "_tinder_stealth_applied", False):
            Stealth().apply_stealth_sync(self.page)
            self.page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});"
            )
            self.page.evaluate(
                """() => {
                // 伪造 WebGL
                const origGetParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p) {
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel Iris OpenGL Engine';
                    return origGetParam.apply(this, arguments);
                };
                // 伪造 chrome runtime
                window.chrome = { runtime: {}, app: {} };
                // 伪造 permissions
                const origPerm = window.navigator.permissions.query;
                window.navigator.permissions.query = (params) =>
                    params.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : origPerm(params);
                console.log('[Stealth] init script injected');
            }"""
            )
            setattr(self.page, "_tinder_stealth_applied", True)

        self._bind_page_helpers()

        self._log("info", "初始化完成")
        return True

    def navigate_to_messages(self):
        """导航到消息页面"""
        try:
            # Tinder SPA: /app/matches 默认显示"配对"标签，需要点击"消息"标签才显示对话列表
            msg_url = "https://tinder.com/app/matches"
            direct_message_url = "https://tinder.com/app/messages"
            tab_message_url = "https://tinder.com/app/matches?tab=messages"
            in_message_surface = (
                "tinder.com/app/matches" in self.page.url
                or "tinder.com/app/messages" in self.page.url
            )
            has_message_list = self._conversation_anchor_count() > 0
            messages_tab_selected = self._messages_tab_is_selected()
            tab_count = self.page.locator('[role="tab"]').count()

            if not in_message_surface:
                self._log("info", f"导航到消息页: {msg_url}")
                self.page.goto(msg_url, timeout=15000)
                time.sleep(5)  # 等待 SPA 初始化（不用 networkidle，会一直等）
                has_message_list = self._conversation_anchor_count() > 0
                messages_tab_selected = self._messages_tab_is_selected()
                tab_count = self.page.locator('[role="tab"]').count()
            elif not has_message_list or not messages_tab_selected:
                try:
                    self.page.wait_for_selector('[role="tab"], [class*="focus-button-style"]', state="attached", timeout=5000)
                except Exception:
                    pass
                time.sleep(2)
                has_message_list = self._conversation_anchor_count() > 0
                messages_tab_selected = self._messages_tab_is_selected()
                tab_count = self.page.locator('[role="tab"]').count()

            if not has_message_list and tab_count == 0:
                for fallback_url in (direct_message_url, tab_message_url):
                    self._log("info", f"消息页为空白，强制打开: {fallback_url}")
                    self.page.goto(fallback_url, timeout=15000)
                    time.sleep(5)
                    has_message_list = self._conversation_anchor_count() > 0
                    messages_tab_selected = self._messages_tab_is_selected()
                    if has_message_list or messages_tab_selected:
                        break
                if not has_message_list and not messages_tab_selected:
                    if self._bootstrap_message_surface_from_recent_chats():
                        has_message_list = self._conversation_anchor_count() > 0
                        messages_tab_selected = self._messages_tab_is_selected()

            # 点击"消息"标签，切换到对话列表视图
            if not has_message_list or not messages_tab_selected:
                try:
                    clicked = False
                    last_error = None
                    for _ in range(2):
                        tab_candidates = [
                            self.page.locator('[role="tab"]').filter(has_text=re.compile(r'^Messages$', re.I)).first,
                            self.page.locator('[role="tab"]').filter(has_text=re.compile(r'^消息$')).first,
                            self.page.locator('[class*="focus-button-style"]').filter(has_text=re.compile(r'^Messages$', re.I)).first,
                            self.page.locator('[class*="focus-button-style"]').filter(has_text=re.compile(r'^消息$')).first,
                        ]

                        for msg_tab in tab_candidates:
                            try:
                                if msg_tab.count() > 0 and msg_tab.is_visible(timeout=1500):
                                    msg_tab.click(timeout=5000)
                                    clicked = True
                                    break
                            except Exception as exc:
                                last_error = exc
                                continue

                        if not clicked:
                            try:
                                tabs = self.page.locator('[role="tab"]')
                                if tabs.count() >= 2:
                                    tabs.nth(1).click(timeout=5000)
                                    clicked = True
                            except Exception as exc:
                                last_error = exc

                        if clicked:
                            self._log("info", "已点击消息标签，等待消息列表渲染")
                            break

                        time.sleep(2)

                    if not clicked:
                        raise RuntimeError(f"未找到 Messages/消息 tab: {last_error}" if last_error else "未找到 Messages/消息 tab")

                    time.sleep(3)  # 等待 SPA 切换渲染完成
                except Exception as click_err:
                    self._handle_webdriver_exception(click_err, "navigate_to_messages tab click")
                    self._log("warning", f"点击消息标签未命中: {click_err}")
                    # 备选：依次尝试真正的消息路由和 tab deep link
                    for fallback_url in (direct_message_url, tab_message_url):
                        try:
                            self.page.goto(fallback_url, timeout=10000)
                            time.sleep(3)
                            has_message_list = self._conversation_anchor_count() > 0
                            messages_tab_selected = self._messages_tab_is_selected()
                            if has_message_list or messages_tab_selected:
                                break
                        except Exception as fallback_exc:
                            self._handle_webdriver_exception(fallback_exc, "navigate_to_messages fallback")
                            continue

            has_message_list = self._conversation_anchor_count() > 0
            messages_tab_selected = self._messages_tab_is_selected()
            surface_issue = self._classify_message_surface_issue()
            self._log(
                "info",
                f"[Tinder] 消息页状态: messages_tab_selected={messages_tab_selected}, "
                f"conversation_cards={self._conversation_anchor_count()}, "
                f"issue={surface_issue}",
            )

            HumanDelay.think()
            return True
        except Exception as e:
            self._handle_webdriver_exception(e, "navigate_to_messages")
            self._log("error", f"导航消息页面失败: {e}")
            return False


    def _extract_messages(self) -> list:
        """提取聊天消息，使用布局计算精准区分敌我，抗类名混淆（从 history_scraper.py 移植）"""
        time.sleep(2)
        return self.page.evaluate("""
            () => {
                const logs = Array.from(document.querySelectorAll('[role="log"]'));
                const chatLog = logs.find(l => l.id && l.id.includes('SC.chat')) || logs[0];
                if (!chatLog) return [];

                const result = [];
                const messageMeta = (node, sender, text) => {
                    const metaNode = node.closest('[role="article"], [role="listitem"], [data-testid], [data-qa-id], [data-id], [id]') || node.parentElement;
                    const timeEl = metaNode ? metaNode.querySelector('time[datetime], [datetime]') : null;
                    const timestamp = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
                    const nodeId = metaNode ? (
                        metaNode.getAttribute('data-id') ||
                        metaNode.getAttribute('data-qa-id') ||
                        metaNode.getAttribute('data-testid') ||
                        metaNode.getAttribute('id') ||
                        ''
                    ) : '';
                    const cursor = nodeId || timestamp;
                    return {
                        timestamp,
                        message_key: cursor ? `${sender}:${cursor}:${text}` : ''
                    };
                };

                // 1) span.text 消息（正文文本，去除 timestamp/label 等 UI 干扰）
                const spans = chatLog.querySelectorAll('span.text');
                spans.forEach(span => {
                    const raw = span.innerText.trim();
                    // 空文本 = GIF/图片多媒体，赋默认值
                    const text = (!raw || raw.length > 500) ? '[收到一个图片/GIF表情]' : raw;
                    // 排除系统时间戳/标签残留（常见格式："HH:MM 发送" 或 "发了张 gif"）
                    if (/^\\d{1,2}:\\d{2}/.test(text) || /发了张/.test(text)) return;

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
                        if (color.includes('255, 255, 255')) is_mine = true;
                    }
                    const sender = is_mine ? 'me' : 'them';
                    result.push({ text, sender, is_mine, ...messageMeta(span, sender, text) });
                });

                // 2) 图片/GIF 附件（不在 span.text 内，用 img 检测）
                const imgNodes = Array.from(chatLog.querySelectorAll('img')).filter(img => {
                    const src = (img.src || '').toLowerCase();
                    return src &&
                        !src.includes('static-assets.gotinder.com') &&
                        (src.includes('tenor.com') || src.includes('media.') || src.includes('cdn.') || src.includes('giphy') || src.includes('.gif'));
                });
                imgNodes.forEach(img => {
                    let is_mine = false;
                    const container = img.closest('[class*="msg"]') || img.parentElement;
                    if (container) {
                        const style = window.getComputedStyle(container);
                        if (style.justifyContent === 'flex-end' || style.alignItems === 'flex-end') is_mine = true;
                    }
                    const text = '[收到一个图片/GIF表情]';
                    const sender = is_mine ? 'me' : 'them';
                    result.push({ text, sender, is_mine, ...messageMeta(img, sender, text) });
                });

                // 3) 去重
                const unique = [];
                for (let i = 0; i < result.length; i++) {
                    const prev = unique[unique.length - 1];
                    if (
                        prev &&
                        prev.text === result[i].text &&
                        (!result[i].message_key || prev.message_key === result[i].message_key)
                    ) continue;
                    unique.push(result[i]);
                }
                return unique;
            }
        """)

    def check_all_contacts(self) -> int:
        """
        废弃红点过滤，全面改用"物理点击 + 提取最后一条消息身份"的分段式阻断遍历。
        检测到未回复消息立刻回复，返回本轮触发回复的次数（int）。
        """
        new_matches_sent = 0
        if self.config.get("browser_headless", False):
            self._log("info", "[Tinder] 无头模式下跳过新配对页预扫，优先保证消息回复主链路")
        else:
            if self._has_new_matches_modal():
                new_matches_sent = self.check_new_matches()
                if new_matches_sent > 0:
                    self._log("info", f"[Tinder] 新配对发送完毕: {new_matches_sent} 条")

        self.navigate_to_messages()
        time.sleep(5)

        if self.is_message_list_loading():
            self._log("warning", "消息列表仍在转圈，尝试自动恢复")
            if not self._recover_message_surface():
                self._log("warning", "消息列表仍在转圈（Tinder 后端问题），跳过本轮巡检")
                raise TinderBackendError("消息列表加载中（Tinder 后端问题）")

        if not self._wait_for_message_cards():
            if self._message_list_has_native_empty_state():
                self._log("info", "Tinder 消息列表为原生空状态，本轮无可巡检联系人")
                return 0
            self._log("warning", "消息卡片仍为空，尝试主动恢复消息页")
            if not self._recover_message_surface() or self._conversation_anchor_count() == 0:
                if self._message_list_has_native_empty_state():
                    self._log("info", "Tinder 消息列表恢复后确认原生空状态，本轮无可巡检联系人")
                    return 0
                self._log("warning", "消息列表空载（Tinder 后端/UI 状态异常），跳过本轮巡检")
                raise TinderBackendError("消息列表空载（Tinder 后端/UI 状态异常）")

        for _ in range(5):
            prev_count = 0
            for _ in range(10):
                self.page.evaluate(
                    """() => {
                        var sc = null;
                        var cs = document.querySelectorAll('[class*=list], [class*=conversation], [class*=message], [class*=panel], [class*=item]');
                        for (var i=0; i<cs.length; i++) {
                            var c = cs[i];
                            if (c.scrollHeight > c.clientHeight + 50) { sc = c; break; }
                        }
                        if (sc) sc.scrollTop += 400;
                        else window.scrollBy(0, 400);
                    }"""
                )
                time.sleep(0.4)
                count = self._conversation_anchor_count()
                if count == prev_count:
                    break
                prev_count = count

        entries = self._collect_message_cards(limit=13)
        base_window = min(5, len(entries))
        scan_limit = base_window
        found_unreplied_in_window = False
        dormant_candidates = []
        self._log(
            "info",
            f"最终收集到 {len(entries)} 个对话卡片，按滚动窗口检测（首轮前 {base_window} 个）",
        )

        reply_triggered = 0

        for index, entry in enumerate(entries):
            if index >= scan_limit:
                self._log("info", f"检测窗口已结束（当前上限 {scan_limit}），跳过本轮剩余联系人")
                break
            try:
                href = entry.get("href", "")
                absolute_url = href if href.startswith('http') else f'https://tinder.com{href}'

                if not self._open_chat({
                    "href": absolute_url,
                    "name": entry.get("name", ""),
                    "match_id": entry.get("match_id", ""),
                }):
                    self._log("warning", f"#{index + 1} 无法加载聊天页: {absolute_url}")
                    continue

                match_id = entry.get("match_id") or (absolute_url.split('/messages/')[-1] if '/messages/' in absolute_url else 'unknown')
                match_name = entry.get("name", "unknown")
                self.page.evaluate(
                    '() => { document.querySelectorAll("[role=log]").forEach(l => l.scrollTop = 0); }'
                )
                time.sleep(0.5)

                messages = self._append_preview_reaction_event(self._extract_messages(), entry)
                effective_messages = sanitize_messages_for_context(
                    self._trim_trailing_fallback_messages(messages)
                )
                bio = self._extract_profile_bio()
                age = self._extract_match_age()
                self._log("info", f"#{index + 1} 资料: age={age}, bio={bio[:50] if bio else 'N/A'}...")

                reply = None
                used_cache = False
                if not effective_messages:
                    self._log("info", f"#{index + 1} 新配对无消息，生成开场白")
                    reply, used_cache = self._generate_or_fetch_reply(match_id, [], bio, age, match_name=match_name)
                    if not reply:
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "opener_no_safe_reply",
                            intent="opener",
                            reason="no_safe_opener",
                            messages=[],
                        )
                        self._log("warning", f"#{index + 1} 无法生成自然开场白，跳过发送")
                        continue
                    success = self.send_reply(reply, messages=[])
                    if success:
                        if used_cache:
                            confirm_sent("tinder", match_id)
                        snapshot_key = self.record_conversation(
                            match_id,
                            match_name or "Unknown",
                            messages,
                            reply,
                            intent="opener",
                        )
                        persisted_snapshot_key = self._persisted_snapshot_key(snapshot_key, context="tinder opener")
                        self._update_incremental_baseline(
                            match_id,
                            match_name or "Unknown",
                            [{"sender": "me", "text": reply, "is_mine": True}],
                            handled_inbound_signature=tuple(),
                            handled_inbound_reason="opened",
                            metadata={"last_snapshot_key": persisted_snapshot_key},
                        )
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "opener_sent",
                            intent="opener",
                            reply=reply,
                            messages=[],
                            metadata={"used_cache": used_cache},
                        )
                        reply_triggered += 1
                        self._log("info", f"#{index + 1} ✓ 开场白发送成功")
                    else:
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "opener_send_failed",
                            intent="opener",
                            reply=reply,
                            messages=[],
                            metadata=get_last_send_diagnostics(self.page),
                        )
                        self._log("warning", f"#{index + 1} ❌ 开场白发送失败")
                    continue

                latest_sender = effective_messages[-1].get("sender", "them")
                self._log(
                    "info",
                    f"#{index + 1} 最后一条: sender={latest_sender} | {effective_messages[-1].get('text', '')[:30]}",
                )

                if not self._is_new_messages(match_id, effective_messages):
                    self._log("info", f"#{index + 1} 无新消息，跳过")
                    continue

                if latest_sender == "them":
                    should_send, reason = self.should_reply(effective_messages)
                    if not should_send:
                        self._update_incremental_baseline(
                            match_id,
                            match_name or "Unknown",
                            effective_messages,
                            handled_inbound_signature=self._inbound_signature(effective_messages),
                            handled_inbound_reason=f"skipped:{reason}",
                        )
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "reply_business_skipped",
                            intent="reply",
                            reason=reason,
                            messages=effective_messages,
                        )
                        self._log("info", f"#{index + 1} 命中业务拦截，跳过回复: {reason}")
                        continue

                    found_unreplied_in_window = True
                    new_scan_limit = min(len(entries), index + 1 + 3)
                    if new_scan_limit > scan_limit:
                        self._log(
                            "info",
                            f"#{index + 1} 命中未回复，检测窗口扩展: {scan_limit} -> {new_scan_limit}",
                        )
                        scan_limit = new_scan_limit
                    reply, used_cache = self._generate_or_fetch_reply(match_id, effective_messages, bio, age)
                    if not reply:
                        self._update_incremental_baseline(
                            match_id,
                            match_name or "Unknown",
                            effective_messages,
                            handled_inbound_signature=self._inbound_signature(effective_messages),
                            handled_inbound_reason="skipped:no_safe_reply",
                        )
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "reply_no_safe_reply",
                            intent="reply",
                            reason="no_safe_reply",
                            messages=effective_messages,
                        )
                        self._log("info", f"#{index + 1} 无安全回复，跳过发送")
                        continue
                    self._log(
                        "info",
                        f"#{index + 1} → {'使用缓存回复' if used_cache else '检测到未回复，立刻回复'}",
                    )
                    success = self.send_reply(reply, messages=effective_messages)
                    if success:
                        if used_cache:
                            confirm_sent("tinder", match_id)
                        snapshot_key = self.record_conversation(
                            match_id,
                            match_name or "Unknown",
                            effective_messages,
                            reply,
                            intent="reply",
                        )
                        persisted_snapshot_key = self._persisted_snapshot_key(snapshot_key, context="tinder reply")
                        self._update_incremental_baseline(
                            match_id,
                            match_name or "Unknown",
                            effective_messages,
                            handled_inbound_signature=self._inbound_signature(effective_messages),
                            handled_inbound_reason="replied",
                            metadata={"last_snapshot_key": persisted_snapshot_key},
                        )
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "reply_sent",
                            intent="reply",
                            reply=reply,
                            messages=effective_messages,
                            metadata={"used_cache": used_cache},
                        )
                        reply_triggered += 1
                        self._log("info", f"#{index + 1} ✓ 回复成功")
                    else:
                        record_runtime_feedback(
                            "tinder",
                            match_id,
                            match_name or "Unknown",
                            "reply_send_failed",
                            intent="reply",
                            reply=reply,
                            messages=effective_messages,
                            metadata=get_last_send_diagnostics(self.page),
                        )
                        self._log("warning", f"#{index + 1} ❌ 回复失败")
                    continue

                should_activate, activation_reason = self._get_dormant_reactivation_candidate(
                    match_id,
                    match_name or "Unknown",
                    effective_messages,
                )
                if should_activate:
                    dormant_candidates.append({
                        "entry": entry,
                        "match_id": match_id,
                        "match_name": match_name or "Unknown",
                        "messages": effective_messages,
                        "bio": bio,
                        "age": age,
                    })
                    self._log("info", f"#{index + 1} 记录沉睡激活候选: {match_name or 'Unknown'}")
                else:
                    self._log("info", f"#{index + 1} 已回复，当前不做激活: {activation_reason}")
                continue

            except Exception as e:
                self._handle_webdriver_exception(e, f"check_all_contacts #{index + 1}")
                self._log("warning", f"#{index + 1} 异常: {e}")
                continue

        if base_window > 0 and not found_unreplied_in_window:
            self._log("info", f"前 {base_window} 个联系人未检测到未回复消息，按规则跳过本轮后续联系人")

        total_sent_so_far = new_matches_sent + reply_triggered
        if base_window > 0 and not found_unreplied_in_window and total_sent_so_far == 0:
            should_run_dormant_round, dormant_round_reason = self._should_run_dormant_reactivation_round()
            if not should_run_dormant_round:
                self._log("info", f"[激活] 跳过沉睡激活轮次: {dormant_round_reason}")
                total_replied = new_matches_sent + reply_triggered
                self._log(
                    "info",
                    f"本轮遍历结束，新配对 {new_matches_sent} 条，消息列表 {reply_triggered} 条，共触发 {total_replied} 次回复",
                )
                return total_replied

            self._mark_dormant_reactivation_round_started()
            extra_entries = self._collect_all_message_cards(
                max_cards=200,
                max_scrolls=DORMANT_REACTIVATION_MESSAGE_LIST_MAX_SCROLLS,
            )
            seen_dormant_keys = {
                candidate["match_id"] or candidate["entry"].get("href", "")
                for candidate in dormant_candidates
            }
            for entry in extra_entries:
                key = entry.get("match_id") or entry.get("href", "")
                if key and key in seen_dormant_keys:
                    continue
                dormant_candidates.append({
                    "entry": entry,
                    "match_id": entry.get("match_id", ""),
                    "match_name": entry.get("name", "Unknown") or "Unknown",
                    "messages": [],
                    "bio": "",
                    "age": 0,
                })
                if key:
                    seen_dormant_keys.add(key)

            if dormant_candidates:
                dormant_candidates, _ = self._rotate_dormant_candidates(dormant_candidates)
                self._log(
                    "info",
                    f"[激活] 本轮无未回复消息，独立扫描 {len(dormant_candidates)} 个沉睡候选"
                    f"（每轮最多 {DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND} 个 / {DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND}s）"
                )

            dormant_round_deadline = time.time() + DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND
            last_attempted_dormant_key = ""
            for candidate_index, candidate in enumerate(dormant_candidates):
                if candidate_index >= DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND:
                    self._log("info", f"[激活] 已达到本轮沉睡激活尝试上限 {DORMANT_REACTIVATION_MAX_ATTEMPTS_PER_ROUND}，结束")
                    break
                if time.time() >= dormant_round_deadline:
                    self._log("info", f"[激活] 已达到本轮沉睡激活时间预算 {DORMANT_REACTIVATION_MAX_SECONDS_PER_ROUND}s，结束")
                    break
                last_attempted_dormant_key = self._dormant_candidate_key(candidate)
                entry = candidate["entry"]
                href = entry.get("href", "")
                absolute_url = href if href.startswith('http') else f'https://tinder.com{href}'
                self._log("info", f"[激活] 尝试沉睡联系人: {candidate['match_name']}")
                try:
                    if not self._open_chat({
                        "href": absolute_url,
                        "name": candidate["match_name"],
                        "match_id": candidate["match_id"],
                    }):
                        self._log("warning", f"[激活] 无法打开沉睡联系人: {candidate['match_name']}")
                        continue

                    messages = sanitize_messages_for_context(
                        self._trim_trailing_fallback_messages(
                            self._append_preview_reaction_event(self._extract_messages(), entry)
                        )
                    )
                    bio = self._extract_profile_bio()
                    age = self._extract_match_age()
                    should_activate, activation_reason = self._get_dormant_reactivation_candidate(
                        candidate["match_id"],
                        candidate["match_name"],
                        messages,
                    )
                    if not should_activate:
                        self._log("info", f"[激活] 候选复核未通过，跳过: {candidate['match_name']} | {activation_reason}")
                        continue

                    activation_reply, used_cache = self._generate_or_fetch_reply(
                        candidate["match_id"],
                        messages,
                        bio,
                        age,
                        match_name=candidate["match_name"],
                        intent="reactivation",
                    )
                    if not activation_reply:
                        self._mark_dormant_reactivation_sent(
                            candidate["match_id"],
                            candidate["match_name"],
                            messages,
                            reason="skipped:no_safe_reactivation",
                        )
                        record_runtime_feedback(
                            "tinder",
                            candidate["match_id"],
                            candidate["match_name"],
                            "reactivation_no_safe_reply",
                            intent="reactivation",
                            reason="no_safe_reactivation",
                            messages=messages,
                        )
                        self._log("info", f"[激活] 无安全激活回复，跳过: {candidate['match_name']}")
                        continue

                    if self.send_reply(activation_reply, messages=messages):
                        if used_cache:
                            confirm_sent("tinder", candidate["match_id"])
                        snapshot_key = self.record_conversation(
                            candidate["match_id"],
                            candidate["match_name"],
                            messages,
                            activation_reply,
                            intent="reactivation",
                        )
                        persisted_snapshot_key = self._persisted_snapshot_key(snapshot_key, context="tinder reactivation")
                        self._mark_dormant_reactivation_sent(candidate["match_id"], candidate["match_name"], messages)
                        self._update_incremental_baseline(
                            candidate["match_id"],
                            candidate["match_name"],
                            messages,
                            metadata={"last_snapshot_key": persisted_snapshot_key},
                        )
                        record_runtime_feedback(
                            "tinder",
                            candidate["match_id"],
                            candidate["match_name"],
                            "reactivation_sent",
                            intent="reactivation",
                            reply=activation_reply,
                            messages=messages,
                            metadata={"used_cache": used_cache},
                        )
                        reply_triggered += 1
                        self._log("info", f"[激活] ✓ 沉睡联系人激活成功: {candidate['match_name']}")
                        break

                    record_runtime_feedback(
                        "tinder",
                        candidate["match_id"],
                        candidate["match_name"],
                        "reactivation_send_failed",
                        intent="reactivation",
                        reply=activation_reply,
                        messages=messages,
                        metadata=get_last_send_diagnostics(self.page),
                    )
                    self._log("warning", f"[激活] ❌ 激活发送失败: {candidate['match_name']}")
                except Exception as e:
                    self._handle_webdriver_exception(e, f"dormant reactivation {candidate.get('match_name', 'Unknown')}")
                    self._log("warning", f"[激活] 沉睡联系人激活异常: {e}")
            self._advance_dormant_scan_cursor(last_attempted_dormant_key)
        elif base_window > 0 and not found_unreplied_in_window and total_sent_so_far > 0:
            self._log(
                "info",
                f"[激活] 本轮已触发 {total_sent_so_far} 次正常发送，跳过沉睡激活，避免叠加打扰"
            )

        total_replied = new_matches_sent + reply_triggered
        self._log(
            "info",
            f"本轮遍历结束，新配对 {new_matches_sent} 条，消息列表 {reply_triggered} 条，共触发 {total_replied} 次回复",
        )
        return total_replied

    def scroll_chat_to_bottom(self):
        """
        滚动聊天到底部
        确保视野在最底部,防止 DOM 卸载或点击拦截
        """
        try:
            chat_selectors = [
                'div[role="log"]',
                '[class*="messageList"]',
                '[class*="messages-container"]',
                '[class*="conversationView"]',
            ]

            for selector in chat_selectors:
                try:
                    container = self.page.locator(selector).last
                    if container.is_visible():
                        container.evaluate("node => node.scrollTop = node.scrollHeight")
                        self.page.wait_for_timeout(500)
                        return True
                except Exception:
                    continue

            # 备选:直接滚动到底部
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_timeout(500)
            return True

        except Exception as e:
            self._log("warning", f"滚动聊天失败: {e}")
            return False

    def _find_message_input_box(self):
        selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"]',
            'textarea',
            '[role="textbox"]',
        ]
        for selector in selectors:
            try:
                box = self.page.locator(selector).first
                if box.is_visible(timeout=3000):
                    self._log("info", f"输入框命中: {selector}")
                    return box
            except Exception:
                continue
        return None

    def send_message(self, text: str, messages: list[dict] | None = None) -> bool:
        """
        发送消息 - 支持多行拆条发送，模拟真人节奏
        将换行符拆分为多条消息依次发送，每条之间有自然停顿
        """
        text = sanitize_reply_for_send(text, max_len=50, messages=messages)
        if not text or not text.strip():
            self._log("warning", "文本为空，取消发送")
            return False

        self.scroll_chat_to_bottom()
        time.sleep(1)

        # ── Step 1: 拆分清洗 ──
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if not lines:
            self._log("warning", "拆分后无有效文本")
            return False

        self._log("info", f"拆分为 {len(lines)} 条发送: {[line[:20] for line in lines]}")

        input_box = self._find_message_input_box()

        if not input_box:
            self._log("error", f"未找到可见输入框，当前页面: {self.page.url}，跳过发送")
            return False

        # ── Step 2: 循环发送每一行 ──
        chat_url = self.page.url if '/messages/' in self.page.url else None
        for i, line in enumerate(lines):
            for attempt in range(3):
                # 发送每条前验证页面仍在聊天页
                if chat_url and '/messages/' not in self.page.url:
                    self._log("warning", f"页面偏离，重新进入: {self.page.url}")
                    self.page.goto(chat_url, timeout=15000)
                    time.sleep(3)
                    chat_url = self.page.url
                try:
                    input_box = self._find_message_input_box()
                    if not input_box:
                        self._log("error", f"输入框消失，当前页面: {self.page.url}")
                        return False
                    self.page.wait_for_timeout(300)
                    input_box.focus()
                    self.page.wait_for_timeout(200)

                    input_box.fill("")
                    self.page.wait_for_timeout(200)

                    delay = random.randint(50, 100)
                    input_box.press_sequentially(line, delay=delay)
                    self.page.wait_for_timeout(300)

                    self.page.keyboard.press("Enter")
                    self.page.wait_for_timeout(1500)

                    # 校验：输入框应已清空
                    try:
                        val_after = input_box.input_value() or input_box.inner_text()
                        if not val_after.strip():
                            self._log("info", f"第 {i+1}/{len(lines)} 条发送成功: {line[:30]}...")
                            break
                    except Exception:
                        pass

                    # 备选：检查气泡是否出现
                    try:
                        bubble = self.page.locator(f'[class*="bubble"]:has-text("{line[:10]}")').count()
                        if bubble > 0:
                            self._log("info", f"第 {i+1}/{len(lines)} 条发送成功(气泡验证): {line[:30]}...")
                            break
                    except Exception:
                        pass

                    self._log("warning", f"第 {i+1} 条重试第 {attempt+2} 次")
                    self.page.wait_for_timeout(800)

                except Exception as e:
                    self._log("warning", f"第 {i+1} 条异常: {e}, 重试第 {attempt+2} 次")
                    self.page.wait_for_timeout(800)
            else:
                self._log("error", f"第 {i+1} 条发送失败（3次重试）")
                return False

        # ── Step 3: 最终校验 ──
        try:
            final_val = input_box.input_value() or input_box.inner_text()
            if final_val.strip():
                self._log("warning", f"输入框尚有残余: {final_val[:50]}")
                input_box.fill("")
                self.page.wait_for_timeout(500)
        except Exception:
            pass

        self._log("info", f"✓ 全部 {len(lines)} 条发送完成")
        return True

    def record_conversation(
        self,
        match_id: str,
        match_name: str,
        messages: list,
        reply: str,
        *,
        intent: str = "reply",
        outcome: float = 0.5,
        outcome_label: str | None = None,
    ) -> str:
        """记录对话结果到语料飞轮，返回 snapshot_key"""
        try:
            _, snapshot_key = self.corpus_store.store(
                match_id,
                match_name,
                messages,
                reply,
                intent=intent,
                outcome=outcome,
                outcome_label=outcome_label,
                platform="tinder",
            )
            self._log("info", f"[语料飞轮] 记录: {match_name} | {reply[:20]}...")
            result_key = snapshot_key
        except Exception as e:
            self._log("warning", f"[语料飞轮] 记录失败: {e}")
            result_key = ""

        # ── 增量写入 pending_corpus.jsonl（进化链路Step1数据注入） ──
        entry = {
            "record_type": "replied_conversation_snapshot",
            "platform": "tinder",
            "match_id": match_id,
            "match_name": match_name,
            "match_index": 0,  # 向下兼容：历史数据无 index，以 match_id 代唯一性
            "messages": messages,
            "reply": reply,
            "intent": intent,
            "outcome": outcome,
            "outcome_label": outcome_label,
            "timestamp": datetime.now().isoformat(),
        }
        if result_key:
            entry["snapshot_key"] = result_key
        try:
            queue_file = Path(__file__).parent.parent / "pending_corpus.jsonl"
            with open(queue_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log("warning", f"pending_corpus 写入失败: {e}")
        return result_key

    def reset_to_list_view(self):
        """
        返回列表视图 - 重置全局状态
        防止下一轮循环的元素选择器被锁定在当前对话上下文
        """
        try:
            # 按 ESC 或点击左侧列表
            try:
                self.page.keyboard.press("Escape")
                self.page.wait_for_timeout(500)
            except Exception:
                pass

            # 点击左侧消息列表区域
            try:
                back_btn = self.page.locator('button:has-text("←"), button[aria-label*="back"], [class*="backButton"]').first
                if back_btn.is_visible():
                    back_btn.click(force=True)
                    self.page.wait_for_timeout(500)
            except Exception:
                pass

            return True
        except Exception as e:
            self._log("warning", f"重置视图失败: {e}")
            return False

    def scroll_to_load_history(self, max_scrolls: int = 5) -> bool:
        """
        滚动到底部加载旧对话
        通过注入 JavaScript 直接操作 DOM 元素的 scrollTop 属性触发懒加载
        """
        try:
            # 查找滚动容器
            scroll_selectors = [
                '[class*="messageList-container"]',
                '[class*="conversationList"]',
                '[class*="messages-container"]',
                '[class*="chatlist"]',
                '[data-testid*="messageList"]',
            ]

            scroll_container = None
            for selector in scroll_selectors:
                try:
                    container = self.page.locator(selector).first
                    if container.is_visible():
                        scroll_container = container
                        self._log("info", f"找到滚动容器: {selector}")
                        break
                except Exception:
                    continue

            if not scroll_container:
                self._log("warning", "未找到滚动容器，尝试全页滚动")
                for i in range(max_scrolls):
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    self.page.wait_for_timeout(1500)
                return True

            # 执行滚动加载
            for i in range(max_scrolls):
                scroll_container.evaluate("node => node.scrollTop = node.scrollHeight")
                self.page.wait_for_timeout(1500)
                self._log("info", f"滚动加载第 {i+1}/{max_scrolls}")

            self._log("info", f"滚动加载完成，共 {max_scrolls} 次")
            return True

        except Exception as e:
            self._log("warning", f"滚动加载失败: {e}")
            return False

    def check_wechat_request(self, messages: list) -> bool:
        """
        检查是否已留微信
        使用正则表达式匹配历史消息文本
        """
        import re

        # 匹配常见变体
        pattern = re.compile(r'(微信|wechat|vx|v号|微信号|加我|扫一下|留个)', re.IGNORECASE)

        for msg in messages:
            text = msg.get('text', '')
            if pattern.search(text):
                self._log("info", f"发现微信/联系方式关键词: {text[:30]}")
                return True

        return False

    def _is_new_messages(self, match_id: str, messages: list) -> bool:
        """
        增量检测：比对 history_baseline.json 中该 match_id 的最近消息。
        仅比较“最后一句文本”会误伤像“？”这种重复追问，因此这里改为：
        1. 先比较历史消息条数
        2. 再比较最近几条 sender/text 签名
        3. 最后才回退到末条文本比较
        额外规则：
        - 如果当前对话实际仍然是“对方最后发言、我方尚未接话”的悬空状态，
          即便 baseline 已经包含这条消息，也要放行一次，避免全量抓取后把现存未回复消息永久压掉。
        """
        messages = sanitize_messages_for_context(self._trim_trailing_fallback_messages(messages))
        if not messages:
            return False

        latest_text = messages[-1].get('text', '').strip()
        if not latest_text:
            return False

        try:
            latest_sender = messages[-1].get("sender", "")
            last_recorded_text = None
            _, keyed = self._load_incremental_baseline()
            prev_entry = keyed.get(self._conversation_key(match_id, "", 0))
            if prev_entry is None:
                return True
            prev_messages = sanitize_messages_for_context(
                self._trim_trailing_fallback_messages((prev_entry or {}).get('messages', []))
            )
            if prev_messages:
                last_recorded_text = prev_messages[-1].get('text', '').strip()

            current_inbound = self._inbound_signature(messages)
            recorded_inbound = self._inbound_signature(prev_messages)
            handled_inbound = self._restore_inbound_signature((prev_entry or {}).get("last_handled_inbound_signature"))
            handled_reason = str((prev_entry or {}).get("last_handled_inbound_reason", "") or "")
            handled_text = " ".join(str((prev_entry or {}).get("last_handled_inbound_text", "") or "").split())
            if not handled_text and handled_inbound:
                handled_text = handled_inbound[-1]
            handled_at = self._parse_baseline_timestamp((prev_entry or {}).get("last_handled_inbound_at", ""))

            if (
                latest_sender == "them"
                and handled_reason.startswith("skipped:")
                and handled_text
                and latest_text == handled_text
            ):
                if not handled_reason.startswith("skipped:no_safe"):
                    should_send_now, updated_reason = self.should_reply(messages)
                    if should_send_now:
                        self._log(
                            "info",
                            f"旧跳过规则已失效，重新放行该入站: {latest_text[:30]}...",
                        )
                        return True
                    self._log(
                        "info",
                        f"同一条旧跳过入站复核后仍不应回复: {updated_reason}",
                    )
                if handled_at is None:
                    self._log(
                        "info",
                        f"同一条跳过入站缺少时间戳，按已处理跳过重试: {latest_text[:30]}...",
                    )
                    return False
                if (datetime.now() - handled_at) < timedelta(hours=SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS):
                    remaining = timedelta(hours=SKIPPED_INBOUND_RETRY_COOLDOWN_HOURS) - (datetime.now() - handled_at)
                    remaining_hours = max(int(remaining.total_seconds() // 3600), 0)
                    self._log(
                        "info",
                        f"同一条跳过入站仍在冷却中（{handled_reason}），约剩 {remaining_hours}h，跳过重试: {latest_text[:30]}...",
                    )
                    return False

            if len(current_inbound) > len(recorded_inbound):
                self._record_partner_followup_if_needed(
                    match_id,
                    str((prev_entry or {}).get("match_name", "") or ""),
                    messages,
                    prev_entry,
                )
                self._log("info", f"检测到入站消息条数增加: {len(recorded_inbound)} -> {len(current_inbound)}")
                return True

            if handled_inbound and current_inbound == handled_inbound:
                self._log(
                    "info",
                    f"最近入站已处理，跳过重复放行: {handled_reason or (current_inbound[-1] if current_inbound else 'empty')}",
                )
                return False

            def _tail_signature(items: list, size: int = 3) -> tuple:
                tail = items[-size:]
                normalized = []
                for item in tail:
                    sender = item.get('sender', '')
                    text = ' '.join((item.get('text', '') or '').split())
                    cursor = (
                        item.get("message_key")
                        or item.get("timestamp")
                        or item.get("datetime")
                        or item.get("id")
                        or text
                    )
                    normalized.append((sender, str(cursor), text))
                return tuple(normalized)

            current_sig = _tail_signature(messages)
            recorded_sig = _tail_signature(prev_messages)
            if current_sig == recorded_sig:
                if latest_sender == "them":
                    self._log("info", f"最近消息签名未变，但当前仍是未回复入站消息，放行: {current_sig[-1] if current_sig else 'empty'}")
                    return True
                self._log("info", f"最近消息签名未变（已回复过），跳过: {current_sig[-1] if current_sig else 'empty'}")
                return False

            if latest_sender != "them" and current_inbound == recorded_inbound:
                self._log("info", "仅我方消息/历史污染发生变化，未检测到新的入站消息")
                return False

            if latest_text == last_recorded_text:
                if latest_sender == "them":
                    self._log("info", f"最新文本未变，但当前仍是未回复入站消息，放行: {latest_text[:30]}...")
                    return True
                self._log("info", f"最新消息未变（已回复过），跳过: {latest_text[:30]}...")
                return False

            return True
        except Exception as e:
            self._log("warning", f"增量检测异常: {e}，默认放行")
            return True

    def _trim_trailing_fallback_messages(self, messages: list) -> list:
        """尾部若是统一兜底回复，则视为未完成回复并从有效上下文中裁掉。"""
        effective = list(messages or [])
        trimmed = 0
        while effective:
            last = effective[-1] or {}
            if last.get("sender") != "me":
                break
            if not is_fallback_reply(last.get("text", "")):
                break
            effective.pop()
            trimmed += 1
        if trimmed:
            self._log("warning", f"检测到尾部兜底回复 {trimmed} 条，按未回复会话继续处理")
        return effective

    def should_reply(self, messages: list) -> tuple:
        """平台包装：实际业务判断已统一到 shared reply engine。"""
        return should_reply_to_messages(messages)

    def _extract_profile_bio(self) -> str:
        """
        从右侧 ProfileCard 提取对方资料。
        严格限定在 aside 或右侧面板范围内，绝不查询聊天区域。
        提取后硬清洗已知己方资料片段，防止泄露进 LLM 上下文。
        """
        dom_rules = _load_dom_rule_section("tinder_profile")
        noise_fragments = [
            str(item).strip()
            for item in dom_rules.get("noise_fragments", [])
            if str(item).strip()
        ]
        own_profile_fragments = [
            str(item).strip()
            for item in dom_rules.get("own_profile_fragments", [])
            if str(item).strip()
        ]
        selectors = [
            str(item).strip()
            for item in dom_rules.get("selectors", [])
            if str(item).strip()
        ]

        bio = self.page.evaluate(r"""
            (rules) => {
                const noiseFragments = rules.noiseFragments || [];
                const selectors = rules.selectors || [];

                const extractText = (root) => {
                    if (!root) return '';
                    const walker = document.createTreeWalker(
                        root,
                        NodeFilter.SHOW_TEXT,
                        {
                            acceptNode: (node) => {
                                const text = node.nodeValue.trim();
                                if (!text || text.length < 2) return NodeFilter.FILTER_REJECT;
                                if (/^\d{1,2}:\d{2}$/.test(text)) return NodeFilter.FILTER_REJECT;
                                const parent = node.parentElement;
                                if (!parent) return NodeFilter.FILTER_REJECT;
                                const tag = parent.tagName.toLowerCase();
                                if (['button', 'a', 'script', 'style'].includes(tag)) {
                                    return NodeFilter.FILTER_REJECT;
                                }
                                return NodeFilter.FILTER_ACCEPT;
                            }
                        }
                    );
                    const texts = [];
                    let node;
                    while ((node = walker.nextNode())) texts.push(node.nodeValue.trim());
                    return texts.join(' | ').replace(/\s+/g, ' ').trim();
                };

                const isNoisy = (text) => {
                    if (!text) return true;
                    const compact = text.replace(/\s+/g, ' ').trim();
                    if (!compact || compact.length < 6) return true;
                    return noiseFragments.some((fragment) => {
                        const item = String(fragment || '').trim();
                        if (!item) return false;
                        const compactLower = compact.toLowerCase();
                        const itemLower = item.toLowerCase();
                        if (itemLower === 'messages' || item === '消息') {
                            return compactLower === itemLower;
                        }
                        return compactLower.includes(itemLower);
                    });
                };

                for (const sel of selectors) {
                    const nodes = Array.from(document.querySelectorAll(sel));
                    for (const node of nodes) {
                        const text = extractText(node);
                        if (!isNoisy(text)) return text;
                    }
                }

                const aside = document.querySelector('aside, [role="complementary"]');
                if (!aside) return '';
                const asideText = extractText(aside);
                if (isNoisy(asideText)) return '';
                return asideText;
            }
        """, {"selectors": selectors, "noiseFragments": noise_fragments})

        for frag in noise_fragments:
            bio = bio.replace(frag, "")
        for frag in own_profile_fragments:
            bio = bio.replace(frag, '')
        bio = ' | '.join([s.strip() for s in bio.split('|') if s.strip()])
        return bio

    def _extract_match_age(self) -> int:
        """
        从 Tinder 聊天页右侧 Profile 卡提取对方年龄。
        Tinder 聊天页的 aside 是"匹配列表"侧边栏，Profile 卡在更深层结构中。
        如果检测到 aside 包含匹配列表特征（多个名字/LIKES YOU/last message），
        说明拿到的是列表侧边栏而非 Profile 卡，返回 0 让 LLM 不引用年龄。
        """
        try:
            result = self.page.evaluate(r"""
                () => {
                    // Tinder 聊天页的 aside 是匹配列表，不是 Profile 卡
                    // Profile 卡通常在更内层的结构里，尝试多种定位方式
                    
                    // 策略1: 查找聊天主面板旁边的 Profile 悬浮卡（不是全局 aside）
                    // Tinder 在聊天页右侧会有一个小面积的 profile 展示区
                    const selectors = [
                        // 聊天容器内/旁的 profile 相关区域
                        '[class*="profileCard"]',
                        '[class*="profile-card"]',
                        '[class*="matchProfile"]',
                        '[data-testid*="profile"]',
                        // 可能是 header 区域的名字+年龄
                        'header [class*="name"]',
                        // 精确定位到 aside 但验证不是 match list
                    ];
                    
                    let profileArea = null;
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) { profileArea = el; break; }
                    }
                    
                    // 如果找到了专门的 profile 区域，从中提取年龄
                    if (profileArea) {
                        const text = profileArea.innerText;
                        // 匹配 "Name, 26" 或 "Name 26岁"
                        const nameAgeMatch = text.match(/,\s*(\d{1,2})\s*(?:岁|years?)?/i);
                        if (nameAgeMatch) {
                            const age = parseInt(nameAgeMatch[1], 10);
                            if (age >= 18 && age <= 50) return age;
                        }
                    }
                    
                    // 策略2: 检查全局 aside 是否为 match list
                    // 如果 aside 包含 LIKES YOU 或多个 "last message" 模式，说明是列表
                    const aside = document.querySelector('aside');
                    if (aside) {
                        const asideText = aside.innerText;
                        const isMatchList = (
                            (asideText.match(/LIKES YOU/g) || []).length > 1 ||
                            (asideText.match(/last message was:/g) || []).length > 1 ||
                            (asideText.match(/'s last message was:/g) || []).length > 1
                        );
                        if (isMatchList) {
                            // 这是 match list aside，不是 Profile 卡
                            return -1;  // 特殊标记：无效 aside
                        }
                        
                        // 不在 match list 中，尝试提取
                        // 找 "Name, Age" 格式（逗号后面是年龄）
                        const commaAge = asideText.match(/,[^,]*?\b(\d{2})\b/);
                        if (commaAge) {
                            const age = parseInt(commaAge[1], 10);
                            if (age >= 18 && age <= 50) return age;
                        }
                    }
                    
                    // 策略3: 直接在页面顶部 header 区域查找（Name + Age）
                    const headerAge = document.querySelector('header');
                    if (headerAge) {
                        const headerText = headerAge.innerText;
                        const hMatch = headerText.match(/,\s*(\d{1,2})\s*(?:岁|years?)?/i);
                        if (hMatch) {
                            const age = parseInt(hMatch[1], 10);
                            if (age >= 18 && age <= 50) return age;
                        }
                    }
                    
                    return 0;
                }
            """)
            age_val = int(result) if result else 0
            if age_val == -1:
                return 0  # 确认是 match list aside，放弃提取
            return age_val
        except Exception as e:
            self._log("warning", f"年龄提取失败: {e}")
            return 0

    def send_reply(self, message: str, messages: list[dict] | None = None) -> bool:
        """统一走 shared_assets.unified_send_message，避免平台实现漂移"""
        message = sanitize_reply_for_send(message, max_len=50, messages=messages)
        if not message:
            self._log("warning", "待发送文本为空，取消发送")
            return False
        return send_message_unified(
            page=self.page,
            message=message,
            platform="tinder",
            message_context=messages,
        )

    def process_unread_messages(self):
        """
        处理未读消息。
        直接委托 check_all_contacts() 完成全流程：
        导航→收集卡片→逐个判断→生成回复→发送→记录。
        check_all_contacts 内部已包含 per-item 的 should_reply 判断和发送逻辑。
        """
        self._ensure_proxy_sticky()
        try:
            return self.check_all_contacts()
        except TinderBackendError:
            raise
        except Exception as e:
            self._log("error", f"process_unread_messages 异常: {e}")
            return 0

    def swipe_card(self, direction: str = "left"):
        """滑动卡片"""
        try:
            if direction == "left":
                self.swiper.swipe_left()
            elif direction == "right":
                self.swiper.swipe_right()
            elif direction == "super":
                self.swiper.swipe_up()

            self._log("info", f"已滑动: {direction}")
            self.guard.record_action("swipe")

        except Exception as e:
            self._log("warning", f"滑动失败: {e}")

    def run_session(self):
        """运行一个会话"""
        try:
            self.setup()

            action_count = 0
            max_actions = self.config.get("max_session_actions", 20)

            while action_count < max_actions:
                can_continue, reason = self.guard.can_proceed()
                if not can_continue:
                    self._log("warning", f"会话中断: {reason}")
                    break

                processed = self.process_unread_messages()
                action_count += processed

                if self.config.get("auto_like"):
                    self.swipe_card("right")
                    action_count += 1

                if action_count >= 15:
                    burst_pause = random.randint(5, 20)
                    self._log("info", f"达到操作阈值，暂停 {burst_pause} 分钟")
                    time.sleep(burst_pause * 60)

            self._log("info", f"会话结束，共 {action_count} 个操作")

        except KeyboardInterrupt:
            self._log("warning", "用户中断")
        except Exception as e:
            self._log("error", f"会话异常: {e}")
        finally:
            self.guard.end_session()

    def cleanup(self):
        """清理资源（仅关闭 guard，不关闭浏览器）"""
        if hasattr(self, 'guard'):
            self.guard.end_session()


def run_cli():
    """命令行入口"""
    print("=== Tinder 自动化 Bot v2 ===")
    print(f"目标: {'自动右滑' if CONFIG.get('auto_like') else '仅自动回复'}")
    print(f"地区: {CONFIG.get('country', 'JP')}")
    print(f"LLM: {CONFIG.get('llm_model', 'MiniMax-M2.7')}")
    print()

    bot = TinderBot(CONFIG)

    try:
        bot.run_session()
    except KeyboardInterrupt:
        print("\n[Bot] 已停止")
    finally:
        bot.cleanup()


if __name__ == "__main__":
    run_cli()
