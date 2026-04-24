#!/usr/bin/env python3
"""
flush_corpus.py
读取待处理队列 → 合并到 corpus_history.json
用法: python flush_corpus.py [--dry-run]
"""
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
PENDING_FILE = SCRIPT_DIR / "pending_corpus.jsonl"
HISTORY_FILE = SCRIPT_DIR / "corpus_history.json"
BACKUP_FILE = SCRIPT_DIR / "corpus_history_backup.json"


def load_history():
    if HISTORY_FILE.exists():
        return json.load(open(HISTORY_FILE, encoding='utf-8'))
    return []


def save_history(data):
    # 先备份
    if HISTORY_FILE.exists():
        BACKUP_FILE.write_text(HISTORY_FILE.read_text(encoding='utf-8'), encoding='utf-8')
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def is_duplicate(new_entry: dict, history: list) -> bool:
    """根据 match_id 和首条消息判断是否重复"""
    match_id = new_entry.get('match_id')
    if match_id:
        return any(c.get('match_id') == match_id for c in history)
    # 兜底：对比首条消息
    new_msgs = new_entry.get('messages', [])
    if not new_msgs:
        return False
    return any(
        c.get('messages', [])[:1] == new_msgs[:1]
        for c in history
    )


def merge():
    pending = []
    if PENDING_FILE.exists():
        with open(PENDING_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    pending.append(json.loads(line))

    if not pending:
        print("[flush] 队列为空，无需合并")
        return

    history = load_history()
    new_count = 0
    dup_count = 0

    for entry in pending:
        if not is_duplicate(entry, history):
            history.append(entry)
            new_count += 1
        else:
            dup_count += 1

    save_history(history)

    # 清空队列
    PENDING_FILE.write_text('', encoding='utf-8')

    # ── 强化清理：删除增量文件旧版本，确保 NotebookLM 单例 ──────────────
    # 增量文件指同一类来源在每次流水线跑完后会重新生成的 Markdown/JSON
    # NotebookLM 每次上传同一文件会新增一个 Source，必须在生成新文件前
    # 删除旧版，避免触碰 50 个 Source 上限
    incremental_patterns = [
        SCRIPT_DIR / "corpus_history.md",
        SCRIPT_DIR / "corpus_markdown.md",
    ]
    removed_any = False
    for f in incremental_patterns:
        if f.exists():
            f.unlink()
            print(f"  [cleanup] 已删除旧增量文件: {f.name}")
            removed_any = True
    if not removed_any:
        print("  [cleanup] 无旧增量文件需清理")
    # ───────────────────────────────────────────────────────────────────

    print(f"[flush] 合并完成: +{new_count} 新记录, 跳过 {dup_count} 重复, 共 {len(history)} 条历史")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='只显示不写入')
    args = parser.parse_args()

    if args.dry_run:
        pending = []
        if PENDING_FILE.exists():
            with open(PENDING_FILE, 'r', encoding='utf-8') as f:
                pending = [json.loads(l) for l in f if l.strip()]
        print(f"[dry-run] 待处理: {len(pending)} 条")
        for p in pending:
            print(f"  - {p.get('match_name', '?')} | {p.get('added_at', '')}")
    else:
        merge()