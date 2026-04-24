#!/usr/bin/env python3
"""
unified_orchestrator.py
=========================
交替协作式双核守护进程（Time-Slicing Dual-Core Daemon）

状态机
------
TINDER_ACTIVE  → 巡检 Tinder，30 分钟无回复则降级
BUMBLE_ACTIVE  → 巡检 Bumble，30 分钟无回复则降级
EVOLUTION      → 凌晨 00:00-08:00 执行演化流水线

切换逻辑
--------
Tinder 降级 : 连续 2 轮（每轮 ≈cooldown 分钟）无新回复
             且距上次 Tinder 回复 > 30 分钟
Bumble 降级 : 同理
紧急切换   : 某平台出现严重错误（网络/锁失败）时立即切换

宵禁拦截
--------
00:00 - 08:00 期间：
  1. 终止所有 Chat Worker
  2. 执行 unified_evolution_pipeline（凌晨演化）
  3. 主进程挂起至 08:00 唤醒

依赖文件（同一 shared_assets 目录）
--------------------------------------
core/unified_reply_engine.py   — 统一回复引擎
core/tinder_bot.py              — Tinder Bot
core/bumble_bot.py              — Bumble Bot（v5）
strategy_config.json             — 共享策略
../bumble-automation/...        — Bumble 独立模块

用法
----
python3 unified_orchestrator.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import random
import logging
import importlib
import signal
import threading
import json
import urllib.request
import re
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ─────────────────────────────────────────────────────────────────
# 路径
# ─────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).parent          # shared_assets/
TINDER_DIR      = SCRIPT_DIR.parent / "tinder-automation"
BUMBLE_DIR      = SCRIPT_DIR.parent / "bumble-automation"
SHARED_CFG      = SCRIPT_DIR / "strategy_config.json"

sys.path.insert(0, str(SCRIPT_DIR))         # shared_assets/ → unified_reply_engine.py
sys.path.insert(1, str(TINDER_DIR))        # tinder-automation/ → core.* (TinderBot)
sys.path.insert(2, str(BUMBLE_DIR))        # bumble-automation/  → core.* (BumbleBot)

from logging.handlers import RotatingFileHandler
LOG_FILE = SCRIPT_DIR / "orchestrator.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=52428800, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("Orchestrator")

from config import get_config
from unified_reply_engine import (
    generate_reply, click_contact, wait_for_chat_ready,
    back_to_list, is_curfew, next_wake_time, load_strategy,
)

config = get_config()
TINDER_PROFILE  = config.tinder.profile_dir
BUMBLE_PROFILE  = config.bumble.profile_dir


def _read_shell_export(name: str) -> str | None:
    zshrc = Path.home() / ".zshrc"
    if not zshrc.exists():
        return None
    pattern = re.compile(rf'^\s*export\s+{re.escape(name)}=(["\']?)(.+?)\1\s*$')
    try:
        for line in zshrc.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match:
                return match.group(2)
    except Exception:
        return None
    return None


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or _read_shell_export("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TINDER_TELEGRAM_CHAT_ID")
    or ""
)


def _send_telegram_summary(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(f"[Notify] Telegram 未配置，跳过结果摘要: {message}")
        return

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("[Notify] 已发送 Telegram 结果摘要")
    except Exception as exc:
        log.warning(f"[Notify] Telegram 结果摘要发送失败: {exc}")


def _cycle_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _notify_run_result(title: str, details: list[str]) -> None:
    message = "\n".join([f"🤖 {title}", f"⏰ {_cycle_stamp()}", *details])
    _send_telegram_summary(message)


def _purge_core_modules() -> None:
    """清理已加载的 core 包，避免 Tinder/Bumble 同名包相互污染。"""
    for name in list(sys.modules):
        if name == "core" or name.startswith("core."):
            sys.modules.pop(name, None)


def _import_tinder_bot():
    """按需加载 Tinder core 包，避免与 Bumble 的 core 包冲突。"""
    _purge_core_modules()
    if str(TINDER_DIR) in sys.path:
        sys.path.remove(str(TINDER_DIR))
    sys.path.insert(0, str(TINDER_DIR))
    module = importlib.import_module("core.tinder_bot")
    return module.TinderBot, module.TinderBackendError


def _import_bumble_inspect():
    """按需加载 Bumble inspect 模块，避免与 Tinder 的 core 包冲突。"""
    _purge_core_modules()
    if str(BUMBLE_DIR) in sys.path:
        sys.path.remove(str(BUMBLE_DIR))
    if str(SCRIPT_DIR) in sys.path:
        sys.path.remove(str(SCRIPT_DIR))
    sys.path.insert(0, str(BUMBLE_DIR))
    sys.path.insert(0, str(SCRIPT_DIR))
    module = importlib.import_module("bumble_inspect")
    return module.run_inspect

# ─────────────────────────────────────────────────────────────────
# 状态机
# ─────────────────────────────────────────────────────────────────
class State(Enum):
    TINDER_ACTIVE = "tinder_active"
    BUMBLE_ACTIVE = "bumble_active"
    EVOLUTION     = "evolution"


@dataclass
class OrchestratorState:
    state            : State = State.TINDER_ACTIVE
    tinder_last_reply: float = 0.0   # unix timestamp
    bumble_last_reply : float = 0.0
    tinder_idle_minutes : float = 0.0
    bumble_idle_minutes : float = 0.0
    cooldown          : int   = 0     # 静默冷却分钟数；回复后的 1 分钟休眠不计入递增基线
    max_cooldown      : int   = 60
    last_tinder_reply_count: int = 0
    last_bumble_reply_count: int = 0
    consecutive_no_reply : int = 0    # 连续无回复轮次

    # 配置
    downgrade_minutes : int   = 30   # 超过 N 分钟无回复则切换
    consecutive_threshold: int = 2   # 连续 N 轮无回复触发降级

    def record_tinder_reply(self, count: int):
        self.tinder_last_reply = time.time()
        self.last_tinder_reply_count = count
        if count > 0:
            self.consecutive_no_reply = 0
            self.cooldown = 0
        else:
            self.consecutive_no_reply += 1

    def record_bumble_reply(self, count: int):
        self.bumble_last_reply = time.time()
        self.last_bumble_reply_count = count
        if count > 0:
            self.consecutive_no_reply = 0
            self.cooldown = 0
        else:
            self.consecutive_no_reply += 1

    def should_downgrade_tinder(self) -> bool:
        if self.consecutive_no_reply < self.consecutive_threshold:
            return False
        idle = (time.time() - self.tinder_last_reply) / 60
        return idle >= self.downgrade_minutes

    def should_downgrade_bumble(self) -> bool:
        if self.consecutive_no_reply < self.consecutive_threshold:
            return False
        idle = (time.time() - self.bumble_last_reply) / 60
        return idle >= self.downgrade_minutes

    def advance_cooldown(self):
        self.cooldown = min(self.cooldown + 5, self.max_cooldown)


# ─────────────────────────────────────────────────────────────────
# Tinder 巡检
# ─────────────────────────────────────────────────────────────────
def inspect_tinder(_state=None) -> int:
    """
    巡检 Tinder，返回本轮回复数。直接在主进程调用，复用 BrowserManager 单例。
    """
    from project_config import build_tinder_config

    bot = None
    TinderBot, TinderBackendError = _import_tinder_bot()
    try:
        config = build_tinder_config()
        bot = TinderBot(config)
        bot.setup()
        reply_count = bot.check_all_contacts()
        if reply_count > 0:
            log.info(f"[Tinder] 本轮回复 {reply_count} 条")
        else:
            log.info("[Tinder] 本轮无新消息")
        return reply_count
    except TinderBackendError as e:
        log.warning(f"[Tinder] ⚠️ 后端异常: {e}")
        return -1  # 触发 30 分钟退避
    except Exception as e:
        log.error(f"[Tinder] 巡检异常: {e}")
        return 0
    finally:
        if bot is not None:
            bot.cleanup()


# ─────────────────────────────────────────────────────────────────
# Bumble 巡检
# ─────────────────────────────────────────────────────────────────
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


def inspect_bumble(_state=None) -> int:
    """
    巡检 Bumble，返回本轮回复数。直接在主进程调用，复用 BrowserManager 单例。
    """
    run_inspect = _import_bumble_inspect()

    try:
        reply_count = run_inspect()
        if reply_count > 0:
            log.info(f"[Bumble] 本轮回复 {reply_count} 条")
        elif reply_count == 0:
            log.info("[Bumble] 本轮无新消息")
        return reply_count
    except Exception as e:
        log.error(f"[Bumble] 巡检异常: {e}")
        return -1  # 触发 30 分钟退避


# ─────────────────────────────────────────────────────────────────
# 演化流水线（宵禁期间）
# ─────────────────────────────────────────────────────────────────
def run_evolution_pipeline() -> tuple[bool, str]:
    """
    执行深夜演化流水线（统一增量模式）。
    统一走 unified_evolution.py，避免绕开自动审核/同步/NotebookLM 链路，
    也避免只更新 tinder-automation/strategy_config.json 而共享策略不落盘。
    """
    import subprocess

    cmd = [sys.executable, str(SCRIPT_DIR / "unified_evolution.py")]
    try:
        log.info("[Evolution] 开始: Unified Evolution Pipeline")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode == 0:
            tail = (result.stdout or "").strip()[-400:]
            log.info(f"[Evolution] ✅ Unified Evolution Pipeline{': ' + tail if tail else ''}")
            return True, tail or "成功"
        else:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            log.error(f"[Evolution] ❌ Unified Evolution Pipeline: {detail}")
            return False, detail or "失败"
    except subprocess.TimeoutExpired:
        log.error("[Evolution] ⏱ Unified Evolution Pipeline 超时")
        return False, "超时"
    except Exception as e:
        log.error(f"[Evolution] ❌ Unified Evolution Pipeline: {e}")
        return False, str(e)


# ─────────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────────
def run_loop():
    """双核交替调度状态机"""
    # 初始化 TinderBot（预热，加载配置）
    try:
        import sys as _sys
        _sys.path.insert(0, str(TINDER_DIR))
        from core.tinder_bot import TinderBot, CONFIG
        _ = CONFIG
        log.info("[Init] Tinder CONFIG 加载成功")
    except Exception as e:
        log.error(f"[Init] Tinder CONFIG 加载失败: {e}")

    try:
        strategy = load_strategy()
        log.info(f"[Init] 策略加载成功 v{strategy.get('version', '?')}")
    except Exception as e:
        log.warning(f"[Init] 策略加载警告: {e}")

    log.info("=" * 50)
    log.info("Unified 双核守护进程启动")
    log.info(f"  共享策略: {SHARED_CFG}")
    log.info("=" * 50)

    current_platform = 'TINDER'
    cooldown = 0
    MAX_COOLDOWN = 60
    BACKEND_BACKOFF_SECONDS = 30 * 60
    backend_backoff_until = {
        'TINDER': 0.0,
        'BUMBLE': 0.0,
    }

    def _other_platform(platform: str) -> str:
        return 'BUMBLE' if platform == 'TINDER' else 'TINDER'

    def _backoff_remaining(platform: str) -> int:
        return max(0, int(backend_backoff_until.get(platform, 0.0) - time.time()))

    def _platform_ready(platform: str) -> bool:
        return _backoff_remaining(platform) <= 0

    def _sleep_until_backend_recovers() -> None:
        remaining = {
            name: seconds for name, seconds in (
                ('TINDER', _backoff_remaining('TINDER')),
                ('BUMBLE', _backoff_remaining('BUMBLE')),
            ) if seconds > 0
        }
        if not remaining:
            return
        wake_platform = min(remaining, key=remaining.get)
        sleep_secs = remaining[wake_platform]
        log.warning(
            f"[Orchestrator] 双平台均处于后端冷却，挂起 {sleep_secs} 秒，优先等待 {wake_platform} 恢复"
        )
        _notify_run_result(
            "本轮运行结果",
            [
                "平台: Tinder + Bumble",
                "结果: 双平台后端异常冷却中",
                f"后续: 挂起 {sleep_secs // 60 if sleep_secs >= 60 else sleep_secs} {'分钟' if sleep_secs >= 60 else '秒'}，等待 {wake_platform} 恢复",
            ],
        )
        time.sleep(max(1, sleep_secs))

    def _enter_backend_backoff(platform: str) -> None:
        backend_backoff_until[platform] = time.time() + BACKEND_BACKOFF_SECONDS
        remaining_min = max(1, BACKEND_BACKOFF_SECONDS // 60)
        other = _other_platform(platform)
        if _platform_ready(other):
            log.warning(f" [{platform}] 后端异常，冷却 {remaining_min} 分钟，立即切换至 {other}")
            _notify_run_result(
                "本轮运行结果",
                [
                    f"平台: {platform}",
                    "结果: 后端异常",
                    f"后续: 冷却 {remaining_min} 分钟，并立即切换至 {other}",
                ],
            )
        else:
            other_remaining = _backoff_remaining(other)
            log.warning(
                f" [{platform}] 后端异常，冷却 {remaining_min} 分钟；{other} 也在冷却，等待最近平台恢复"
            )
            _notify_run_result(
                "本轮运行结果",
                [
                    f"平台: {platform}",
                    "结果: 后端异常",
                    f"后续: 冷却 {remaining_min} 分钟；{other} 剩余冷却 {max(1, other_remaining // 60) if other_remaining >= 60 else other_remaining} {'分钟' if other_remaining >= 60 else '秒'}",
                ],
            )

    while True:
        # ── 0. 宵禁检查 ──────────────────────────────────────
        if is_curfew():
            log.info("[Curfew] 03:00-08:00 深夜演化时段，回复任务挂起...")
            ok, detail = run_evolution_pipeline()
            _notify_run_result(
                "深夜演化结果",
                [
                    f"状态: {'成功' if ok else '失败'}",
                    f"摘要: {detail[:280] if detail else 'N/A'}",
                ],
            )
            if is_curfew():
                sleep_secs = next_wake_time()
                log.info(f"[Curfew] 演化完成，挂起至 08:00 ({sleep_secs:.0f}s)")
                time.sleep(max(sleep_secs, 300))
            else:
                log.info("[Curfew] 演化完成，已离开宵禁窗口，立即恢复巡检")
            current_platform = 'TINDER'
            cooldown = 0
            continue

        if not _platform_ready('TINDER') and not _platform_ready('BUMBLE'):
            _sleep_until_backend_recovers()
            current_platform = 'TINDER' if _backoff_remaining('TINDER') <= _backoff_remaining('BUMBLE') else 'BUMBLE'
            continue

        if not _platform_ready(current_platform):
            other = _other_platform(current_platform)
            if _platform_ready(other):
                remaining = _backoff_remaining(current_platform)
                log.info(f"[{current_platform}] 后端冷却剩余 {remaining}s，先切换至 {other}")
                current_platform = other
                continue

        # ── 1. Tinder 分支 ─────────────────────────────────
        if current_platform == 'TINDER':
            log.info("[Tinder] 扫描未回复消息...")
            replied_count = inspect_tinder(None)

            if replied_count > 0:
                cooldown = 0
                log.info(f" [Tinder] 本轮回复 {replied_count} 条，休眠 1 分钟...")
                _notify_run_result(
                    "本轮运行结果",
                    [
                        "平台: Tinder",
                        f"结果: 回复 {replied_count} 条",
                        "后续: 休眠 1 分钟后继续",
                    ],
                )
                time.sleep(60)
                continue
            elif replied_count == -1:
                _enter_backend_backoff('TINDER')
                current_platform = 'BUMBLE'
                continue
            else:
                log.info(" [Tinder] 无新消息，立即切换至 Bumble")
                current_platform = 'BUMBLE'
                continue

        # ── 2. Bumble 分支 ─────────────────────────────────
        if current_platform == 'BUMBLE':
            log.info("[Bumble] 扫描未回复消息...")
            replied_count = inspect_bumble(None)

            if replied_count > 0:
                cooldown = 0
                log.info(f" [Bumble] 本轮回复 {replied_count} 条，休眠 1 分钟...")
                _notify_run_result(
                    "本轮运行结果",
                    [
                        "平台: Bumble",
                        "前序: Tinder 无新消息",
                        f"结果: 回复 {replied_count} 条",
                        "后续: 休眠 1 分钟后继续",
                    ],
                )
                time.sleep(60)
                continue
            elif replied_count == -1:
                _enter_backend_backoff('BUMBLE')
                current_platform = 'TINDER'
                continue
            else:
                cooldown = cooldown + 5 if cooldown > 0 else 5
                cooldown = min(cooldown, MAX_COOLDOWN)
                log.info(f" [Orchestrator] 双平台静默，进入冷却状态 {cooldown} 分钟...")
                # 设计选择：双平台静默摘要不发 Telegram，也不在进程重启后补发；
                # Telegram 只用于回复成功、后端异常、演化结果等需要人工关注的事件。
                log.info(" [Notify] 双平台无新消息，跳过 Telegram 静默摘要")
                time.sleep(cooldown * 60)
                current_platform = 'TINDER'
                continue


# ─────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def _sig_handler(sig, frame):
        log.info(f"收到信号 {sig}，退出")
        sys.exit(0)
    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    run_loop()
