#!/usr/bin/env python3
"""
merge_corpus.py
读取 Tinder / Bumble 的 pending_corpus.jsonl，去重后写入 shared_assets/unified_corpus.jsonl。
兼容平台感知的增量语料字段，供手动排查或旧入口使用。
"""
import json, hashlib
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
TINDER_DIR = SCRIPT_DIR.parent / "tinder-automation"
BUMBLE_DIR = SCRIPT_DIR.parent / "bumble-automation"
UNIFIED    = SCRIPT_DIR / "unified_corpus.jsonl"

SOURCES = {
    "tinder": TINDER_DIR / "pending_corpus.jsonl",
    "bumble": BUMBLE_DIR / "pending_corpus.jsonl",
}


def iter_records(path: Path, platform: str):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec.setdefault("platform", platform)
            yield rec


def hashrec(rec: dict) -> str:
    key = json.dumps({
        "record_type": rec.get("record_type", ""),
        "platform": rec.get("platform", ""),
        "match_id": rec.get("match_id", ""),
        "match_name": rec.get("match_name", rec.get("name", "")),
        "sender": rec.get("sender", ""),
        "text": rec.get("text", ""),
        "reply": rec.get("reply", ""),
        "intent": rec.get("intent", ""),
        "outcome_label": rec.get("outcome_label", ""),
        "feedback_event": rec.get("feedback_event", ""),
        "messages": rec.get("messages", []),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def merge():
    if UNIFIED.exists():
        UNIFIED.unlink()

    seen = set()
    total = 0
    written = 0

    for platform, path in SOURCES.items():
        for rec in iter_records(path, platform):
            h = hashrec(rec)
            if h in seen:
                continue
            seen.add(h)
            total += 1
            with open(UNIFIED, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1

    print(f"[merge] 合并完成：{total} 条原始，{written} 条写入 (去重后)")
    return written


if __name__ == "__main__":
    merge()
