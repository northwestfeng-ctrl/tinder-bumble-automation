#!/usr/bin/env python3
"""
将 unified_corpus.jsonl 渲染为 Markdown，并同步到指定 NotebookLM 笔记本。

同步策略：
1. 读取 unified_corpus.jsonl
2. 生成稳定文件名的 Markdown 快照
3. 删除同标题旧 source（避免重复堆积）
4. 新增 source 并等待 ready
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).parent
DEFAULT_CORPUS = SCRIPT_DIR / "unified_corpus.jsonl"
DEFAULT_SNAPSHOT = SCRIPT_DIR / "unified_corpus_snapshot.md"
DEFAULT_RESULT = SCRIPT_DIR / "notebooklm_sync_result.json"


def resolve_notebook_id(explicit_value: str = "") -> str:
    notebook_id = str(explicit_value or "").strip() or os.getenv("NOTEBOOKLM_NOTEBOOK_ID", "").strip()
    if not notebook_id and DEFAULT_RESULT.exists():
        try:
            payload = json.loads(DEFAULT_RESULT.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            notebook_id = str(payload.get("notebook_id", "") or "").strip()
    if notebook_id:
        return notebook_id
    raise RuntimeError(
        "NotebookLM notebook id is required. "
        "Pass --notebook-id, set NOTEBOOKLM_NOTEBOOK_ID, or keep notebooklm_sync_result.json."
    )


def iter_jsonl(path: Path) -> Iterable[dict]:
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


def render_snapshot(corpus_path: Path, snapshot_path: Path) -> dict[str, int]:
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    stats = {"records": 0, "conversations": 0}

    for rec in iter_jsonl(corpus_path):
        platform = rec.get("platform", "unknown")
        match_id = rec.get("match_id", "") or ""
        name = rec.get("name", "") or "unknown"
        key = (platform, match_id, name)
        grouped[key].append(rec)
        stats["records"] += 1

    stats["conversations"] = len(grouped)

    lines = [
        "# Unified Corpus Snapshot",
        "",
        f"- source_file: `{corpus_path.name}`",
        f"- conversations: {stats['conversations']}",
        f"- messages: {stats['records']}",
        "",
        "> Auto-generated from unified_corpus.jsonl for NotebookLM source sync.",
        "",
    ]

    def sort_key(item: tuple[tuple[str, str, str], list[dict]]) -> tuple[str, str, str]:
        (platform, match_id, name), messages = item
        latest = ""
        if messages:
            latest = str(messages[-1].get("timestamp", "") or "")
        return (platform, latest, match_id or name)

    for (platform, match_id, name), messages in sorted(grouped.items(), key=sort_key):
        lines.append(f"## [{platform}] {name}")
        if match_id:
            lines.append(f"- match_id: `{match_id}`")
        bio = next((str(m.get("bio", "") or "").strip() for m in messages if m.get("bio")), "")
        if bio:
            lines.append(f"- bio: {bio}")
        lines.append("")
        for msg in messages:
            sender = msg.get("sender", "them")
            role = "Me" if sender == "me" else "Them"
            text = str(msg.get("text", "") or "").strip()
            ts = str(msg.get("timestamp", "") or "").strip()
            if not text:
                continue
            if ts:
                lines.append(f"- {role} [{ts}]: {text}")
            else:
                lines.append(f"- {role}: {text}")
        lines.append("")

    snapshot_path.write_text("\n".join(lines), encoding="utf-8")
    return stats


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["notebooklm", *args],
        capture_output=True,
        text=True,
        timeout=300,
    )


def extract_json_blob(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"JSON payload not found: {text[-400:]}")
    return json.loads(text[start:end + 1])


def list_sources(notebook_id: str) -> list[dict]:
    result = run_cli("source", "list", "-n", notebook_id, "--json")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"source list failed: {detail}")
    payload = extract_json_blob(result.stdout)
    return list(payload.get("sources", []))


def delete_source(notebook_id: str, source_id: str) -> None:
    result = run_cli("source", "delete", source_id, "-n", notebook_id, "-y")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"source delete failed: {detail}")


def add_source(notebook_id: str, snapshot_path: Path) -> str:
    result = run_cli("source", "add", str(snapshot_path), "-n", notebook_id, "--json")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"source add failed: {detail}")
    payload = extract_json_blob(result.stdout)
    source = payload.get("source") or {}
    source_id = source.get("id")
    if not source_id:
        raise RuntimeError(f"source add returned no id: {payload}")
    return str(source_id)


def wait_source_ready(notebook_id: str, source_id: str, timeout: int) -> None:
    result = run_cli("source", "wait", source_id, "-n", notebook_id, "--timeout", str(timeout), "--json")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"source wait failed: {detail}")
    payload = extract_json_blob(result.stdout)
    if payload.get("status") != "ready":
        raise RuntimeError(f"source not ready: {payload}")


def sync_notebook(corpus_path: Path, snapshot_path: Path, notebook_id: str, wait_timeout: int) -> dict:
    notebook_id = resolve_notebook_id(notebook_id)
    if not corpus_path.exists() or corpus_path.stat().st_size == 0:
        raise FileNotFoundError(f"corpus missing or empty: {corpus_path}")

    render_stats = render_snapshot(corpus_path, snapshot_path)
    source_title = snapshot_path.name

    deleted_ids: list[str] = []
    for src in list_sources(notebook_id):
        if src.get("title") == source_title:
            delete_source(notebook_id, str(src["id"]))
            deleted_ids.append(str(src["id"]))

    source_id = add_source(notebook_id, snapshot_path)
    wait_source_ready(notebook_id, source_id, wait_timeout)

    return {
        "notebook_id": notebook_id,
        "snapshot_path": str(snapshot_path),
        "source_title": source_title,
        "source_id": source_id,
        "deleted_source_ids": deleted_ids,
        **render_stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync unified corpus into NotebookLM as a source")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="Path to unified_corpus.jsonl")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT), help="Rendered markdown snapshot path")
    parser.add_argument("--result", default=str(DEFAULT_RESULT), help="Write sync result JSON to this path")
    parser.add_argument("--notebook-id", default="", help="Target NotebookLM notebook id")
    parser.add_argument("--wait-timeout", type=int, default=300, help="Seconds to wait for source processing")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    args = parser.parse_args()

    try:
        result = sync_notebook(
            corpus_path=Path(args.corpus),
            snapshot_path=Path(args.snapshot),
            notebook_id=args.notebook_id,
            wait_timeout=args.wait_timeout,
        )
    except Exception as e:
        print(f"[NotebookLM Sync] ERROR: {e}", file=sys.stderr)
        return 1

    result_path = Path(args.result)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"[NotebookLM Sync] OK notebook={result['notebook_id']} "
            f"source={result['source_id']} deleted={len(result['deleted_source_ids'])} "
            f"conversations={result['conversations']} messages={result['records']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
