#!/usr/bin/env python3
"""
共享语料飞轮存储
================

为 Tinder / Bumble 提供统一的对话结果存储与 outcome 更新语义。
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import sqlite3
import logging
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Optional

PROJECTS_DIR = Path(__file__).resolve().parent.parent
LEGACY_DB_PATH = PROJECTS_DIR / "tinder-automation" / "conversation_log.db"
DB_PATH = Path.home() / ".openclaw" / "conversation_log.db"
STALE_RECORD_RETENTION_DAYS = 90
STALE_RECORD_OUTCOME_THRESHOLD = 0.55
POSITIVE_OUTCOME_LABELS = {"partner_followup_question", "partner_followup_engaged"}
SAFE_TABLE_NAMES = {"conversations", "conversations_v3", "match_profiles", "match_profiles_v2"}
MISSING_SNAPSHOT_KEY = "__snapshot_store_failed__"
log = logging.getLogger("ConversationStore")

PARTNER_FOLLOWUP_OUTCOMES = {
    "partner_followup_low_info": (0.35, "partner_followup_low_info"),
    "partner_followup_basic": (0.6, "partner_followup_basic"),
    "partner_followup_question": (0.8, "partner_followup_question"),
    "partner_followup_engaged": (0.9, "partner_followup_engaged"),
}

CORPUS_OUTCOME_LABEL_BONUS = {
    "partner_followup_engaged": 0.18,
    "partner_followup_question": 0.12,
    "partner_followup_basic": 0.05,
    "partner_followup_low_info": -0.15,
}
CORPUS_INTENT_BONUS = {
    "reply": 0.08,
    "opener": 0.03,
    "reactivation": 0.0,
}
CORPUS_RECENCY_HALF_LIFE_DAYS = 14.0


def outcome_from_partner_followup(event: str) -> tuple[float, str] | None:
    return PARTNER_FOLLOWUP_OUTCOMES.get(str(event or "").strip())


def _parse_db_timestamp(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "")).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _recency_multiplier(created_at: datetime | None) -> float:
    if not created_at:
        return 1.0
    now = datetime.now(timezone.utc)
    age_days = max((now - created_at).total_seconds() / 86400.0, 0.0)
    return math.pow(0.5, age_days / CORPUS_RECENCY_HALF_LIFE_DAYS)


def _corpus_priority_score(
    outcome: float,
    outcome_label: str | None,
    created_at: datetime | None,
    intent: str = "",
) -> float:
    base = float(outcome or 0.0)
    label_bonus = CORPUS_OUTCOME_LABEL_BONUS.get(str(outcome_label or "").strip(), 0.0)
    normalized_intent = str(intent or "").strip()
    intent_bonus = CORPUS_INTENT_BONUS.get(normalized_intent, 0.0)
    if normalized_intent == "reactivation" and label_bonus < 0.1:
        intent_bonus = -0.02
    return round((base + label_bonus + intent_bonus) * _recency_multiplier(created_at), 6)


def _snapshot_key(
    platform: str,
    match_id: str,
    messages: list,
    reply: str,
    intent: str = "",
) -> str:
    payload = {
        "platform": str(platform or "").strip(),
        "match_id": str(match_id or "").strip(),
        "messages": messages or [],
        "reply": str(reply or "").strip(),
        "intent": str(intent or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ConversationStore:
    """平台感知的对话结果存储（SQLite）"""

    def __init__(self, db_path: str = None):
        self.db_path = str(self._resolve_db_path(db_path))
        self._prepare_db_file()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        self._configure_connection(conn)
        return conn

    def _resolve_db_path(self, db_path: str | None) -> Path:
        if db_path:
            return Path(db_path).expanduser()
        env_path = os.getenv("APP_SHARED_DB_PATH") or os.getenv("APP_DATABASE__SHARED_DB_PATH")
        if env_path:
            return Path(env_path).expanduser()
        try:
            from config import get_config

            return Path(getattr(get_config(), "shared_db_path", DB_PATH)).expanduser()
        except Exception:
            return DB_PATH

    def _prepare_db_file(self) -> None:
        target = Path(self.db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if (
                not target.exists()
                and LEGACY_DB_PATH.exists()
                and target.resolve() != LEGACY_DB_PATH.resolve()
            ):
                shutil.copy2(LEGACY_DB_PATH, target)
                log.info(f"已迁移旧 conversation store: {LEGACY_DB_PATH} -> {target}")
        except Exception as exc:
            log.warning(f"conversation store 旧库迁移失败: {exc}")

    @staticmethod
    def _configure_connection(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")

    def _safe_table_name(self, table_name: str) -> str:
        candidate = str(table_name or "").strip()
        if candidate not in SAFE_TABLE_NAMES:
            raise ValueError(f"unsafe table name: {candidate}")
        return candidate

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return bool(row)

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> list[str]:
        table_name = self._safe_table_name(table_name)
        if not self._table_exists(conn, table_name):
            return []
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [str(row[1]) for row in rows]

    def _table_sql(self, conn: sqlite3.Connection, table_name: str) -> str:
        table_name = self._safe_table_name(table_name)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return str(row[0] or "") if row else ""

    def _create_conversations_table(self, conn: sqlite3.Connection, table_name: str = "conversations") -> None:
        table_name = self._safe_table_name(table_name)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT 'tinder',
                match_id TEXT NOT NULL,
                match_name TEXT,
                messages_json TEXT NOT NULL,
                reply TEXT NOT NULL,
                intent TEXT,
                snapshot_key TEXT UNIQUE,
                outcome REAL DEFAULT 0.5,
                outcome_label TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def _migrate_conversations_table(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "conversations")
        if not columns:
            self._create_conversations_table(conn)
        else:
            table_sql = self._table_sql(conn, "conversations")
            needs_rebuild = (
                "platform" not in columns
                or "intent" not in columns
                or "snapshot_key" not in columns
                or "UNIQUE(platform, match_id)" in table_sql.replace('"', "")
            )
            if needs_rebuild:
                select_platform = "platform" if "platform" in columns else "'tinder'"
                select_intent = "intent" if "intent" in columns else "''"
                legacy_rows = conn.execute(
                    f"""
                    SELECT id, match_id, match_name, messages_json, reply,
                           outcome, outcome_label, created_at, updated_at,
                           {select_platform} AS platform,
                           {select_intent} AS intent
                    FROM conversations
                    ORDER BY id ASC
                    """
                ).fetchall()
                self._create_conversations_table(conn, "conversations_v3")
                for row in legacy_rows:
                    (
                        row_id,
                        match_id,
                        match_name,
                        messages_json,
                        reply,
                        outcome,
                        outcome_label,
                        created_at,
                        updated_at,
                        platform,
                        intent,
                    ) = row
                    try:
                        messages = json.loads(messages_json)
                    except Exception:
                        messages = []
                    snapshot_key = _snapshot_key(platform, match_id, messages, reply, intent)
                    conn.execute(
                        """
                        INSERT INTO conversations_v3
                            (id, platform, match_id, match_name, messages_json, reply,
                             intent, snapshot_key, outcome, outcome_label, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(snapshot_key) DO UPDATE SET
                            match_name=excluded.match_name,
                            outcome=excluded.outcome,
                            outcome_label=excluded.outcome_label,
                            updated_at=excluded.updated_at
                        """,
                        (
                            row_id,
                            platform,
                            match_id,
                            match_name,
                            messages_json,
                            reply,
                            intent,
                            snapshot_key,
                            outcome,
                            outcome_label,
                            created_at,
                            updated_at,
                        ),
                    )
                conn.execute("DROP TABLE conversations")
                conn.execute("ALTER TABLE conversations_v3 RENAME TO conversations")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_match_id ON conversations(platform, match_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_outcome ON conversations(outcome)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversation_created_at ON conversations(created_at)"
        )

    def _refresh_match_profile(
        self,
        conn: sqlite3.Connection,
        match_id: str,
        match_name: str,
        *,
        platform: str = "tinder",
        outcome: float | None = None,
    ) -> None:
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(MAX(updated_at), CURRENT_TIMESTAMP)
            FROM conversations
            WHERE platform=? AND match_id=?
            """,
            (platform, match_id),
        ).fetchone()
        conversation_count = int(row[0] or 0) if row else 0
        conn.execute(
            """
            INSERT INTO match_profiles
                (platform, match_id, name, last_outcome, conversation_count, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(platform, match_id) DO UPDATE SET
                name=excluded.name,
                last_outcome=COALESCE(excluded.last_outcome, match_profiles.last_outcome),
                conversation_count=excluded.conversation_count,
                updated_at=CURRENT_TIMESTAMP
            """,
            (platform, match_id, match_name, outcome, conversation_count),
        )

    def _refresh_all_match_profiles(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT platform, match_id, COALESCE(MAX(match_name), '')
            FROM conversations
            GROUP BY platform, match_id
            """
        ).fetchall()
        existing_keys = {
            (str(row[0] or ""), str(row[1] or ""))
            for row in conn.execute("SELECT platform, match_id FROM match_profiles").fetchall()
        }
        seen_keys: set[tuple[str, str]] = set()
        for platform, match_id, match_name in rows:
            key = (str(platform or ""), str(match_id or ""))
            seen_keys.add(key)
            latest = conn.execute(
                """
                SELECT outcome
                FROM conversations
                WHERE platform=? AND match_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                key,
            ).fetchone()
            outcome = float(latest[0]) if latest and latest[0] is not None else None
            self._refresh_match_profile(
                conn,
                key[1],
                str(match_name or ""),
                platform=key[0],
                outcome=outcome,
            )
        stale_keys = existing_keys - seen_keys
        for platform, match_id in stale_keys:
            conn.execute(
                "DELETE FROM match_profiles WHERE platform=? AND match_id=?",
                (platform, match_id),
            )

    def _migrate_match_profiles_table(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "match_profiles")
        if columns and "platform" in columns:
            return

        if columns:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS match_profiles_v2 (
                    platform TEXT NOT NULL DEFAULT 'tinder',
                    match_id TEXT NOT NULL,
                    name TEXT,
                    tags_json TEXT,
                    last_outcome REAL DEFAULT 0.5,
                    conversation_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform, match_id)
                )
            """)
            conn.execute("""
                INSERT INTO match_profiles_v2
                    (platform, match_id, name, tags_json, last_outcome,
                     conversation_count, updated_at)
                SELECT
                    'tinder',
                    match_id,
                    name,
                    tags_json,
                    last_outcome,
                    conversation_count,
                    updated_at
                FROM match_profiles
            """)
            conn.execute("DROP TABLE match_profiles")
            conn.execute("ALTER TABLE match_profiles_v2 RENAME TO match_profiles")
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS match_profiles (
                    platform TEXT NOT NULL DEFAULT 'tinder',
                    match_id TEXT NOT NULL,
                    name TEXT,
                    tags_json TEXT,
                    last_outcome REAL DEFAULT 0.5,
                    conversation_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(platform, match_id)
                )
            """)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_profiles_platform_match_id ON match_profiles(platform, match_id)"
        )

    def _init_db(self):
        with self._connect() as conn:
            self._migrate_conversations_table(conn)
            self._migrate_match_profiles_table(conn)
            self.cleanup_old_records(conn=conn)
            conn.commit()

    def cleanup_old_records(
        self,
        *,
        conn: sqlite3.Connection | None = None,
        retention_days: int = STALE_RECORD_RETENTION_DAYS,
        stale_outcome_threshold: float = STALE_RECORD_OUTCOME_THRESHOLD,
    ) -> int:
        own_conn = conn is None
        if own_conn:
            conn = self._connect()
        assert conn is not None
        cutoff_modifier = f"-{int(retention_days)} days"
        placeholders = ", ".join("?" for _ in POSITIVE_OUTCOME_LABELS)
        sql = f"""
            DELETE FROM conversations
            WHERE id IN (
                SELECT c.id
                FROM conversations c
                JOIN (
                    SELECT platform, match_id, MAX(id) AS latest_id
                    FROM conversations
                    GROUP BY platform, match_id
                ) latest
                  ON latest.platform = c.platform
                 AND latest.match_id = c.match_id
                WHERE c.id != latest.latest_id
                  AND datetime(COALESCE(c.created_at, CURRENT_TIMESTAMP)) < datetime('now', ?)
                  AND COALESCE(c.outcome, 0.5) < ?
                  AND COALESCE(c.outcome_label, '') NOT IN ({placeholders})
            )
        """
        params: list[object] = [cutoff_modifier, float(stale_outcome_threshold), *sorted(POSITIVE_OUTCOME_LABELS)]
        try:
            cursor = conn.execute(sql, tuple(params))
            deleted = int(cursor.rowcount or 0)
            if deleted:
                self._refresh_all_match_profiles(conn)
                conn.execute("PRAGMA optimize")
            if own_conn:
                conn.commit()
            return deleted
        finally:
            if own_conn:
                conn.close()

    def store(
        self,
        match_id: str,
        match_name: str,
        messages: list,
        reply: str,
        outcome: float = 0.5,
        outcome_label: str = None,
        intent: str = "",
        *,
        platform: str = "tinder",
    ) -> tuple[int, str]:
        """记录一轮对话结果，返回 (rowid, snapshot_key)"""
        with self._connect() as conn:
            snapshot_key = _snapshot_key(platform, match_id, messages, reply, intent)
            cursor = conn.execute(
                """
                INSERT INTO conversations
                    (platform, match_id, match_name, messages_json, reply, intent, snapshot_key,
                     outcome, outcome_label, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(snapshot_key) DO UPDATE SET
                    match_name=excluded.match_name,
                    messages_json=excluded.messages_json,
                    reply=excluded.reply,
                    intent=excluded.intent,
                    outcome=excluded.outcome,
                    outcome_label=excluded.outcome_label,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    platform,
                    match_id,
                    match_name,
                    json.dumps(messages, ensure_ascii=False),
                    reply,
                    intent,
                    snapshot_key,
                    outcome,
                    outcome_label,
                ),
            )
            self._refresh_match_profile(
                conn,
                match_id,
                match_name,
                platform=platform,
                outcome=outcome,
            )
            conn.commit()
            return cursor.lastrowid, snapshot_key

    def update_outcome(
        self,
        match_id: str,
        outcome: float,
        outcome_label: str = None,
        *,
        platform: str = "tinder",
        snapshot_key: str = "",
    ) -> None:
        """后续更新结果"""
        with self._connect() as conn:
            if snapshot_key:
                row = conn.execute(
                    """
                    SELECT id, COALESCE(match_name, '')
                    FROM conversations
                    WHERE platform=? AND match_id=? AND snapshot_key=?
                    LIMIT 1
                    """,
                    (platform, match_id, snapshot_key),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, COALESCE(match_name, '')
                    FROM conversations
                    WHERE platform=? AND match_id=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (platform, match_id),
                ).fetchone()
            if not row:
                return
            conn.execute(
                """
                UPDATE conversations
                SET outcome=?, outcome_label=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (outcome, outcome_label, row[0]),
            )
            self._refresh_match_profile(
                conn,
                match_id,
                str(row[1] or ""),
                platform=platform,
                outcome=outcome,
            )
            conn.commit()

    def get_match_history(self, match_id: str, *, platform: str = "tinder") -> list:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT messages_json, reply, intent, outcome, outcome_label, created_at
                FROM conversations
                WHERE platform=? AND match_id=?
                ORDER BY id ASC
                """,
                (platform, match_id),
            ).fetchall()
            return [
                {
                    "messages": json.loads(r[0]),
                    "reply": r[1],
                    "intent": r[2],
                    "outcome": r[3],
                    "outcome_label": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]

    def get_top_corpus(
        self,
        limit: int = 50,
        min_outcome: float = 0.8,
        *,
        platform: str | None = None,
    ) -> list:
        with self._connect() as conn:
            sql = """
                SELECT platform, messages_json, reply, intent, outcome, outcome_label, created_at
                FROM conversations
                WHERE outcome >= ? AND reply IS NOT NULL AND reply != ''
            """
            params: list[object] = [min_outcome]
            if platform:
                sql += " AND platform = ?"
                params.append(platform)
            sql += " ORDER BY updated_at DESC"
            rows = conn.execute(sql, tuple(params)).fetchall()

            candidates = []
            for platform_name, messages_json, reply, intent, outcome, outcome_label, created_at_raw in rows:
                msgs = json.loads(messages_json)
                if not msgs:
                    continue
                them_msgs = [m for m in msgs if m.get("sender") in ("them", "other")]
                if not them_msgs:
                    continue
                user_input = them_msgs[-1].get("text", "").strip()
                bot_output = str(reply or "").strip()
                if user_input and bot_output and len(bot_output) > 1:
                    created_at = _parse_db_timestamp(created_at_raw)
                    candidates.append(
                        {
                            "platform": platform_name,
                            "input": user_input,
                            "output": bot_output,
                            "intent": str(intent or ""),
                            "outcome": outcome,
                            "outcome_label": outcome_label,
                            "corpus_score": _corpus_priority_score(
                                outcome,
                                outcome_label,
                                created_at,
                                str(intent or ""),
                            ),
                            "created_at": str(created_at_raw or ""),
                        }
                    )
            if platform:
                candidates.sort(key=lambda item: (item["corpus_score"], item["outcome"]), reverse=True)
                return candidates[:limit]

            by_platform: dict[str, list[dict]] = {}
            for item in candidates:
                by_platform.setdefault(item["platform"] or "unknown", []).append(item)
            for items in by_platform.values():
                items.sort(key=lambda item: (item["corpus_score"], item["outcome"]), reverse=True)

            platform_queues = {
                name: deque(items)
                for name, items in by_platform.items()
            }
            ordered_platforms = sorted(
                platform_queues.keys(),
                key=lambda name: (len(platform_queues[name]), name),
                reverse=True,
            )
            corpus: list[dict] = []
            while ordered_platforms and len(corpus) < limit:
                next_round = []
                for platform_name in ordered_platforms:
                    items = platform_queues.get(platform_name)
                    if not items:
                        continue
                    corpus.append(items.popleft())
                    if len(corpus) >= limit:
                        break
                    if items:
                        next_round.append(platform_name)
                ordered_platforms = next_round
            return corpus

    def get_all_outcomes(self, *, platform: str | None = None) -> dict:
        with self._connect() as conn:
            sql = """
                SELECT outcome_label, COUNT(*) as cnt
                FROM conversations
                WHERE outcome_label IS NOT NULL
            """
            params: list[object] = []
            if platform:
                sql += " AND platform = ?"
                params.append(platform)
            sql += " GROUP BY outcome_label"
            rows = conn.execute(sql, tuple(params)).fetchall()
            return {r[0]: r[1] for r in rows}

    def update_match_profile(
        self,
        match_id: str,
        name: str,
        tags: list = None,
        outcome: float = None,
        *,
        platform: str = "tinder",
    ) -> None:
        with self._connect() as conn:
            if outcome is not None:
                conn.execute(
                    """
                    INSERT INTO match_profiles
                        (platform, match_id, name, tags_json, last_outcome,
                         conversation_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(platform, match_id) DO UPDATE SET
                        name=excluded.name,
                        tags_json=excluded.tags_json,
                        last_outcome=COALESCE(excluded.last_outcome, last_outcome),
                        conversation_count=conversation_count+1,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (platform, match_id, name, json.dumps(tags or [], ensure_ascii=False), outcome),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO match_profiles (platform, match_id, name, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(platform, match_id) DO UPDATE SET
                        name=excluded.name,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (platform, match_id, name),
                )
            conn.commit()

    def get_stats(self, *, platform: str | None = None) -> dict:
        with self._connect() as conn:
            if platform:
                params = (platform,)
                total = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE platform = ?",
                    params,
                ).fetchone()[0]
                avg_outcome = conn.execute(
                    "SELECT AVG(outcome) FROM conversations WHERE platform = ? AND outcome IS NOT NULL",
                    params,
                ).fetchone()[0] or 0
                positive = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE platform = ? AND outcome >= 0.8",
                    params,
                ).fetchone()[0]
                negative = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE platform = ? AND outcome <= 0.2 AND outcome > 0",
                    params,
                ).fetchone()[0]
            else:
                total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
                avg_outcome = conn.execute(
                    "SELECT AVG(outcome) FROM conversations WHERE outcome IS NOT NULL"
                ).fetchone()[0] or 0
                positive = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE outcome >= 0.8"
                ).fetchone()[0]
                negative = conn.execute(
                    "SELECT COUNT(*) FROM conversations WHERE outcome <= 0.2 AND outcome > 0"
                ).fetchone()[0]
            return {
                "total_conversations": total,
                "avg_outcome": round(avg_outcome, 3),
                "positive": positive,
                "negative": negative,
                "neutral": total - positive - negative,
            }


def 回流_corpus_to_file(
    corpus_store: ConversationStore,
    output_path: str = None,
    limit: int = 50,
    *,
    platform: str | None = None,
):
    if output_path is None:
        output_path = str(PROJECTS_DIR / "tinder-automation" / "corpus.json")

    top_corpus = corpus_store.get_top_corpus(limit=limit, platform=platform)

    existing = []
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    existing_inputs = {
        (c.get("platform"), c.get("input"), c.get("outcome_label"))
        for c in existing
        if isinstance(c, dict)
    }
    new_entries = [
        c for c in top_corpus
        if (c.get("platform"), c.get("input"), c.get("outcome_label")) not in existing_inputs
    ]

    merged = (existing + new_entries)[-100:]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"[语料飞轮] 回流 {len(new_entries)} 条高转化语料到 corpus.json")
    print(f"[语料飞轮] corpus.json 共 {len(merged)} 条")
    return merged
