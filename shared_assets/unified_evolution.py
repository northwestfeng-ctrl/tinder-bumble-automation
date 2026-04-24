#!/usr/bin/env python3
"""
unified_evolution.py
=====================
深夜（00:00-08:00）全量语料采集与 NotebookLM 演化流水线。

执行步骤
--------
1. 全量抓取
   - tinder_history_scraper.py   → ../tinder-automation/pending_corpus.jsonl
   - bumble_history_scraper.py / bumble_corpus_history.json → ../bumble-automation/pending_corpus.jsonl

2. 语料合流
   - 读取两端 pending_corpus.jsonl
   - 统一字段格式：{sender, text, platform, timestamp, name}
   - 合并写入 shared_assets/unified_corpus.jsonl
   - 去重（按平台/对象/消息维度哈希）

3. NotebookLM 演化
   - notebooklm_sync.py         → 真正同步 NotebookLM 网页来源
   - nblm_uploader.py --analyze  → 生成 review draft

4. 自动审核并覆盖生产策略
   - 写入 shared_assets/strategy_config.review.json
   - 本地规则审核通过后自动同步到生产 strategy_config.json

用法
----
# 手动触发（测试）
python3 unified_evolution.py

# 自动（由 unified_orchestrator.py 在 00:00-08:00 宵禁期间调用）
"""
from __future__ import annotations

import os
import sys
import json
import re
import hashlib
import subprocess
import logging
import time
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime
from typing import Iterator

from runtime_feedback import EVENT_BASE_WEIGHTS, build_runtime_feedback_summary

# ─────────────────────────────────────────────────────────────────
# 路径
# ─────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).parent          # shared_assets/
TINDER_DIR      = SCRIPT_DIR.parent / "tinder-automation"
BUMBLE_DIR      = SCRIPT_DIR.parent / "bumble-automation"

TINDER_CORPUS  = TINDER_DIR / "pending_corpus.jsonl"
BUMBLE_CORPUS  = BUMBLE_DIR / "pending_corpus.jsonl"
BUMBLE_SNAPSHOT = BUMBLE_DIR / "bumble_corpus_history.json"
UNIFIED_CORPUS = SCRIPT_DIR / "unified_corpus.jsonl"
UNIFIED_CFG    = SCRIPT_DIR / "strategy_config.json"
REVIEW_CFG     = SCRIPT_DIR / "strategy_config.review.json"
NBLM_SYNC_RESULT = SCRIPT_DIR / "notebooklm_sync_result.json"
NBLM_CONTEXT = SCRIPT_DIR / "notebooklm_context.json"

NBLM_UPLOADER  = TINDER_DIR / "nblm_uploader.py"
NBLM_SYNCER    = SCRIPT_DIR / "notebooklm_sync.py"
NBLM_CONTEXT_FETCHER = SCRIPT_DIR / "notebooklm_context.py"

# ─────────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────────
LOG_FILE = SCRIPT_DIR / "evolution.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("Evolution")


# ─────────────────────────────────────────────────────────────────
# Step 1: 采集
# ─────────────────────────────────────────────────────────────────
def _refresh_bumble_pending_from_snapshot() -> bool:
    """当 Bumble scraper 缺失时，使用历史快照回填 pending_corpus.jsonl。"""
    if not BUMBLE_SNAPSHOT.exists():
        log.warning(f"[Scrape] Bumble 快照不存在: {BUMBLE_SNAPSHOT}")
        return False

    try:
        data = json.loads(BUMBLE_SNAPSHOT.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            log.warning("[Scrape] Bumble 快照为空，跳过回填")
            return False

        with open(BUMBLE_CORPUS, "w", encoding="utf-8") as f:
            for index, conv in enumerate(data):
                payload = {
                    "platform": "bumble",
                    "match_id": conv.get("match_id") or f"snapshot-{index}",
                    "match_name": conv.get("match_name") or conv.get("name") or "unknown",
                    "bio": conv.get("bio", ""),
                    "messages": conv.get("messages", []),
                    "timestamp": conv.get("timestamp", ""),
                }
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")

        log.info(f"[Scrape] ✅ bumble (snapshot fallback, {len(data)} conversations)")
        return True
    except Exception as e:
        log.error(f"[Scrape] ❌ bumble snapshot fallback: {e}")
        return False


def step_scrape(platform: str) -> bool:
    """调用对应平台的历史抓取脚本。"""
    scripts = {
        "tinder": [TINDER_DIR / "history_scraper.py"],
        "bumble": [
            BUMBLE_DIR / "history_scraper.py",
            BUMBLE_DIR / "bumble_history_scraper.py",
        ],
    }
    candidates = scripts.get(platform, [])
    script = next((path for path in candidates if path.exists()), None)
    if not script:
        if platform == "bumble":
            log.warning("[Scrape] Bumble scraper 缺失，尝试使用本地快照回填 pending_corpus.jsonl")
            return _refresh_bumble_pending_from_snapshot()
        log.warning(f"[Scrape] 平台 {platform} 脚本不存在: {candidates}")
        return False

    log.info(f"[Scrape] 开始采集 {platform}...")
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            log.info(f"[Scrape] ✅ {platform}")
            return True
        else:
            log.error(f"[Scrape] ❌ {platform}: {result.stderr[-200:]}")
            return False
    except subprocess.TimeoutExpired:
        log.error(f"[Scrape] ⏱ {platform} 超时")
        return False
    except Exception as e:
        log.error(f"[Scrape] ❌ {platform}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# Step 2: 合流
# ─────────────────────────────────────────────────────────────────
def _iter_jsonl(path: Path) -> Iterator[dict]:
    """逐行读取 jsonl"""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _normalize_pending_messages(messages: list) -> list[dict]:
    normalized: list[dict] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        sender = item.get("sender", "")
        if sender not in ("me", "them"):
            sender = "me" if item.get("is_mine") else "them"
        text = (
            item.get("text")
            or item.get("message")
            or item.get("content")
            or item.get("msg")
            or ""
        ).strip()
        normalized.append({
            "sender": sender or "them",
            "text": text,
        })
    return normalized


def _pending_record_key(rec: dict) -> str:
    return json.dumps({
        "record_type": rec.get("record_type", ""),
        "platform": rec.get("platform", ""),
        "match_id": rec.get("match_id", ""),
        "match_name": rec.get("match_name", rec.get("name", "")),
        "preview": rec.get("preview", ""),
        "bio": rec.get("bio", rec.get("match_bio", "")),
        "reply": rec.get("reply", ""),
        "intent": rec.get("intent", ""),
        "outcome": rec.get("outcome", ""),
        "outcome_label": rec.get("outcome_label", ""),
        "feedback_event": rec.get("feedback_event", ""),
        "feedback_reason": rec.get("feedback_reason", ""),
        "messages": _normalize_pending_messages(rec.get("messages", [])),
    }, ensure_ascii=False, sort_keys=True)


def _dedupe_pending_file(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0

    deduped: list[dict] = []
    seen: set[str] = set()
    original = 0
    for rec in _iter_jsonl(path):
        original += 1
        key = _pending_record_key(rec)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rec)

    with open(path, "w", encoding="utf-8") as f:
        for rec in deduped:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return original, len(deduped)


def step_prepare_pending() -> None:
    """合流前预清洗各平台 pending 队列，避免迁移期重复增量污染演化。"""
    for label, path in [("tinder", TINDER_CORPUS), ("bumble", BUMBLE_CORPUS)]:
        original, deduped = _dedupe_pending_file(path)
        if original:
            status = "changed" if original != deduped else "clean"
            log.info(f"[Prepare] {label} pending: {original} -> {deduped} ({status})")
        else:
            log.info(f"[Prepare] {label} pending: 0 -> 0 (empty)")


def _unify_record(rec: dict, platform: str) -> dict:
    """
    将不同平台的语料记录统一为标准格式。
    标准字段：sender, text, platform, timestamp, name
    """
    sender = rec.get("sender", "")
    # 兼容新旧字段
    if "text" not in rec:
        for k in ("message", "content", "msg"):
            if k in rec:
                rec["text"] = rec[k]
                break

    return {
        "sender":    sender if sender in ("me", "them") else "them",
        "text":      rec.get("text", ""),
        "platform":  platform,
        "timestamp": rec.get("timestamp", ""),
        "match_id":  rec.get("match_id", ""),
        "message_index": rec.get("message_index", ""),
        "name":      rec.get("name", rec.get("match_name", "")),
        "bio":       rec.get("bio", rec.get("match_bio", "")),
        "source_record_type": rec.get("record_type", ""),
        "reply": rec.get("reply", ""),
        "intent": rec.get("intent", ""),
        "outcome": rec.get("outcome", ""),
        "outcome_label": rec.get("outcome_label", ""),
        "feedback_event": rec.get("feedback_event", ""),
        "feedback_reason": rec.get("feedback_reason", ""),
    }


def _iter_unified_records(rec: dict, platform: str) -> Iterator[dict]:
    """兼容单消息记录与 conversations/messages 记录。"""
    messages = rec.get("messages", [])
    if isinstance(messages, list) and messages:
        base_name = rec.get("match_name") or rec.get("name") or "unknown"
        base_bio = rec.get("bio", rec.get("match_bio", ""))
        base_timestamp = rec.get("timestamp", "")
        match_id = rec.get("match_id", "")
        base_meta = {
            "source_record_type": rec.get("record_type", ""),
            "reply": rec.get("reply", ""),
            "intent": rec.get("intent", ""),
            "outcome": rec.get("outcome", ""),
            "outcome_label": rec.get("outcome_label", ""),
            "feedback_event": rec.get("feedback_event", ""),
            "feedback_reason": rec.get("feedback_reason", ""),
        }
        yielded_texts: list[str] = []

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            text = (
                message.get("text")
                or message.get("message")
                or message.get("content")
                or message.get("msg")
                or ""
            ).strip()
            if not text:
                continue

            sender = message.get("sender", "")
            if sender not in ("me", "them"):
                sender = "me" if message.get("is_mine") else "them"
            yielded_texts.append(text)

            yield {
                "sender": sender,
                "text": text,
                "platform": platform,
                "timestamp": message.get("timestamp", base_timestamp),
                "match_id": match_id,
                "message_index": index,
                "name": base_name,
                "bio": base_bio,
                **base_meta,
            }
        reply = str(rec.get("reply", "") or "").strip()
        if reply and reply not in yielded_texts:
            yield {
                "sender": "me",
                "text": reply,
                "platform": platform,
                "timestamp": base_timestamp,
                "match_id": match_id,
                "message_index": len(yielded_texts),
                "name": base_name,
                "bio": base_bio,
                **base_meta,
            }
        return

    unified = _unify_record(rec, platform)
    if not unified.get("text", "").strip() and unified.get("reply", "").strip():
        unified["sender"] = "me"
        unified["text"] = unified.get("reply", "").strip()
    if unified.get("text", "").strip():
        yield unified


def _hash_record(rec: dict) -> str:
    """计算去重哈希，保留平台/对象/时间维度，避免误伤常见短句。"""
    key = json.dumps({
        "platform": rec.get("platform", ""),
        "name": rec.get("name", ""),
        "match_id": rec.get("match_id", ""),
        "timestamp": rec.get("timestamp", ""),
        "message_index": rec.get("message_index", ""),
        "sender": rec.get("sender", ""),
        "text": rec.get("text", ""),
        "source_record_type": rec.get("source_record_type", ""),
        "intent": rec.get("intent", ""),
        "outcome_label": rec.get("outcome_label", ""),
        "feedback_event": rec.get("feedback_event", ""),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def step_merge() -> int:
    """
    合并两端 pending_corpus.jsonl，写入 unified_corpus.jsonl。
    返回合并后的记录数。
    """
    seen: set[str] = set()
    total = 0
    written = 0
    raw_stats: dict[str, Counter] = defaultdict(Counter)
    unified_total_stats: dict[str, Counter] = defaultdict(Counter)
    unified_written_stats: dict[str, Counter] = defaultdict(Counter)

    for existing in _iter_jsonl(UNIFIED_CORPUS):
        seen.add(_hash_record(existing))

    for platform, path in [("tinder", TINDER_CORPUS), ("bumble", BUMBLE_CORPUS)]:
        for rec in _iter_jsonl(path):
            record_type = rec.get("record_type", "unknown") or "unknown"
            raw_stats[platform][record_type] += 1
            for unified in _iter_unified_records(rec, platform):
                source_record_type = unified.get("source_record_type", "unknown") or "unknown"
                h = _hash_record(unified)
                total += 1
                unified_total_stats[platform][source_record_type] += 1
                if h in seen:
                    continue
                seen.add(h)

                with open(UNIFIED_CORPUS, "a", encoding="utf-8") as f:
                    f.write(json.dumps(unified, ensure_ascii=False) + "\n")
                    written += 1
                    unified_written_stats[platform][source_record_type] += 1

    log.info(f"[Merge] 合并完成，写入 {written} 条新记录（原始 {total} 条，去重后）")
    for platform in ("tinder", "bumble"):
        raw_counter = raw_stats.get(platform, Counter())
        total_counter = unified_total_stats.get(platform, Counter())
        written_counter = unified_written_stats.get(platform, Counter())
        raw_summary = ", ".join(f"{k}={v}" for k, v in sorted(raw_counter.items())) or "none"
        total_summary = ", ".join(f"{k}={v}" for k, v in sorted(total_counter.items())) or "none"
        written_summary = ", ".join(f"{k}={v}" for k, v in sorted(written_counter.items())) or "none"
        log.info(
            f"[Merge] {platform}: raw_pending[{raw_summary}] | "
            f"unified_total[{total_summary}] | unified_written[{written_summary}]"
        )
    return written


# ─────────────────────────────────────────────────────────────────
# Step 3: NotebookLM 演化
# ─────────────────────────────────────────────────────────────────
def step_notebooklm_upload() -> bool:
    """将 unified_corpus 真实同步为 NotebookLM 网页来源。"""
    if not NBLM_SYNCER.exists():
        log.error(f"[NBLM] 同步器不存在: {NBLM_SYNCER}")
        return False

    if not UNIFIED_CORPUS.exists() or UNIFIED_CORPUS.stat().st_size == 0:
        log.warning("[NBLM] 合流语料为空，跳过上传")
        return False

    log.info("[NBLM] 开始同步 NotebookLM 网页来源...")
    try:
        result = subprocess.run(
            [sys.executable, str(NBLM_SYNCER),
             "--corpus", str(UNIFIED_CORPUS),
             "--result", str(NBLM_SYNC_RESULT),
             "--json"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode == 0:
            detail = (result.stdout or "").strip()
            log.info(f"[NBLM] ✅ 网页来源同步成功: {detail[-500:]}")
            return True
        else:
            detail = (result.stderr or result.stdout or "").strip()
            log.error(f"[NBLM] ❌ 网页来源同步失败: {detail[-500:]}")
            return False
    except subprocess.TimeoutExpired:
        log.error("[NBLM] ⏱ 网页来源同步超时")
        return False
    except Exception as e:
        log.error(f"[NBLM] ❌ 网页来源同步异常: {e}")
        return False


def step_notebooklm_analyze() -> bool:
    """调用分析生成 review draft。"""
    if not NBLM_UPLOADER.exists():
        log.error(f"[NBLM] 分析器不存在: {NBLM_UPLOADER}")
        return False

    log.info("[NBLM] 开始分析生成策略...")
    try:
        result = subprocess.run(
            [sys.executable, str(NBLM_UPLOADER), "--analyze",
             "--corpus", str(UNIFIED_CORPUS),
             "--notebooklm-context", str(NBLM_CONTEXT),
             "--output", str(REVIEW_CFG)],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            log.info(f"[NBLM] ✅ review draft 生成成功: {REVIEW_CFG}")
            return True
        else:
            detail = (result.stderr or result.stdout or "").strip()
            log.error(f"[NBLM] ❌ 策略生成失败: {detail[-400:]}")
            return False
    except subprocess.TimeoutExpired:
        log.error("[NBLM] ⏱ 分析超时")
        return False
    except Exception as e:
        log.error(f"[NBLM] ❌ 分析异常: {e}")
        return False


def step_notebooklm_context() -> bool:
    """从当前最新 NotebookLM source 拉取 guide/定向 ask，作为本地演化辅助上下文。"""
    if not NBLM_CONTEXT_FETCHER.exists():
        log.error(f"[NBLM] Context fetcher 不存在: {NBLM_CONTEXT_FETCHER}")
        return False
    if not NBLM_SYNC_RESULT.exists():
        log.error(f"[NBLM] 同步结果不存在: {NBLM_SYNC_RESULT}")
        return False

    log.info("[NBLM] 开始拉取 NotebookLM source guide / strategy notes...")
    try:
        result = subprocess.run(
            [sys.executable, str(NBLM_CONTEXT_FETCHER),
             "--sync-result", str(NBLM_SYNC_RESULT),
             "--output", str(NBLM_CONTEXT),
             "--json"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            detail = (result.stdout or "").strip()
            _merge_runtime_feedback_context()
            log.info(f"[NBLM] ✅ 辅助上下文生成成功: {detail[-500:]}")
            return True
        detail = (result.stderr or result.stdout or "").strip()
        log.error(f"[NBLM] ❌ 辅助上下文生成失败: {detail[-500:]}")
        return False
    except subprocess.TimeoutExpired:
        log.error("[NBLM] ⏱ 辅助上下文生成超时")
        return False
    except Exception as e:
        log.error(f"[NBLM] ❌ 辅助上下文生成异常: {e}")
        return False


def _merge_runtime_feedback_context() -> None:
    """把运行时真实结果并入 notebooklm_context.json，供后续分析参考。"""
    if not NBLM_CONTEXT.exists():
        return
    try:
        payload = json.loads(NBLM_CONTEXT.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    summary = build_runtime_feedback_summary()
    if not summary.get("text"):
        payload.pop("runtime_feedback_summary", None)
        payload.pop("runtime_feedback_summary_text", None)
        NBLM_CONTEXT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    payload["runtime_feedback_summary"] = summary
    payload["runtime_feedback_summary_text"] = summary.get("text", "")
    NBLM_CONTEXT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────
# Step 4: 自动审核 + 策略同步
# ─────────────────────────────────────────────────────────────────
def _looks_meta_pattern(text: str) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return True
    if re.match(r"^(对方|聊到|当对方|如果对方|回应|话题里)", candidate):
        return True
    meta_phrases = ("触发场景", "错误做法", "推进见面", "旅行话题", "好奇心问题", "开玩笑自称")
    return any(token in candidate for token in meta_phrases)


def _looks_meta_example(text: str) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return True
    meta_phrases = ("让对方", "继续互动", "调侃式", "名字+", "模板", "推进话题", "互动感")
    return any(token in candidate for token in meta_phrases)


def _pattern_bucket(pattern: str, example: str, why_it_works: str = "") -> str:
    text = " ".join(str(x or "") for x in (pattern, example, why_it_works))
    lowered = text.lower()
    if any(token in pattern for token in ("嗨", "你好", "hi", "hey", "hiii")):
        return "opener"
    if any(token in pattern for token in ("为什么", "怎么", "啥", "吗", "?", "？")):
        return "question_hook"
    if any(token in pattern for token in ("不好意思", "抱歉", "修炼", "消失", "忙")):
        return "reengage_after_gap"
    if any(token in text for token in ("原谅", "喜欢", "领证", "下一个", "约", "咖啡", "见面")):
        return "relationship_push"
    if any(token in text for token in ("哈哈", "emoji", "调侃", "悬念", "玩笑")):
        return "playful_banter"
    if any(token in lowered for token in ("english", "english reply", "hey", "hmm", "hiii")):
        return "english_or_crosslang"
    return "generic"


def _topic_bucket(pattern: str, example: str, why_it_works: str = "") -> str:
    text = " ".join(str(x or "") for x in (pattern, example, why_it_works))
    if any(token in text for token in ("圣经", "基督", "神吗", "信仰", "教会", "宗教")):
        return "faith"
    if any(token in text for token in ("古典音乐", "乐章", "富特文格勒")):
        return "classical_music"
    if any(token in text for token in ("AI", "bug", "Bug", "调试")):
        return "ai_meta"
    if any(token in text for token in ("前女友", "前任")):
        return "ex_relationship"
    return "generic"


def _parse_generated_at(raw_value: str) -> datetime | None:
    candidate = str(raw_value or "").strip()
    if not candidate:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def _load_notebook_expectation() -> tuple[str, str]:
    payload = {}
    for path in (NBLM_CONTEXT, NBLM_SYNC_RESULT):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            break
    if not isinstance(payload, dict):
        return "", ""
    return str(payload.get("source_id", "") or ""), str(payload.get("source_title", "") or "")


def _load_runtime_feedback_summary() -> dict:
    if not NBLM_CONTEXT.exists():
        return {}
    try:
        payload = json.loads(NBLM_CONTEXT.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("runtime_feedback_summary", {})
    return summary if isinstance(summary, dict) else {}


def _is_complete_runtime_feedback_snapshot(snapshot: dict | None) -> bool:
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    required_fields = (
        "generated_at",
        "total_events",
        "weighted_sent_total",
        "weighted_failed_total",
        "weighted_net_score",
        "text",
    )
    for field_name in required_fields:
        if field_name not in snapshot:
            return False
    return True


def _is_valid_failure_pattern(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    pattern = str(item.get("pattern", "")).strip()
    root_cause = str(item.get("root_cause", "")).strip()
    example = str(item.get("example", "")).strip()
    if not pattern or not root_cause or not example:
        return False
    if _looks_meta_pattern(pattern) or _looks_meta_example(example):
        return False
    return True


def _runtime_feedback_review(summary: dict) -> tuple[bool, str]:
    if not isinstance(summary, dict):
        return True, "无运行反馈门槛"

    total_events = int(summary.get("total_events", 0) or 0)
    if total_events < 4:
        return True, f"运行反馈样本不足（events={total_events}），跳过反馈门槛"

    weighted_sent = float(summary.get("weighted_sent_total", 0.0) or 0.0)
    weighted_failed = float(summary.get("weighted_failed_total", 0.0) or 0.0)
    weighted_net = float(summary.get("weighted_net_score", 0.0) or 0.0)
    event_scores = summary.get("weighted_event_scores", {}) or {}

    hard_negative = 0.0
    no_safe_negative = 0.0
    hard_negative_events = ("reply_send_failed", "reactivation_send_failed", "queue_generation_failed")
    no_safe_events = ("reply_no_safe_reply", "reactivation_no_safe_reply", "queue_no_safe_reply", "opener_no_safe_reply")
    for event in hard_negative_events:
        hard_negative += abs(min(float(event_scores.get(event, 0.0) or 0.0), 0.0))
    for event in no_safe_events:
        no_safe_negative += abs(min(float(event_scores.get(event, 0.0) or 0.0), 0.0))

    success_unit = max(EVENT_BASE_WEIGHTS.get("reply_sent", 1.0), EVENT_BASE_WEIGHTS.get("reactivation_sent", 0.9), 0.1)
    hard_negative_unit = max(abs(EVENT_BASE_WEIGHTS.get(event, 0.0)) for event in hard_negative_events)
    no_safe_unit = max(abs(EVENT_BASE_WEIGHTS.get(event, 0.0)) for event in no_safe_events)
    min_sent_for_hard_negative = round(success_unit * 0.3, 3)
    hard_negative_threshold = round(hard_negative_unit * 0.9, 3)
    net_negative_threshold = -round(max(hard_negative_unit * 0.45, no_safe_unit * 0.7), 3)
    negative_margin = round(success_unit * 0.5, 3)

    if weighted_sent <= min_sent_for_hard_negative and hard_negative >= hard_negative_threshold:
        return False, (
            "近期运行反馈显示发送层负反馈过强"
            f"（weighted_sent={weighted_sent:.2f}, hard_negative={hard_negative:.2f}）"
        )

    if weighted_net < net_negative_threshold and (hard_negative + no_safe_negative) > (weighted_sent + negative_margin):
        return False, (
            "近期运行反馈净分为负，且失败/无安全回复明显压过成功"
            f"（net={weighted_net:.2f}, sent={weighted_sent:.2f}, "
            f"hard_negative={hard_negative:.2f}, no_safe={no_safe_negative:.2f}）"
        )

    return True, (
        "运行反馈门槛通过"
        f"（events={total_events}, net={weighted_net:.2f}, sent={weighted_sent:.2f}, "
        f"hard_negative={hard_negative:.2f}, no_safe={no_safe_negative:.2f}）"
    )


def _auto_review_strategy(started_at_ts: float, expected_source_id: str = "", expected_source_title: str = "") -> tuple[bool, str]:
    if not REVIEW_CFG.exists():
        return False, "review draft 不存在"

    try:
        strategy = json.loads(REVIEW_CFG.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"review draft 读取失败: {e}"

    allowed_root_keys = {
        "version",
        "generated_at",
        "source",
        "success_patterns",
        "failure_patterns",
        "notebook_id",
        "notebooklm_source_id",
        "notebooklm_source_title",
        "runtime_feedback_snapshot",
    }
    unknown_root_keys = sorted(set(strategy.keys()) - allowed_root_keys)
    if unknown_root_keys:
        return False, f"review draft 含未知根字段: {', '.join(unknown_root_keys)}"

    required_string_fields = (
        "version",
        "generated_at",
        "source",
        "notebook_id",
        "notebooklm_source_id",
        "notebooklm_source_title",
    )
    for field_name in required_string_fields:
        value = strategy.get(field_name, "")
        if not isinstance(value, str) or not value.strip():
            return False, f"review draft 字段非法或为空: {field_name}"

    if not isinstance(strategy.get("success_patterns"), list):
        return False, "review draft success_patterns 类型非法"
    if not isinstance(strategy.get("failure_patterns"), list):
        return False, "review draft failure_patterns 类型非法"
    failure_patterns = strategy.get("failure_patterns") or []
    approved_failure_patterns = [item for item in failure_patterns if _is_valid_failure_pattern(item)]
    if len(approved_failure_patterns) < 1:
        return False, "failure_patterns 为空或未通过本地质量审核"

    runtime_feedback_snapshot = strategy.get("runtime_feedback_snapshot")
    if runtime_feedback_snapshot is not None and not isinstance(runtime_feedback_snapshot, dict):
        return False, "review draft runtime_feedback_snapshot 类型非法"
    if runtime_feedback_snapshot is not None and not _is_complete_runtime_feedback_snapshot(runtime_feedback_snapshot):
        return False, "review draft runtime_feedback_snapshot 缺少必需字段"

    review_mtime = REVIEW_CFG.stat().st_mtime
    if review_mtime + 2 < started_at_ts:
        return False, "review draft 不是本轮新生成产物（mtime 早于本轮启动时间）"

    generated_at = _parse_generated_at(strategy.get("generated_at", ""))
    if generated_at is None:
        return False, "review draft 缺少可解析 generated_at"
    if generated_at.timestamp() + 2 < started_at_ts:
        return False, f"review draft 不是本轮新生成产物（generated_at={strategy.get('generated_at', '')}）"

    if expected_source_id:
        actual_source_id = str(strategy.get("notebooklm_source_id", "") or "")
        if actual_source_id != expected_source_id:
            return False, f"review draft source_id 不匹配（expected={expected_source_id}, actual={actual_source_id or 'empty'}）"

    if expected_source_title:
        actual_source_title = str(strategy.get("notebooklm_source_title", "") or "")
        if actual_source_title != expected_source_title:
            return False, f"review draft source_title 不匹配（expected={expected_source_title}, actual={actual_source_title or 'empty'}）"

    success_patterns = strategy.get("success_patterns") or []
    if not success_patterns:
        return False, "success_patterns 为空"

    approved = []
    buckets: set[str] = set()
    topic_buckets: list[str] = []
    for item in success_patterns:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", "")).strip()
        example = str(item.get("example", "")).strip()
        why_it_works = str(item.get("why_it_works", "")).strip()
        if not pattern or not example:
            continue
        if _looks_meta_pattern(pattern):
            continue
        if _looks_meta_example(example):
            continue
        approved.append(item)
        buckets.add(_pattern_bucket(pattern, example, why_it_works))
        topic_buckets.append(_topic_bucket(pattern, example, why_it_works))

    if not approved:
        return False, "success_patterns 未通过本地质量审核"
    if len(approved) < 3:
        return False, f"success_patterns 覆盖过窄（approved={len(approved)} < 3）"
    if len(buckets) < 2:
        return False, f"success_patterns 场景过窄（bucket_count={len(buckets)}）"
    non_generic_topics = {bucket for bucket in topic_buckets if bucket != "generic"}
    if non_generic_topics and len(non_generic_topics) == 1 and len(non_generic_topics) == len(set(topic_buckets)):
        return False, f"success_patterns 过度聚焦单一窄话题（topic={next(iter(non_generic_topics))}）"
    runtime_feedback_ok, runtime_feedback_reason = _runtime_feedback_review(_load_runtime_feedback_summary())
    if not runtime_feedback_ok:
        return False, runtime_feedback_reason
    return True, (
        f"通过自动审核（approved_success_patterns={len(approved)}, "
        f"bucket_count={len(buckets)}, runtime_feedback={runtime_feedback_reason})"
    )


def step_sync_strategy() -> bool:
    """
    将生成的统一策略同步到当前实际消费的 strategy_config.json。
    """
    if not REVIEW_CFG.exists():
        log.warning("[Sync] review draft 不存在，跳过同步")
        return False

    with open(REVIEW_CFG, "r", encoding="utf-8") as f:
        new_strategy = json.load(f)

    targets = {
        "shared": UNIFIED_CFG,
        "tinder": TINDER_DIR / "strategy_config.json",
    }

    all_ok = True
    for name, path in targets.items():
        try:
            old = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            if "runtime_feedback_snapshot" in new_strategy:
                new_snapshot = new_strategy.get("runtime_feedback_snapshot")
                if not _is_complete_runtime_feedback_snapshot(new_snapshot):
                    if old.get("runtime_feedback_snapshot") is not None:
                        log.warning("[Sync] runtime_feedback_snapshot 不完整，保留旧值")
                    new_strategy = {k: v for k, v in new_strategy.items() if k != "runtime_feedback_snapshot"}
            # 合并：保留平台特有的字段（profile 等）
            merged = {**old, **new_strategy}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            log.info(f"[Sync] ✅ {name}: {path}")
        except Exception as e:
            log.error(f"[Sync] ❌ {name}: {e}")
            all_ok = False

    return all_ok


# ─────────────────────────────────────────────────────────────────
# 清理
# ─────────────────────────────────────────────────────────────────
def step_cleanup():
    """清空各平台的 pending_corpus.jsonl（已合流的记录）"""
    for path in [TINDER_CORPUS, BUMBLE_CORPUS]:
        if path.exists():
            path.unlink()
            log.info(f"[Cleanup] 已清空 {path.name}")


# ─────────────────────────────────────────────────────────────────
# 主流水线
# ─────────────────────────────────────────────────────────────────
def run_pipeline(full: bool = True):
    """
    执行完整（或仅增量）演化流水线。

    参数
    ----
    full : True  = 全量采集 + 合流 + NBLM
           False = 仅 NBLM 分析（使用已有 unified_corpus.jsonl）
    """
    log.info("=" * 50)
    log.info(f"演化流水线启动 (full={full}) @ {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info("=" * 50)
    started_at_ts = time.time()

    # 清空旧的 unified corpus（重新开始）
    if full and UNIFIED_CORPUS.exists():
        UNIFIED_CORPUS.unlink()

    if full:
        # Step 1: 采集
        step_scrape("tinder")
        step_scrape("bumble")

        # Step 1.5: 预清洗 pending
        step_prepare_pending()

        # Step 2: 合流
        record_count = step_merge()
        if record_count == 0:
            log.warning("[Pipeline] 无新语料，跳过 NotebookLM")
            return

    # Step 3: NotebookLM
    nblm_ok = False
    if UNIFIED_CORPUS.exists() and UNIFIED_CORPUS.stat().st_size > 0:
        upload_ok = step_notebooklm_upload()
        if not upload_ok:
            log.error("[Pipeline] NotebookLM 上传/预检失败，停止后续同步")
            return False

        context_ok = step_notebooklm_context()
        if not context_ok:
            log.error("[Pipeline] NotebookLM 辅助上下文拉取失败，停止后续同步")
            return False

        analyze_ok = step_notebooklm_analyze()
        if not analyze_ok:
            log.error("[Pipeline] NotebookLM 分析失败，停止后续同步")
            return False
        nblm_ok = True
    else:
        log.warning("[Pipeline] unified_corpus.jsonl 为空，跳过 NBLM")
        return False

    # Step 4: 自动审核后自动同步生产策略
    if nblm_ok:
        expected_source_id, expected_source_title = _load_notebook_expectation()
        approved, reason = _auto_review_strategy(
            started_at_ts,
            expected_source_id=expected_source_id,
            expected_source_title=expected_source_title,
        )
        if not approved:
            log.error(f"[Pipeline] 自动审核未通过，停止覆盖生产策略: {reason}")
            return False
        log.info(f"[Pipeline] 自动审核通过: {reason}")
        if not step_sync_strategy():
            log.error("[Pipeline] 生产策略自动覆盖失败")
            return False
        log.info("[Pipeline] NotebookLM 网页来源已同步，生产策略已自动覆盖")

    if full and nblm_ok:
        step_cleanup()

    log.info("=" * 50)
    log.info(f"演化流水线完成 @ {datetime.now():%Y-%m-%d %H:%M:%S}")
    log.info("=" * 50)
    return True


# ─────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="深夜演化流水线")
    parser.add_argument("--incremental", action="store_true",
                        help="仅执行 NBLM 分析，不重新采集")
    args = parser.parse_args()

    run_pipeline(full=not args.incremental)
