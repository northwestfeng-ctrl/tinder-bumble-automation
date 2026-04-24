#!/usr/bin/env python3
"""
语料飞轮兼容入口
================

主实现已经迁到 shared_assets/conversation_store.py。
保留这个模块，避免旧脚本和现有 Tinder 入口失效。
"""
from __future__ import annotations

import sys
from pathlib import Path

SHARED_ASSETS_ROOT = Path(__file__).resolve().parent.parent.parent / "shared_assets"
sys.path.insert(0, str(SHARED_ASSETS_ROOT))

from conversation_store import DB_PATH, ConversationStore, 回流_corpus_to_file  # noqa: E402


if __name__ == "__main__":
    store = ConversationStore()
    print("[语料飞轮] 数据库统计:", store.get_stats())
    print("[语料飞轮] 高转化语料:", len(store.get_top_corpus(limit=20)))
