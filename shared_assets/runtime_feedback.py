#!/usr/bin/env python3
"""
runtime_feedback.py
===================
统一记录运行时发送/跳过结果，并为演化链路提供简洁摘要。
"""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_FEEDBACK_FILE = SCRIPT_DIR / "runtime_feedback.jsonl"
RUNTIME_FEEDBACK_RETENTION_DAYS = 30
RUNTIME_FEEDBACK_MAX_RECORDS = 5000
RUNTIME_FEEDBACK_PRUNE_BYTES = 2 * 1024 * 1024

MAX_TEXT_LEN = 120
MAX_CONTEXT_ITEMS = 3
WEIGHT_HALF_LIFE_HOURS = 72.0

EVENT_BASE_WEIGHTS = {
    "opener_sent": 0.8,
    "reply_sent": 1.0,
    "reactivation_sent": 0.9,
    "opener_no_safe_reply": -0.35,
    "reply_no_safe_reply": -0.55,
    "reactivation_no_safe_reply": -0.45,
    "reply_business_skipped": -0.25,
    "reactivation_skipped": -0.2,
    "opener_send_failed": -0.7,
    "reply_send_failed": -0.9,
    "reactivation_send_failed": -0.85,
    "queue_no_safe_reply": -0.3,
    "queue_generation_failed": -0.7,
    "partner_followup_low_info": -0.45,
    "partner_followup_basic": 0.25,
    "partner_followup_question": 0.65,
    "partner_followup_engaged": 0.85,
}


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clean_text(text: Any, limit: int = MAX_TEXT_LEN) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def _compact_messages(messages: list[dict] | None, limit: int = MAX_CONTEXT_ITEMS) -> list[str]:
    result: list[str] = []
    for msg in list(messages or [])[-limit:]:
        if not isinstance(msg, dict):
            continue
        sender = "Me" if msg.get("sender") == "me" or msg.get("is_mine") else "Them"
        text = _clean_text(msg.get("text", ""))
        if text:
            result.append(f"{sender}: {text}")
    return result


def _age_decay(hours_ago: float) -> float:
    if hours_ago <= 0:
        return 1.0
    return 0.5 ** (hours_ago / WEIGHT_HALF_LIFE_HOURS)


def _weighted_value(event: str, hours_ago: float) -> float:
    base = EVENT_BASE_WEIGHTS.get(event, 0.0)
    return base * _age_decay(hours_ago)


def _prune_feedback_file(
    path: Path = RUNTIME_FEEDBACK_FILE,
    *,
    retention_days: int = RUNTIME_FEEDBACK_RETENTION_DAYS,
    max_records: int = RUNTIME_FEEDBACK_MAX_RECORDS,
) -> None:
    if not path.exists():
        return

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    kept: list[dict[str, Any]] = []
    for rec in _iter_feedback_records(path):
        raw_ts = str(rec.get("timestamp", "") or "")
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", ""))
        except Exception:
            continue
        if ts < cutoff:
            continue
        kept.append(rec)

    if len(kept) > max_records:
        kept = kept[-max_records:]

    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        for rec in kept:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    temp_path.replace(path)


def record_runtime_feedback(
    platform: str,
    match_id: str,
    match_name: str,
    event: str,
    *,
    intent: str = "",
    reason: str = "",
    reply: str = "",
    messages: list[dict] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload = {
        "timestamp": _utc_now_iso(),
        "platform": str(platform or "").strip(),
        "match_id": str(match_id or "").strip(),
        "match_name": str(match_name or "").strip(),
        "event": str(event or "").strip(),
        "intent": str(intent or "").strip(),
        "reason": _clean_text(reason, 180),
        "reply": _clean_text(reply, 160),
        "context_tail": _compact_messages(messages),
        "metadata": metadata or {},
    }
    try:
        with open(RUNTIME_FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        try:
            if RUNTIME_FEEDBACK_FILE.stat().st_size >= RUNTIME_FEEDBACK_PRUNE_BYTES:
                _prune_feedback_file()
        except Exception:
            pass
    except Exception:
        pass


def _iter_feedback_records(path: Path = RUNTIME_FEEDBACK_FILE):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if isinstance(payload, dict):
                yield payload


def build_runtime_feedback_summary(
    *,
    path: Path = RUNTIME_FEEDBACK_FILE,
    window_days: int = 7,
    max_examples: int = 8,
) -> dict[str, Any]:
    try:
        _prune_feedback_file(path)
    except Exception:
        pass

    cutoff = datetime.utcnow() - timedelta(days=window_days)
    records: list[dict[str, Any]] = []
    for rec in _iter_feedback_records(path):
        raw_ts = str(rec.get("timestamp", "") or "")
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", ""))
        except Exception:
            continue
        if ts < cutoff:
            continue
        records.append(rec)

    sent_events = {"opener_sent", "reply_sent", "reactivation_sent"}
    failed_events = {"opener_send_failed", "reply_send_failed", "reactivation_send_failed"}
    skipped_events = {
        "opener_no_safe_reply",
        "reply_no_safe_reply",
        "reply_business_skipped",
        "reactivation_no_safe_reply",
        "reactivation_skipped",
    }

    counts_by_event = Counter()
    counts_by_platform = Counter()
    skip_reasons = Counter()
    fail_reasons = Counter()
    recent_examples: list[str] = []
    weighted_events: dict[str, float] = {}
    weighted_platforms: dict[str, float] = {}
    weighted_intents: dict[str, float] = {}
    positive_examples: list[tuple[float, str]] = []
    negative_examples: list[tuple[float, str]] = []
    weighted_sent_total = 0.0
    weighted_skipped_total = 0.0
    weighted_failed_total = 0.0

    for rec in records:
        event = str(rec.get("event", "") or "")
        platform = str(rec.get("platform", "") or "")
        intent = str(rec.get("intent", "") or "")
        reason = str(rec.get("reason", "") or "")
        match_name = str(rec.get("match_name", "") or "") or str(rec.get("match_id", "") or "")
        raw_ts = str(rec.get("timestamp", "") or "")
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", ""))
        except Exception:
            ts = datetime.utcnow()
        hours_ago = max((datetime.utcnow() - ts).total_seconds() / 3600.0, 0.0)
        weighted = _weighted_value(event, hours_ago)
        counts_by_event[event] += 1
        counts_by_platform[platform] += 1
        if event:
            weighted_events[event] = weighted_events.get(event, 0.0) + weighted
        if platform:
            weighted_platforms[platform] = weighted_platforms.get(platform, 0.0) + weighted
        if intent:
            weighted_intents[intent] = weighted_intents.get(intent, 0.0) + weighted

        if event in sent_events:
            weighted_sent_total += max(weighted, 0.0)
        elif event in skipped_events:
            weighted_skipped_total += abs(weighted)
        elif event in failed_events:
            weighted_failed_total += abs(weighted)

        if event in skipped_events and reason:
            skip_reasons[reason] += 1
        if event in failed_events and reason:
            fail_reasons[reason] += 1

        if len(recent_examples) < max_examples:
            reply = str(rec.get("reply", "") or "")
            intent = str(rec.get("intent", "") or "")
            context_tail = rec.get("context_tail", []) or []
            parts = [f"[{platform}:{match_name}] {event}"]
            if intent:
                parts.append(f"intent={intent}")
            if reason:
                parts.append(f"reason={reason}")
            if reply:
                parts.append(f"reply={reply}")
            if context_tail:
                parts.append(f"context={' | '.join(context_tail)}")
            example_text = " ; ".join(parts)
            recent_examples.append(example_text)

        if weighted > 0:
            positive_examples.append((weighted, f"[{platform}:{match_name}] {event}"))
        elif weighted < 0:
            negative_examples.append((abs(weighted), f"[{platform}:{match_name}] {event}"))

    sent_total = sum(counts_by_event[e] for e in sent_events)
    failed_total = sum(counts_by_event[e] for e in failed_events)
    skipped_total = sum(counts_by_event[e] for e in skipped_events)
    weighted_net_score = weighted_sent_total - weighted_skipped_total - weighted_failed_total

    def _format_weighted(mapping: dict[str, float], *, positive_only: bool = False, negative_only: bool = False, limit: int = 6) -> str:
        items = []
        for key, value in mapping.items():
            if positive_only and value <= 0:
                continue
            if negative_only and value >= 0:
                continue
            items.append((key, value))
        items.sort(key=lambda item: abs(item[1]), reverse=True)
        return ", ".join(f"{k}={round(v, 2)}" for k, v in items[:limit])

    lines = [
        f"- lookback_days: {window_days}",
        f"- total_events: {len(records)}",
        f"- sent_total: {sent_total}",
        f"- skipped_total: {skipped_total}",
        f"- failed_total: {failed_total}",
        f"- weighted_sent_total: {round(weighted_sent_total, 2)}",
        f"- weighted_skipped_total: {round(weighted_skipped_total, 2)}",
        f"- weighted_failed_total: {round(weighted_failed_total, 2)}",
        f"- weighted_net_score: {round(weighted_net_score, 2)}",
    ]
    if counts_by_platform:
        platform_bits = ", ".join(f"{k}={v}" for k, v in counts_by_platform.most_common())
        lines.append(f"- platform_counts: {platform_bits}")
    if counts_by_event:
        event_bits = ", ".join(f"{k}={v}" for k, v in counts_by_event.most_common(8))
        lines.append(f"- top_events: {event_bits}")
    weighted_positive_events = _format_weighted(weighted_events, positive_only=True)
    weighted_negative_events = _format_weighted(weighted_events, negative_only=True)
    if weighted_positive_events:
        lines.append(f"- weighted_positive_events: {weighted_positive_events}")
    if weighted_negative_events:
        lines.append(f"- weighted_negative_events: {weighted_negative_events}")
    weighted_intent_bits = _format_weighted(weighted_intents)
    if weighted_intent_bits:
        lines.append(f"- weighted_intents: {weighted_intent_bits}")
    weighted_platform_bits = _format_weighted(weighted_platforms)
    if weighted_platform_bits:
        lines.append(f"- weighted_platforms: {weighted_platform_bits}")
    if skip_reasons:
        lines.append(
            "- top_skip_reasons: "
            + ", ".join(f"{k}={v}" for k, v in skip_reasons.most_common(6))
        )
    if fail_reasons:
        lines.append(
            "- top_fail_reasons: "
            + ", ".join(f"{k}={v}" for k, v in fail_reasons.most_common(6))
        )
    if recent_examples:
        lines.append("【近期运行样本】")
        lines.extend(recent_examples)
    if positive_examples:
        lines.append("【近期高权重正样本】")
        for _, text in sorted(positive_examples, key=lambda item: item[0], reverse=True)[:4]:
            lines.append(text)
    if negative_examples:
        lines.append("【近期高权重负样本】")
        for _, text in sorted(negative_examples, key=lambda item: item[0], reverse=True)[:4]:
            lines.append(text)

    return {
        "generated_at": _utc_now_iso(),
        "lookback_days": window_days,
        "total_events": len(records),
        "sent_total": sent_total,
        "skipped_total": skipped_total,
        "failed_total": failed_total,
        "weighted_sent_total": round(weighted_sent_total, 4),
        "weighted_skipped_total": round(weighted_skipped_total, 4),
        "weighted_failed_total": round(weighted_failed_total, 4),
        "weighted_net_score": round(weighted_net_score, 4),
        "platform_counts": dict(counts_by_platform),
        "event_counts": dict(counts_by_event),
        "weighted_event_scores": {k: round(v, 4) for k, v in weighted_events.items()},
        "weighted_platform_scores": {k: round(v, 4) for k, v in weighted_platforms.items()},
        "weighted_intent_scores": {k: round(v, 4) for k, v in weighted_intents.items()},
        "skip_reasons": dict(skip_reasons),
        "fail_reasons": dict(fail_reasons),
        "examples": recent_examples,
        "text": "\n".join(lines).strip(),
    }
