#!/usr/bin/env python3
"""
corpus_feedback.py
数据回流埋点 — 每次对话结束时调用，写入待处理队列
"""
import json
import time
from pathlib import Path
from datetime import datetime

PENDING_FILE = Path(__file__).parent / "pending_corpus.jsonl"


def append_conversation(conversation: dict):
    """
    追加单条对话到待处理队列
    conversation = {
        "match_name": "Liya",
        "match_id": "xxx",
        "messages": [{"sender": "me", "text": "xxx"}, ...],
        "outcome": "success" | "fail" | "unknown",
        "timestamp": "2026-04-16T19:08:00"
    }
    """
    entry = {
        **conversation,
        "platform": conversation.get("platform", "tinder"),
        "intent": conversation.get("intent", "reply"),
        "outcome": conversation.get("outcome", "unknown"),
        "outcome_label": conversation.get("outcome_label"),
        "feedback_event": conversation.get("feedback_event"),
        "feedback_reason": conversation.get("feedback_reason"),
        "added_at": datetime.now().isoformat(),
    }

    with open(PENDING_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    print(f"[feedback] 写入 pending, 当前队列: {queue_size()} 条")


def queue_size() -> int:
    if not PENDING_FILE.exists():
        return 0
    with open(PENDING_FILE, 'r', encoding='utf-8') as f:
        return sum(1 for _ in f)


def read_pending():
    """读取所有待处理记录"""
    if not PENDING_FILE.exists():
        return []
    records = []
    with open(PENDING_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def clear_pending():
    """清空已处理的队列（合并后调用）"""
    PENDING_FILE.write_text('', encoding='utf-8')
    print("[feedback] 队列已清空")


if __name__ == '__main__':
    # 测试
    append_conversation({
        "match_name": "测试用户",
        "match_id": "test_001",
        "messages": [
            {"sender": "them", "text": "你好啊"},
            {"sender": "me", "text": "不穿"},
        ],
        "outcome": "unknown",
    })
    print(f"队列大小: {queue_size()}")
