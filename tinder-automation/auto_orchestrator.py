#!/usr/bin/env python3
"""
auto_orchestrator.py
全局调度器：常驻聊天 + 定时语料进化流水线
双进程架构：聊天Daemon + 定时演化Worker
"""
import sys
import os
import time
import json
import signal
import logging
import subprocess
import threading
from pathlib import Path
from datetime import datetime, time as dtime
from multiprocessing import Process, Queue
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except Exception:
    BackgroundScheduler = None
    CronTrigger = None

# ── 路径 ──────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
SHARED_ASSETS_DIR = SCRIPT_DIR.parent / "shared_assets"
PROFILE_PATH = str(Path.home() / ".tinder-automation" / "browser-profile")
LOCK_FILE = SCRIPT_DIR / ".tinder_daemon.lock"
STATE_FILE = SCRIPT_DIR / "bot_watchdog.json"
ERROR_LOG = SCRIPT_DIR / "orchestrator_error.log"
STRATEGY_FILE = SHARED_ASSETS_DIR / "strategy_config.json"

# ── 日志 ─────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            ERROR_LOG,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5,
            encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("Orchestrator")

# ── 全局退出 ─────────────────────────────────────────────────
RUNNING = True


def handle_signal(signum, frame):
    global RUNNING
    log.warning("收到退出信号，正在关闭...")
    RUNNING = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ── 工具函数 ─────────────────────────────────────────────────

def run_subprocess(script_name: str, args: list = None, retries: int = 2,
                   timeout: int = 600) -> bool:
    """带超时+重试的子流程调用"""
    cmd = [sys.executable, str(SCRIPT_DIR / script_name)] + (args or [])
    for attempt in range(retries + 1):
        try:
            log.info(f"[{script_name}] 第{attempt+1}次尝试...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                log.info(f"[{script_name}] 成功")
                if result.stdout.strip():
                    log.debug(f"  stdout: {result.stdout.strip()[-200:]}")
                return True
            else:
                log.error(f"[{script_name}] 失败 (rc={result.returncode}): {result.stderr[-300:]}")
        except subprocess.TimeoutExpired:
            log.error(f"[{script_name}] 超时 ({timeout}s)")
        except Exception as e:
            log.error(f"[{script_name}] 异常: {e}")
        if attempt < retries:
            time.sleep(30)
    return False


def get_last_strategy_mtime() -> float:
    if STRATEGY_FILE.exists():
        return STRATEGY_FILE.stat().st_mtime
    return 0.0


def save_watchdog_state(last_mtime: float, last_evolution: str):
    STATE_FILE.write_text(
        json.dumps({"last_strategy_mtime": last_mtime,
                     "last_evolution": last_evolution}, ensure_ascii=False),
        encoding="utf-8"
    )


def load_watchdog_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_strategy_mtime": 0.0, "last_evolution": ""}


# ── 演化流水线 ───────────────────────────────────────────────

def evolution_pipeline():
    """定时执行统一演化流水线，避免走回 Tinder 本地旧链路。"""
    log.info("=== [演化流水线] 启动 ===")
    if not RUNNING:
        log.warning("[演化流水线] 被中断")
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(SHARED_ASSETS_DIR / "unified_evolution.py")],
            capture_output=True,
            text=True,
            timeout=1200,
        )
        if result.returncode == 0:
            log.info("=== [演化流水线] 完成 ===")
            return True
        detail = (result.stderr or result.stdout or "").strip()
        log.error(f"[演化流水线] unified_evolution.py 失败: {detail[-400:]}")
        return False
    except Exception as e:
        log.error(f"[演化流水线] 异常: {e}")
        return False


# ── 聊天Daemon进程 ────────────────────────────────────────────

def start_chat_daemon(queue: Queue):
    """子进程：运行 tinder_daemon / chat_worker"""
    try:
        sys.path.insert(0, str(SHARED_ASSETS_DIR))
        from unified_orchestrator import run_loop
        run_loop()
    except Exception as e:
        log.error(f"[ChatDaemon] 异常: {e}")
        queue.put({"type": "daemon_error", "msg": str(e)})


# ── 主调度器 ─────────────────────────────────────────────────

class Orchestrator:
    def __init__(self):
        if BackgroundScheduler is None or CronTrigger is None:
            raise RuntimeError("apscheduler 未安装，无法运行 legacy orchestrator；请直接使用 shared_assets/unified_orchestrator.py")
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.daemon_process: Process | None = None
        self.queue = Queue()
        self.last_strategy_mtime = get_last_strategy_mtime()

    def start_daemon(self):
        """启动聊天Daemon进程"""
        if self.daemon_process and self.daemon_process.is_alive():
            log.info("[Daemon] 已在运行")
            return

        log.info("[Daemon] 启动聊天进程...")
        self.daemon_process = Process(
            target=start_chat_daemon,
            args=(self.queue,),
            name="ChatDaemon",
        )
        self.daemon_process.start()
        log.info(f"[Daemon] PID={self.daemon_process.pid}")

    def stop_daemon(self):
        """优雅停止Daemon进程"""
        if self.daemon_process and self.daemon_process.is_alive():
            log.info("[Daemon] 发送SIGTERM...")
            self.daemon_process.terminate()
            self.daemon_process.join(timeout=30)
            if self.daemon_process.is_alive():
                log.warning("[Daemon] 未响应，强制kill")
                self.daemon_process.kill()
                self.daemon_process.join()

    def check_strategy_reload(self):
        """监控 strategy_config.json 变更，触发热重载"""
        current_mtime = get_last_strategy_mtime()
        if current_mtime > self.last_strategy_mtime:
            log.info(f"[热重载] 检测到配置变更 ({current_mtime:.0f})，通知Daemon重载")
            self.last_strategy_mtime = current_mtime
            save_watchdog_state(self.last_strategy_mtime,
                                datetime.now().isoformat())
            # 通知Daemon重载（通过重启Daemon实现零秒热重载）
            self.stop_daemon()
            self.start_daemon()

    def run_evolution_blocking(self):
        """阻塞式执行演化流水线（调度器在线程中调用）"""
        log.info("[Scheduler] 演化流水线开始（阻塞）")
        # 演化期间暂停Daemon
        self.stop_daemon()
        try:
            evolution_pipeline()
        finally:
            # 演化完成后恢复Daemon
            self.start_daemon()

    def setup_scheduler(self):
        """配置定时演化任务"""
        # 每天凌晨 03:00 执行
        self.scheduler.add_job(
            self.run_evolution_blocking,
            CronTrigger(hour=3, minute=0, timezone="Asia/Shanghai"),
            id="evolution_pipeline",
            name="每日语料演化",
            misfire_grace_time=3600,
        )
        log.info("[Scheduler] 已注册 03:00 演化任务")

        # 每 5 分钟检查配置热重载
        self.scheduler.add_job(
            self.check_strategy_reload,
            "interval",
            minutes=5,
            id="strategy_watchdog",
            name="配置热重载检测",
        )
        log.info("[Scheduler] 已注册 5分钟 配置监控")

    def run(self):
        """主循环"""
        log.info("=" * 50)
        log.info("Orchestrator 启动")
        log.info(f"Profile: {PROFILE_PATH}")
        log.info("=" * 50)

        self.setup_scheduler()
        self.scheduler.start()

        # 启动聊天Daemon
        self.start_daemon()

        # 主循环：监控子进程状态 + 检查信号
        watchdog_interval = 30
        last_evolution = load_watchdog_state().get("last_evolution", "")

        while RUNNING:
            try:
                time.sleep(watchdog_interval)

                # 检查Daemon进程状态
                if not self.daemon_process or not self.daemon_process.is_alive():
                    log.warning("[Daemon] 进程已退出，重新启动")
                    self.start_daemon()

                # 检查队列消息
                try:
                    while not self.queue.empty():
                        msg = self.queue.get_nowait()
                        log.info(f"[Daemon 消息] {msg}")
                except Exception:
                    pass

                # 检查演化任务是否完成（更新最后运行时间）
                state = load_watchdog_state()
                if state.get("last_evolution") != last_evolution:
                    last_evolution = state["last_evolution"]
                    log.info(f"[演化] 最后成功运行: {last_evolution}")

            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"[主循环] 异常: {e}")

        # ── 退出流程 ────────────────────────────────────────────
        log.info("正在关闭...")
        self.scheduler.shutdown(wait=True)
        self.stop_daemon()
        log.info("Orchestrator 已关闭")


# ── 入口 ──────────────────────────────────────────────────────
if __name__ == "__main__":
    shared_orchestrator = SHARED_ASSETS_DIR / "unified_orchestrator.py"
    if shared_orchestrator.exists():
        log.warning("[Deprecated] auto_orchestrator.py 已废弃，自动转交 shared_assets/unified_orchestrator.py")
        os.execv(sys.executable, [sys.executable, str(shared_orchestrator)])
    orchestrator = Orchestrator()
    orchestrator.run()
