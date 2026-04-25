# queue_db.py — 统一消息队列（SQLite）
# 步骤1核心: 生产者(巡检)压入 → LLM Worker异步消费 → reply_cache待发送

import sqlite3, time, json, threading, logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

DB_PATH = Path(__file__).parent / "message_queue.db"
log = logging.getLogger("MessageQueue")


@dataclass
class QueuedMessage:
    """压入队列的每条待处理消息"""
    platform: str          # "tinder" | "bumble"
    match_id: str          # 对话唯一ID
    match_name: str        # 对方名字
    messages: list         # [{"sender": "them", "text": "..."}]
    bio: str = ""
    age: int = 0
    enqueued_at: float = field(default_factory=time.time)
    pending_id: int = 0

    def to_dict(self):
        return {
            "platform": self.platform,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "messages": self.messages,
            "bio": self.bio,
            "age": self.age,
            "enqueued_at": self.enqueued_at,
            "pending_id": self.pending_id,
        }


class MessageQueue:
    _init_lock = threading.Lock()  # 只在初始化时用
    _instance: Optional["MessageQueue"] = None

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._lock = threading.Lock()  # 实例级锁
        self._init_db()

    @classmethod
    def get_instance(cls) -> "MessageQueue":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _init_db(self):
        with self._lock:
            self._db_conn.execute("PRAGMA journal_mode=WAL")
            self._db_conn.execute("PRAGMA synchronous=NORMAL")
            self._db_conn.execute("PRAGMA busy_timeout=30000")
            self._db_conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform    TEXT    NOT NULL,
                    match_id    TEXT    NOT NULL,
                    match_name  TEXT,
                    messages    TEXT    NOT NULL,
                    bio         TEXT    DEFAULT '',
                    age         INTEGER DEFAULT 0,
                    enqueued_at REAL   NOT NULL,
                    UNIQUE(platform, match_id)
                )
            """)
            self._db_conn.execute("""
                CREATE TABLE IF NOT EXISTS reply_cache (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform        TEXT    NOT NULL,
                    match_id        TEXT    NOT NULL,
                    reply           TEXT    NOT NULL,
                    generated_at    REAL    NOT NULL,
                    sent            INTEGER DEFAULT 0,
                    sent_at         REAL,
                    UNIQUE(platform, match_id)
                )
            """)
            self._db_conn.commit()

    # ── 生产者: 巡检压入队列 ──────────────────────────────────────
    def enqueue(self, item: QueuedMessage) -> bool:
        with self._lock:
            try:
                # 新消息覆盖旧 pending 时，同步清掉尚未发送的旧 reply，避免发送侧取到过期缓存。
                self._db_conn.execute(
                    "DELETE FROM reply_cache WHERE platform=? AND match_id=? AND sent=0",
                    (item.platform, item.match_id),
                )
                self._db_conn.execute("""
                    INSERT OR REPLACE INTO pending_messages
                        (platform, match_id, match_name, messages, bio, age, enqueued_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    item.platform,
                    item.match_id,
                    item.match_name,
                    json.dumps(item.messages, ensure_ascii=False),
                    item.bio,
                    item.age,
                    item.enqueued_at,
                ))
                self._db_conn.commit()
                return True
            except Exception as e:
                print(f"[Queue] enqueue error: {e}")
                return False

    # ── 消费者: LLM Worker 拉取 ──────────────────────────────────
    def dequeue(self, platform: str = None, limit: int = 10) -> list[QueuedMessage]:
        with self._lock:
            if platform:
                rows = self._db_conn.execute(
                    "SELECT * FROM pending_messages WHERE platform=? ORDER BY enqueued_at ASC LIMIT ?",
                    (platform, limit)
                ).fetchall()
            else:
                rows = self._db_conn.execute(
                    "SELECT * FROM pending_messages ORDER BY enqueued_at ASC LIMIT ?",
                    (limit,)
                ).fetchall()
            cols = ["id", "platform", "match_id", "match_name", "messages", "bio", "age", "enqueued_at"]
            items = []
            for r in rows:
                d = dict(zip(cols, r))
                d["messages"] = json.loads(d["messages"])
                d["pending_id"] = d.pop("id")
                items.append(QueuedMessage(**d))
            return items

    def _is_current_pending(self, item: QueuedMessage) -> bool:
        row = self._db_conn.execute(
            "SELECT id, enqueued_at FROM pending_messages WHERE platform=? AND match_id=?",
            (item.platform, item.match_id),
        ).fetchone()
        if not row:
            return False
        current_id, current_enqueued_at = row
        return current_id == item.pending_id and abs(float(current_enqueued_at) - float(item.enqueued_at)) < 1e-6

    def mark_sent(self, item: QueuedMessage, reply: str) -> bool:
        with self._lock:
            try:
                now = time.time()
                self._db_conn.execute("BEGIN IMMEDIATE")
                deleted = self._db_conn.execute(
                    """
                    DELETE FROM pending_messages
                    WHERE id=? AND platform=? AND match_id=? AND ABS(enqueued_at - ?) < 0.000001
                    """,
                    (item.pending_id, item.platform, item.match_id, item.enqueued_at),
                )
                if deleted.rowcount != 1:
                    self._db_conn.rollback()
                    log.info(
                        f"[Queue] 丢弃过期 reply: {item.platform}/{item.match_name} "
                        f"(match_id={item.match_id}, pending_id={item.pending_id})"
                    )
                    return False
                self._db_conn.execute("""
                    INSERT OR REPLACE INTO reply_cache
                        (platform, match_id, reply, generated_at, sent, sent_at)
                    VALUES (?, ?, ?, ?, 0, NULL)
                """, (item.platform, item.match_id, reply, now))
                self._db_conn.commit()
                return True
            except Exception as e:
                try:
                    self._db_conn.rollback()
                except Exception:
                    pass
                print(f"[Queue] mark_sent error: {e}")
                return False

    def mark_skipped(self, item: QueuedMessage) -> bool:
        """无安全回复时将当前待处理项出队，避免后台 worker 死循环重试同一条消息。"""
        with self._lock:
            try:
                self._db_conn.execute("BEGIN IMMEDIATE")
                deleted = self._db_conn.execute(
                    """
                    DELETE FROM pending_messages
                    WHERE id=? AND platform=? AND match_id=? AND ABS(enqueued_at - ?) < 0.000001
                    """,
                    (item.pending_id, item.platform, item.match_id, item.enqueued_at),
                )
                if deleted.rowcount != 1:
                    self._db_conn.rollback()
                    log.info(
                        f"[Queue] 跳过过期 pending: {item.platform}/{item.match_name} "
                        f"(match_id={item.match_id}, pending_id={item.pending_id})"
                    )
                    return False
                self._db_conn.commit()
                return True
            except Exception as e:
                try:
                    self._db_conn.rollback()
                except Exception:
                    pass
                print(f"[Queue] mark_skipped error: {e}")
                return False

    def get_cached_reply(self, platform: str, match_id: str) -> Optional[str]:
        with self._lock:
            row = self._db_conn.execute(
                "SELECT reply FROM reply_cache WHERE platform=? AND match_id=? AND sent=0",
                (platform, match_id)
            ).fetchone()
            return row[0] if row else None

    def mark_reply_sent(self, platform: str, match_id: str) -> bool:
        with self._lock:
            try:
                cur = self._db_conn.execute("""
                    UPDATE reply_cache
                    SET sent=1, sent_at=?
                    WHERE platform=? AND match_id=? AND sent=0
                """, (time.time(), platform, match_id))
                self._db_conn.commit()
                return cur.rowcount > 0
            except Exception as e:
                print(f"[Queue] mark_reply_sent error: {e}")
                return False

    def clear_sent(self, older_than_hours: int = 24) -> int:
        with self._lock:
            cutoff = time.time() - older_than_hours * 3600
            cur = self._db_conn.execute(
                "DELETE FROM reply_cache WHERE sent=1 AND sent_at<?",
                (cutoff,)
            )
            self._db_conn.commit()
            return cur.rowcount

    # ── 状态监控 ─────────────────────────────────────────────────
    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._db_conn.execute("SELECT COUNT(*) FROM pending_messages").fetchone()[0]

    @property
    def pending_by_platform(self) -> dict:
        with self._lock:
            rows = self._db_conn.execute(
                "SELECT platform, COUNT(*) FROM pending_messages GROUP BY platform"
            ).fetchall()
            return dict(rows)

    @property
    def cache_count(self) -> int:
        with self._lock:
            return self._db_conn.execute(
                "SELECT COUNT(*) FROM reply_cache WHERE sent=0"
            ).fetchone()[0]

    @property
    def stats(self) -> dict:
        return {
            "pending": self.pending_count,
            "pending_by_platform": self.pending_by_platform,
            "cache": self.cache_count,
        }


# ── LLM Worker (后台消费线程) ────────────────────────────────────────
import logging

log = logging.getLogger("LLMWorker")


class LLMWorker:
    """
    独立 Consumer: 不断从队列拉取 → 调用 LLM → 写 reply_cache
    发送(TinderBot/BumbleBot)在下一轮巡检时自行从 reply_cache 消费
    """

    def __init__(self, poll_interval: float = 5.0, platform: str = None):
        """
        poll_interval: 轮询间隔(秒)
        platform: None=双平台, "tinder"=仅Tinder, "bumble"=仅Bumble
        """
        self.poll_interval = poll_interval
        self.platform = platform  # None 表示双平台
        self.queue = MessageQueue.get_instance()
        self._running = False
        self._thread: threading.Thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="LLMWorker")
        self._thread.start()
        log.info(f"[LLMWorker] 启动，轮询间隔 {self.poll_interval}s，平台={'双平台' if self.platform is None else self.platform}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        log.info("[LLMWorker] 已停止")

    def _run(self):
        while self._running:
            try:
                items = self.queue.dequeue(platform=self.platform, limit=5)
                if not items:
                    time.sleep(self.poll_interval)
                    continue

                for item in items:
                    if not self._running:
                        break
                    self._process(item)

            except Exception as e:
                log.error(f"[LLMWorker] 轮询异常: {e}")
                time.sleep(10)

    def _process(self, item: QueuedMessage):
        try:
            log.info(f"[LLMWorker] 处理: {item.platform}/{item.match_name} ({len(item.messages)}条消息)")

            # 延迟导入，避免启动时检查 API key
            from unified_reply_engine import generate_reply
            from runtime_feedback import record_runtime_feedback
            reply = generate_reply(
                messages=item.messages,
                bio=item.bio,
                age=item.age,
                platform=item.platform,
            )
            if not reply:
                self.queue.mark_skipped(item)
                record_runtime_feedback(
                    item.platform,
                    item.match_id,
                    item.match_name,
                    "queue_no_safe_reply",
                    intent="queue_generation",
                    reason="no_safe_reply",
                    messages=item.messages,
                )
                log.info(f"[LLMWorker] ⏭️ 跳过: {item.match_name} | 无安全回复")
                return

            cached = self.queue.mark_sent(item, reply)
            if cached:
                log.info(f"[LLMWorker] ✅ 缓存: {item.match_name} | {reply[:30]}...")
            else:
                log.info(f"[LLMWorker] ↪️ 丢弃过期生成结果: {item.match_name}")

        except Exception as e:
            try:
                from runtime_feedback import record_runtime_feedback
                record_runtime_feedback(
                    item.platform,
                    item.match_id,
                    item.match_name,
                    "queue_generation_failed",
                    intent="queue_generation",
                    reason=str(e),
                    messages=item.messages,
                )
            except Exception:
                pass
            log.error(f"[LLMWorker] ❌ 生成失败: {item.match_id} | {e}")


# ── 快捷函数 ────────────────────────────────────────────────────────
def get_queue() -> MessageQueue:
    return MessageQueue.get_instance()


def enqueue_message(platform: str, match_id: str, match_name: str,
                    messages: list, bio: str = "", age: int = 0) -> bool:
    """巡检模块调用的快捷入队函数"""
    item = QueuedMessage(
        platform=platform,
        match_id=match_id,
        match_name=match_name,
        messages=messages,
        bio=bio,
        age=age,
    )
    return get_queue().enqueue(item)


def get_reply(platform: str, match_id: str) -> Optional[str]:
    """发送模块调用的快捷查询函数"""
    return get_queue().get_cached_reply(platform, match_id)


def confirm_sent(platform: str, match_id: str) -> bool:
    """发送成功后调用"""
    return get_queue().mark_reply_sent(platform, match_id)
