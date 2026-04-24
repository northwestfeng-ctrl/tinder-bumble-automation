#!/usr/bin/env python3
"""
从 NotebookLM 拉取“当前最新语料 source”的辅助分析结果，供本地演化器使用。

输出内容：
- source guide summary / keywords
- 限定到当前 source 的 ask 分析答案
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
DEFAULT_SYNC_RESULT = SCRIPT_DIR / "notebooklm_sync_result.json"
DEFAULT_OUTPUT = SCRIPT_DIR / "notebooklm_context.json"
NARROW_TOPIC_TOKENS = (
    "宗教", "圣经", "基督", "教会", "信仰",
    "哲学", "古典音乐", "乐章", "富特文格勒",
    "AI", "Bug", "bug", "前女友",
)


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


def load_sync_result(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_source_guide(notebook_id: str, source_id: str) -> dict:
    result = run_cli("source", "guide", source_id, "-n", notebook_id, "--json")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"source guide failed: {detail}")
    payload = extract_json_blob(result.stdout)
    return {
        "summary": payload.get("summary", ""),
        "keywords": payload.get("keywords", []),
    }


def fetch_source_strategy_notes(notebook_id: str, source_id: str) -> str:
    question = (
        "只基于这个 source，输出一份极短的中文要点摘要。"
        "不要引用旧资料理论，不要提反脆弱、无需求感、两极化等术语。"
        "只总结当前聊天语料里真正出现过的高张力互动模式和低张力失败模式。"
        "忽略只在单一对话里出现的窄话题内容，比如宗教、哲学、古典音乐、AI 调试、前任细节；"
        "除非它体现的是可跨场景复用的互动动作，而不是话题本身。"
        "优先保留：制造悬念、接住玩笑、轻调侃、推进下一轮互动、带一点关系升级感。"
        "明确排除：低张力附和、平铺直叙补充信息、机械 hi、长篇解释。"
        "输出格式严格限制为："
        "高张力模式：最多3条，每条一行，格式“高张力 | 对方来句 -> 我方回复 | 简短原因”；"
        "低张力模式：最多3条，每条一行，格式“低张力 | 对方来句 -> 我方回复 | 简短原因”。"
        "不要写导语，不要编号，不要引用来源编号。"
    )
    result = run_cli("ask", "-n", notebook_id, "-s", source_id, "--json", question)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"notebook ask failed: {detail}")
    payload = extract_json_blob(result.stdout)
    return str(payload.get("answer", "")).strip()


def compact_strategy_notes(notes: str) -> str:
    compact_lines: list[str] = []
    for raw in notes.splitlines():
        line = raw.strip().lstrip("*-• ")
        if not line:
            continue
        if any(token in line for token in NARROW_TOPIC_TOKENS):
            continue
        if "高张力 |" in line or "低张力 |" in line:
            compact_lines.append(line[:240])
        if len(compact_lines) >= 6:
            break
    if compact_lines:
        return "\n".join(compact_lines)
    return notes[:1200].strip()


def build_context(sync_result_path: Path, output_path: Path) -> dict:
    sync = load_sync_result(sync_result_path)
    notebook_id = str(sync["notebook_id"])
    source_id = str(sync["source_id"])

    guide = fetch_source_guide(notebook_id, source_id)
    notes = compact_strategy_notes(fetch_source_strategy_notes(notebook_id, source_id))

    context = {
        "notebook_id": notebook_id,
        "source_id": source_id,
        "source_title": sync.get("source_title", ""),
        "guide_summary": guide.get("summary", ""),
        "guide_keywords": guide.get("keywords", []),
        "strategy_notes": notes[:4000],
    }
    output_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return context


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch NotebookLM context for the latest synced source")
    parser.add_argument("--sync-result", default=str(DEFAULT_SYNC_RESULT), help="Path to notebooklm_sync_result.json")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output path for notebooklm_context.json")
    parser.add_argument("--json", action="store_true", help="Print JSON result")
    args = parser.parse_args()

    try:
        context = build_context(Path(args.sync_result), Path(args.output))
    except Exception as e:
        print(f"[NotebookLM Context] ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(context, ensure_ascii=False, indent=2))
    else:
        print(
            f"[NotebookLM Context] OK source={context['source_id']} "
            f"keywords={len(context['guide_keywords'])} notes_len={len(context['strategy_notes'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
