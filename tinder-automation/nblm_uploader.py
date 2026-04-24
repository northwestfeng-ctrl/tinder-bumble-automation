#!/usr/bin/env python3
"""
nblm_uploader.py — 演化流水线（纯 API 驱动版）
读取 pending_corpus.jsonl，调用 MiniMax LLM 分析对话策略，自动更新 strategy_config.json

用法:
  python nblm_uploader.py              # 完整流水线：读取语料 → LLM分析 → 更新配置
  python nblm_uploader.py --dry-run   # 打印待发送 Payload，不写文件
"""
import os
import sys
import re
import json
import shutil
import argparse
import anthropic
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).parent
SHARED_ASSETS_DIR = SCRIPT_DIR.parent / "shared_assets"
if str(SHARED_ASSETS_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_ASSETS_DIR))
DEFAULT_CORPUS = SCRIPT_DIR / "pending_corpus.jsonl"
DEFAULT_OUTPUT = SHARED_ASSETS_DIR / "strategy_config.review.json"
DEFAULT_NOTEBOOKLM_CONTEXT = SHARED_ASSETS_DIR / "notebooklm_context.json"

try:
    from unified_reply_engine import (
        should_reply_to_messages as shared_should_reply_to_messages,
        LOW_INFO_REPLY_TEXTS as SHARED_LOW_INFO_REPLY_TEXTS,
        AFFIRMATIVE_REPLY_TEXTS as SHARED_AFFIRMATIVE_REPLY_TEXTS,
        CONTACT_REQUEST_PATTERN as SHARED_CONTACT_REQUEST_PATTERN,
        CONTACT_STAGE_PATTERN as SHARED_CONTACT_STAGE_PATTERN,
        REJECT_PATTERN as SHARED_REJECT_PATTERN,
        END_PATTERN as SHARED_END_PATTERN,
    )
except Exception:
    shared_should_reply_to_messages = None
    SHARED_LOW_INFO_REPLY_TEXTS = set()
    SHARED_AFFIRMATIVE_REPLY_TEXTS = set()
    SHARED_CONTACT_REQUEST_PATTERN = None
    SHARED_CONTACT_STAGE_PATTERN = None
    SHARED_REJECT_PATTERN = None
    SHARED_END_PATTERN = None

LLM_MODEL = "MiniMax-M2.7"
LLM_BASE_URL = "https://api.minimaxi.com/anthropic"

EVOLUTION_PROMPT = """你是一个 dating-chat 对话策略分析系统。下面给你的不是 JSON，而是从真实聊天记录提炼出的纯文本摘要，每一行可能形如：
[platform:name] Them: ...
[platform:name] Me: ...
或
[name] Them: ...; Me: ...; Them: ...

你的任务是：
1. 只提炼适用于 Tinder / Bumble 日常聊天的短回复策略
2. 优先关注轻松、自然、能延续聊天并且更有“推进关系”潜力的互动
3. 忽略明显离题、长篇争论、宗教/政治/哲学辩论、客服式说明、系统异常文本
4. 不要把局部偏题样本放大成全局策略
5. 如果某个话题只在单一对话里出现，比如宗教、哲学、古典音乐、AI 调试、前任细节，不要把“话题本身”沉淀成 success pattern；只有互动动作本身可跨场景复用时才允许保留

输出必须严格为 JSON，且只能包含两个根键：
- "success_patterns": 数组，每项是对象，字段必须且只能包含：
  - "pattern": 对方触发场景或来句
  - "example": 我方可复用的短回复示例
  - "why_it_works": 一句话说明为什么有效
- "failure_patterns": 数组，每项是对象，字段必须且只能包含：
  - "pattern": 失败触发场景或错误做法
  - "root_cause": 失败原因
  - "example": 具体例子

硬性约束：
- success_patterns.pattern 必须直接写成“对方真实来句原文”或非常接近原文的短句，不要写“对方提到…/对方自称…/聊到…”这类摘要标签
- success_patterns.pattern 优先保留问句或原句本身，例如“为什么是七双袜子”而不是“对方问起某个数字或原因”
- success_patterns.example 必须直接写成我方可发送的回复原句，不要写“名字+调侃式质疑”“让对方接梗继续互动”这类策略说明
- success_patterns.example 必须写成可跨场景复用的短回复模板，避免依赖具体人名、明星名、城市名、节日名或一次性梗
- success_patterns 只保留适合短句回复的策略，example 必须是 50 字以内、口语化、可直接复用的回复
- success_patterns 只保留“有明显互动张力”的成功模式：例如能制造悬念、轻调侃、接住玩笑、推进下一轮互动、带一点关系升级感
- 如果某条模式只是礼貌附和、平铺直叙地补充信息，虽然自然但缺少张力，就不要放进 success_patterns
- 如果语料不足以支持某条策略，就不要编造
- 优先保留能泛化到多种聊天场景的互动动作，不要把“信仰/圣经/教会/古典音乐/AI bug/前任”这些垂直话题词本身当成模式核心
- 绝对禁止输出 personality、system_prompt、core_rules、forbidden_tones 等角色设定字段
- 禁止输出任何 JSON 之外的说明文字"""


def _load_dotenv_if_available() -> None:
    """兼容 shared_assets/.env 与 tinder-automation/.env。"""
    try:
        from dotenv import load_dotenv
    except Exception:
        return

    for env_path in (SHARED_ASSETS_DIR / ".env", SCRIPT_DIR / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


def _resolve_llm_api_key() -> str:
    """与统一配置保持一致的 API Key 解析顺序。"""
    return (
        os.getenv("TINDER_LLM_API_KEY")
        or os.getenv("UNIFIED_LLM_API_KEY")
        or os.getenv("APP_LLM__API_KEY")
        or os.getenv("LLM_API_KEY")
        or ""
    )


_load_dotenv_if_available()
LLM_API_KEY = _resolve_llm_api_key()


def _speaker_label(entry: dict) -> str:
    sender = entry.get("sender", "")
    if sender in {"me", "them"}:
        return "Me" if sender == "me" else "Them"
    if entry.get("is_mine") is True:
        return "Me"
    return "Them"


def _format_line_entry(entry: dict) -> str:
    """兼容 unified_corpus 与旧 pending_corpus 的单行摘要。"""
    speaker = _speaker_label(entry)
    text = (entry.get("text") or entry.get("message") or entry.get("content") or "").strip()
    if not text:
        return ""

    name = entry.get("name") or entry.get("match_name") or "unknown"
    platform = entry.get("platform", "")
    prefix = f"[{platform}:{name}]" if platform else f"[{name}]"
    return f"{prefix} {speaker}: {text[:80]}"


def _should_drop_corpus_text(text: object) -> bool:
    candidate = _clean_text(text, 200)
    if not candidate:
        return True
    if candidate == "这会儿有点忙，晚点聊":
        return True
    meta_patterns = (
        r"^(?:Possible responses?|Possible reply|Response options?|Options?)[：:,， ]*",
        r"^(?:Let me analyze|Let me think|I can|I should|I would|My decision is)[:： ]",
        r"(^|\s)#\s*(最近对话|历史摘要|对方资料|对方年龄)\b",
        r"(# 最近对话|# 历史摘要|# 对方资料|# 对方年龄|【任务】|【语言规则】)",
        r"字数统计.{0,10}(?:50|五十)字",
        r"纯回复内容",
        r"符合.{0,20}风格",
        r"不显得讨好",
    )
    return any(re.search(pattern, candidate, re.IGNORECASE) for pattern in meta_patterns)


def _conversation_key(entry: dict) -> tuple[str, str]:
    platform = str(entry.get("platform", "") or "")
    name = str(entry.get("match_name") or entry.get("name") or "unknown")
    stable_id = str(entry.get("match_id") or entry.get("uid") or "").strip()
    return platform, stable_id or name


def _append_message_snapshot(conversations: dict, key: tuple[str, str], entry: dict, idx: int) -> None:
    platform = str(entry.get("platform", "") or "")
    name = str(entry.get("match_name") or entry.get("name") or "unknown")
    bucket = conversations.setdefault(
        key,
        {"platform": platform, "name": name, "messages": [], "last_idx": idx},
    )
    _capture_conversation_metadata(bucket, entry)
    text = (entry.get("text") or entry.get("message") or entry.get("content") or "").strip()
    if text and not _should_drop_corpus_text(text):
        bucket["messages"].append({
            "speaker": _speaker_label(entry),
            "text": text[:80],
        })
        bucket["last_idx"] = idx


def _append_conversation_entry(conversations: dict, key: tuple[str, str], entry: dict, idx: int) -> None:
    platform = str(entry.get("platform", "") or "")
    name = str(entry.get("match_name") or entry.get("name") or "unknown")
    bucket = conversations.setdefault(
        key,
        {"platform": platform, "name": name, "messages": [], "last_idx": idx},
    )
    _capture_conversation_metadata(bucket, entry)
    raw_messages = entry.get("messages", [])
    last_text = ""
    if isinstance(raw_messages, list):
        for msg in raw_messages[-8:]:
            if not isinstance(msg, dict):
                continue
            text = str(msg.get("text", "") or "").strip()
            if not text or _should_drop_corpus_text(text):
                continue
            last_text = text
            bucket["messages"].append({
                "speaker": _speaker_label(msg),
                "text": text[:80],
            })
    reply = _clean_text(entry.get("reply", ""), 80)
    if reply and not _should_drop_corpus_text(reply) and reply != _clean_text(last_text, 80):
        bucket["messages"].append({
            "speaker": "Me",
            "text": reply,
        })
    bucket["last_idx"] = idx


OUTCOME_LABEL_BONUS = {
    "partner_followup_engaged": 0.3,
    "partner_followup_question": 0.22,
    "partner_followup_basic": 0.08,
    "partner_followup_low_info": -0.18,
}


def _capture_conversation_metadata(bucket: dict, entry: dict) -> None:
    outcome = entry.get("outcome")
    try:
        if outcome is not None and str(outcome) != "":
            bucket["outcome"] = float(outcome)
    except Exception:
        pass
    for key in ("outcome_label", "feedback_event", "feedback_reason", "intent", "reply"):
        value = _clean_text(entry.get(key, ""), 120 if key != "reply" else 80)
        if value:
            bucket[key] = value


def _conversation_quality_score(item: dict) -> float:
    score = float(item.get("outcome", 0.0) or 0.0)
    label = str(item.get("outcome_label", "") or "")
    score += OUTCOME_LABEL_BONUS.get(label, 0.0)
    if str(item.get("feedback_event", "") or "").strip():
        score += 0.05
    return round(score, 4)


def _conversation_result_hint(item: dict) -> str:
    label = str(item.get("outcome_label", "") or "").strip()
    if label.startswith("partner_followup_"):
        return f"result={label.replace('partner_followup_', '')}"
    feedback = str(item.get("feedback_event", "") or "").strip()
    if feedback.startswith("partner_followup_"):
        return f"feedback={feedback.replace('partner_followup_', '')}"
    intent = str(item.get("intent", "") or "").strip()
    if intent in {"opener", "reactivation"}:
        return f"intent={intent}"
    return ""


def load_pending_corpus(corpus_path: Path) -> str:
    """读取语料文件，按会话均衡抽样，返回可读的历史消息摘要文本。"""
    if not corpus_path.exists():
        return "（暂无语料）"

    lines = corpus_path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return "（暂无语料）"

    conversations: dict[tuple[str, str], dict] = {}
    sampled_lines = lines[-2000:]
    for idx, line in enumerate(sampled_lines):
        try:
            entry = json.loads(line)
            key = _conversation_key(entry)
            messages = entry.get("messages")
            if isinstance(messages, list) and messages:
                _append_conversation_entry(conversations, key, entry, idx)
            else:
                _append_message_snapshot(conversations, key, entry, idx)
        except Exception:
            continue

    snippets = []
    ranked = sorted(
        conversations.values(),
        key=lambda item: (
            _conversation_quality_score(item),
            item.get("last_idx", 0),
            len(item.get("messages", [])),
        ),
        reverse=True,
    )
    for convo in ranked[:8]:
        recent = convo.get("messages", [])[-8:]
        if not recent:
            continue
        msg_texts = "; ".join(
            f"{msg.get('speaker', 'Them')}: {str(msg.get('text', '')).strip()[:80]}"
            for msg in recent
            if str(msg.get("text", "")).strip()
        )
        if not msg_texts:
            continue
        name = convo.get("name", "unknown")
        platform = convo.get("platform", "")
        prefix = f"[{platform}:{name}]" if platform else f"[{name}]"
        result_hint = _conversation_result_hint(convo)
        if result_hint:
            snippets.append(f"{prefix} ({result_hint}) {msg_texts}")
        else:
            snippets.append(f"{prefix} {msg_texts}")

    return "\n".join(snippets) if snippets else "（语料解析异常）"


def load_notebooklm_context(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    guide_summary = _clean_text(payload.get("guide_summary", ""), 400)
    keywords = payload.get("guide_keywords", []) or []
    strategy_notes = _clean_text(payload.get("strategy_notes", ""), 2500)
    runtime_feedback = _clean_text(payload.get("runtime_feedback_summary_text", ""), 3000)
    noisy_keywords = {
        "宗教交流", "哲学探讨", "AI逻辑与Bug", "人工智能介入对话", "语料同步",
        "基督教信仰", "圣经文本价值", "恋爱话术博弈", "跨文化交流", "古典音乐",
    }
    filtered_keywords = [str(k) for k in keywords if str(k) not in noisy_keywords][:6]
    keywords_text = ", ".join(filtered_keywords)

    # guide_summary 只作为弱参考，尽量压掉容易把模型带偏的说明性噪音
    weak_summary = guide_summary
    for token in ("哲学探讨", "宗教交流", "人工智能介入对话", "AI逻辑与Bug", "NotebookLM等工具提供语料同步"):
        weak_summary = weak_summary.replace(token, "")
    weak_summary = _clean_text(weak_summary, 180)

    parts = [
        "【NotebookLM 辅助归纳】",
        f"- source_title: {payload.get('source_title', '')}",
    ]
    if keywords_text:
        parts.append(f"- source_guide_keywords: {keywords_text}")
    if strategy_notes:
        parts.append("【NotebookLM 强信号策略笔记】")
        parts.append(strategy_notes)
    if weak_summary:
        parts.append("【NotebookLM 弱参考摘要】")
        parts.append(weak_summary)
    if runtime_feedback:
        parts.append("【运行反馈摘要】")
        parts.append(runtime_feedback)
    return "\n".join(parts).strip()


def load_notebooklm_context_payload(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def call_llm分析的(corpus_text: str, notebooklm_context: str = "") -> dict | None:
    """调用 MiniMax LLM，执行演化分析，返回解析后的 dict"""
    print(f"\n[LLM] 调用 {LLM_MODEL} ...")

    if not LLM_API_KEY:
        print("[LLM] 缺少 API Key。请设置 TINDER_LLM_API_KEY、UNIFIED_LLM_API_KEY 或 APP_LLM__API_KEY")
        return None

    client = anthropic.Anthropic(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
    )

    context_block = ""
    if notebooklm_context:
        context_block = f"""

以下是 NotebookLM 仅基于“当前最新语料 source”生成的辅助归纳，不是最终答案，只能作为参考：
{notebooklm_context}
"""

    user_message = f"""{EVOLUTION_PROMPT}{context_block}

【待分析语料】
{corpus_text}"""

    try:
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=4096,
            temperature=0.3,
            system="你是一个严格遵循 JSON 输出格式的对话策略分析助手。",
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        print(f"[LLM] API 调用失败: {e}")
        return None

    raw = ""
    if isinstance(response.content, list):
        for block in response.content:
            if getattr(block, "type", "") == "text":
                raw = getattr(block, "text", "") or ""
                break
    else:
        raw = str(response.content)

    print(f"[LLM] 原始响应 ({len(raw)} chars): {raw[:200]}...")

    # 解析 JSON
    parsed = parse_json_response(raw)
    if parsed:
        return parsed

    print("[LLM] 首次输出未满足 JSON 契约，尝试自动修复格式...")
    repaired = repair_json_response(client, raw)
    if repaired:
        return repaired
    return None


def parse_json_response(raw: str) -> dict | None:
    """从 LLM 输出中提取纯 JSON"""
    # 去除 markdown 代码块
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    # 去除多余空行
    cleaned = re.sub(r"\n{3,}", "\n", cleaned)

    # 定位 JSON 边界
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}") + 1
    if json_start >= 0 and json_end > 0:
        cleaned = cleaned[json_start:json_end]
    else:
        print("[parse] 未找到 JSON 边界")
        return None

    try:
        result = json.loads(cleaned)
        print(f"[parse] JSON 解析成功: {list(result.keys())}")
        return result
    except json.JSONDecodeError as e:
        print(f"[parse] JSON 解析失败: {e}")
        print(f"[parse] cleaned (300字): {cleaned[:300]}")
        return None


def repair_json_response(client: anthropic.Anthropic, raw: str) -> dict | None:
    repair_prompt = f"""把下面这段已有分析结果整理成严格 JSON。

要求：
1. 只能输出 JSON
2. 根键只能有 success_patterns 和 failure_patterns
3. success_patterns 每项只能有 pattern, example, why_it_works
4. failure_patterns 每项只能有 pattern, root_cause, example
5. 不要补充任何解释，不要新增原文没有支持的策略

待整理内容：
{raw[:6000]}
"""
    try:
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=4096,
            temperature=0,
            system="你是一个 JSON 修复器。只能输出合法 JSON，不得输出任何额外文字。",
            messages=[{"role": "user", "content": repair_prompt}],
        )
    except Exception as e:
        print(f"[repair] API 调用失败: {e}")
        return None

    repaired_raw = ""
    if isinstance(response.content, list):
        for block in response.content:
            if getattr(block, "type", "") == "text":
                repaired_raw = getattr(block, "text", "") or ""
                break
    else:
        repaired_raw = str(response.content)

    print(f"[repair] 原始修复响应 ({len(repaired_raw)} chars): {repaired_raw[:200]}...")
    return parse_json_response(repaired_raw)


def _clean_text(value: object, max_len: int = 120) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


def _looks_like_meta_pattern(text: str) -> bool:
    candidate = _clean_text(text, 120)
    meta_prefixes = (
        "对方", "聊到", "聊天到", "当", "如果", "遇到", "回应", "对话中", "话题里",
    )
    meta_phrases = (
        "表示认同", "提出好奇心问题", "回应了你的调侃", "开玩笑自称", "触发场景", "错误做法",
        "短反馈", "语气词", "旅行话题", "好奇心问题", "推进见面", "提到", "自称",
    )
    if any(phrase in candidate for phrase in meta_phrases):
        return True
    if candidate.startswith(meta_prefixes) and len(candidate) > 10 and not re.search(r"[\"“”'‘’：:？?！!，,。]", candidate):
        return True
    return False


def _looks_overfit_example(text: str) -> bool:
    candidate = _clean_text(text, 80)
    suspicious_entities = (
        "王菲", "春晚", "李白", "教会", "马克思", "福州", "厦门", "微信号", "Mabel1130",
    )
    if any(token in candidate for token in suspicious_entities):
        return True
    # 明显一次性数字/账号型内容
    if re.search(r"\b\d{5,}\b", candidate):
        return True
    return False


def _looks_niche_topic_pattern(pattern: str, example: str, why_it_works: str) -> bool:
    text = " ".join((_clean_text(pattern, 80), _clean_text(example, 80), _clean_text(why_it_works, 100)))
    narrow_topic_tokens = (
        "圣经", "基督", "神吗", "信仰", "教会", "宗教",
        "哲学", "古典音乐", "乐章", "富特文格勒", "AI", "bug", "前女友",
    )
    return any(token in text for token in narrow_topic_tokens)


def _conflicts_with_shared_reply_gate(pattern: str) -> bool:
    candidate = _normalize_pattern_text(_clean_text(pattern, 80))
    if not candidate:
        return True

    normalized = candidate.lower()
    affirmative = {item.lower() for item in SHARED_AFFIRMATIVE_REPLY_TEXTS}
    if candidate in SHARED_LOW_INFO_REPLY_TEXTS and normalized not in affirmative:
        return True

    if SHARED_REJECT_PATTERN and SHARED_REJECT_PATTERN.search(candidate):
        return True
    if SHARED_CONTACT_REQUEST_PATTERN and SHARED_CONTACT_REQUEST_PATTERN.search(candidate):
        return True
    if SHARED_CONTACT_STAGE_PATTERN and SHARED_CONTACT_STAGE_PATTERN.search(candidate):
        return True
    if SHARED_END_PATTERN and SHARED_END_PATTERN.search(candidate):
        return True

    return False


def _looks_like_meta_example(text: str) -> bool:
    candidate = _clean_text(text, 80)
    meta_phrases = (
        "让对方", "继续互动", "调侃式", "名字+", "名字＋", "接梗", "制造悬念", "推进话题",
        "拉近距离", "延续聊天", "表达兴趣", "互动感", "幽默感", "质疑式", "模板",
    )
    if any(token in candidate for token in meta_phrases):
        return True
    if re.search(r"[+＋].*(调侃|质疑|互动)", candidate):
        return True
    return False


def _looks_low_tension_example(text: str, why_it_works: str) -> bool:
    candidate = _clean_text(text, 80)
    why = _clean_text(why_it_works, 120)
    low_tension_signals = (
        "亲切感", "补充个人看法", "延续话题深度", "表示认同", "分享看法", "自然交流",
    )
    if any(token in candidate for token in ("文化圈", "亲切感")):
        return True
    if any(token in why for token in low_tension_signals):
        return True
    return False


def _looks_like_pure_emoji_pattern(text: str) -> bool:
    candidate = _clean_text(text, 80)
    if not candidate:
        return False
    # 只包含 emoji / 符号 / 标点的触发句不适合沉淀成 few-shot 示例
    stripped = re.sub(r"[\s\W_]+", "", candidate, flags=re.UNICODE)
    if not stripped:
        return True
    if re.fullmatch(r"[^\w\u4e00-\u9fffA-Za-z0-9]+", candidate):
        return True
    return False


def _normalize_pattern_text(text: str) -> str:
    candidate = _clean_text(text, 80)
    if not candidate:
        return ""
    # 去掉句尾装饰性 emoji / 符号，尽量保留可读的真实来句文本
    candidate = re.sub(r"[^\w\u4e00-\u9fffA-Za-z0-9！？!?。，“”\"'‘’：:，,（）()]+$", "", candidate)
    # 统一常见数字写法，减少同义 pattern 重复
    candidate = candidate.replace("7双", "七双")
    candidate = candidate.strip("：:，,。.!！?？ ")
    return _clean_text(candidate, 80)


def _pattern_family(pattern: str) -> str:
    candidate = _normalize_pattern_text(pattern)
    if "七双" in candidate:
        return "七双袜子"
    return candidate


def _success_pattern_score(pattern: str, example: str, why_it_works: str) -> tuple[int, int, int]:
    score = 0
    if "送我" in example:
        score += 3
    if "告诉你" in example:
        score += 2
    if "原谅你" in example:
        score += 2
    if "悬念" in why_it_works:
        score += 1
    if "推拉" in why_it_works or "张力" in why_it_works:
        score += 1
    # 偏好信息更完整的 pattern，但不要无限偏长
    return (score, min(len(pattern), 40), min(len(example), 40))


def _extract_concrete_pattern(text: object) -> str:
    candidate = _clean_text(text, 120)
    if not candidate:
        return ""

    quote_patterns = [
        r"[\"“”'‘’]([^\"“”'‘’]{2,40})[\"“”'‘’]",
        r"[（(]([^（）()]{2,40})[）)]",
    ]
    for pattern in quote_patterns:
        match = re.search(pattern, candidate)
        if match:
            inner = _clean_text(match.group(1), 60)
            if inner and not _looks_like_meta_pattern(inner):
                return inner

    match = re.match(r"^对方(?:提到|问到|说到|提及|聊到)\s*(.+)$", candidate)
    if match:
        inner = _clean_text(match.group(1), 60).strip("：:，,。.!！?？ ")
        if inner and not _looks_like_meta_pattern(inner):
            return inner

    # 去掉常见元描述前缀，保留更像真实来句的尾部
    candidate = re.sub(
        r"^(对方|聊天里对方|当对方|如果对方|聊到|当聊到|遇到|回应|对话中).*?[：:，,]\s*",
        "",
        candidate,
    )
    candidate = candidate.strip("：:，,。.!！?？ ")
    return _clean_text(candidate, 60)


def _normalize_success_patterns(items: list) -> list[dict]:
    normalized = []
    by_family: dict[str, dict] = {}
    for item in items or []:
        if isinstance(item, dict):
            pattern = _normalize_pattern_text(_extract_concrete_pattern(item.get("pattern")))
            example = _clean_text(item.get("example"), 50)
            why_it_works = _clean_text(item.get("why_it_works"), 100)
            if (
                pattern
                and example
                and not _looks_like_meta_pattern(pattern)
                and not _looks_like_pure_emoji_pattern(pattern)
                and not _looks_like_meta_example(example)
                and not _looks_low_tension_example(example, why_it_works)
                and not _looks_overfit_example(example)
                and not _looks_niche_topic_pattern(pattern, example, why_it_works)
                and not _conflicts_with_shared_reply_gate(pattern)
            ):
                candidate_item = {
                    "pattern": pattern,
                    "example": example,
                    "why_it_works": why_it_works,
                }
                family = _pattern_family(pattern)
                existing = by_family.get(family)
                if not existing:
                    by_family[family] = candidate_item
                    continue
                current_score = _success_pattern_score(
                    candidate_item["pattern"], candidate_item["example"], candidate_item["why_it_works"]
                )
                existing_score = _success_pattern_score(
                    existing["pattern"], existing["example"], existing["why_it_works"]
                )
                if current_score > existing_score:
                    by_family[family] = candidate_item

    normalized.extend(by_family.values())
    normalized.sort(key=lambda item: _success_pattern_score(item["pattern"], item["example"], item["why_it_works"]), reverse=True)
    return normalized[:12]


def _normalize_failure_patterns(items: list) -> list[dict]:
    normalized = []
    for item in items or []:
        if isinstance(item, dict):
            pattern = _clean_text(item.get("pattern"), 80)
            root_cause = _clean_text(item.get("root_cause"), 120)
            example = _clean_text(item.get("example"), 120)
            if pattern and root_cause:
                normalized.append({
                    "pattern": pattern,
                    "root_cause": root_cause,
                    "example": example,
                })
        elif isinstance(item, str):
            text = _clean_text(item, 180)
            if text:
                normalized.append({
                    "pattern": text[:60],
                    "root_cause": text,
                    "example": "",
                })
    return normalized[:12]


def normalize_analysis(analysis: dict) -> dict:
    return {
        "success_patterns": _normalize_success_patterns(analysis.get("success_patterns", [])),
        "failure_patterns": _normalize_failure_patterns(analysis.get("failure_patterns", [])),
    }


def update_strategy_config(analysis: dict, output_path: Path, notebooklm_payload: dict | None = None):
    """将分析结果安全写入 strategy_config.json"""
    print(f"\n[Update] 写入 {output_path} ...")
    backup_path = output_path.with_suffix(output_path.suffix + ".bak")

    # 备份
    if output_path.exists():
        backup_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  备份已存: {backup_path}")

    # 读取现有配置（保留非分析类字段）
    existing = {}
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    normalized = normalize_analysis(analysis)

    notebooklm_payload = notebooklm_payload or {}
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_bits = [f"LLM演化流水线 {generated_at}"]
    if notebooklm_payload.get("source_title"):
        source_bits.append(f"source={notebooklm_payload.get('source_title')}")
    if notebooklm_payload.get("source_id"):
        source_bits.append(f"id={notebooklm_payload.get('source_id')}")

    updated = {
        "version": existing.get("version", "3.2"),
        "generated_at": generated_at,
        "source": " | ".join(source_bits),
        "notebook_id": notebooklm_payload.get("notebook_id", existing.get("notebook_id", "")),
        "notebooklm_source_id": notebooklm_payload.get("source_id", existing.get("notebooklm_source_id", "")),
        "notebooklm_source_title": notebooklm_payload.get("source_title", existing.get("notebooklm_source_title", "")),
        "success_patterns": normalized.get("success_patterns", []),
        "failure_patterns": normalized.get("failure_patterns", []),
    }

    runtime_feedback = notebooklm_payload.get("runtime_feedback_summary", {})
    if isinstance(runtime_feedback, dict) and runtime_feedback:
        updated["runtime_feedback_snapshot"] = {
            "generated_at": runtime_feedback.get("generated_at", ""),
            "lookback_days": runtime_feedback.get("lookback_days", 0),
            "total_events": runtime_feedback.get("total_events", 0),
            "weighted_sent_total": runtime_feedback.get("weighted_sent_total", 0),
            "weighted_skipped_total": runtime_feedback.get("weighted_skipped_total", 0),
            "weighted_failed_total": runtime_feedback.get("weighted_failed_total", 0),
            "weighted_net_score": runtime_feedback.get("weighted_net_score", 0),
            "weighted_event_scores": runtime_feedback.get("weighted_event_scores", {}),
        }

    output_path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"  已写入: {output_path}")


def archive_corpus(corpus_path: Path):
    """归档已消费的语料"""
    if corpus_path != DEFAULT_CORPUS or not corpus_path.exists():
        return
    archive_dir = SCRIPT_DIR / "corpus_archive"
    archive_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"{corpus_path.stem}_{timestamp}{corpus_path.suffix}"
    shutil.move(str(corpus_path), str(archive_path))
    print(f"  已归档语料至: {archive_path}")


def validate_corpus(corpus_path: Path) -> bool:
    """纯 API 模式下的 upload 阶段只做语料校验/预检。"""
    if not corpus_path.exists():
        print(f"[Upload] 语料不存在: {corpus_path}")
        return False
    if corpus_path.stat().st_size == 0:
        print(f"[Upload] 语料为空: {corpus_path}")
        return False
    preview = load_pending_corpus(corpus_path)
    if preview in {"（暂无语料）", "（语料解析异常）"}:
        print(f"[Upload] 语料不可用: {corpus_path}")
        return False
    print(f"[Upload] 纯 API 模式无需远端上传，预检通过: {corpus_path}")
    return True


def dry_run(corpus_path: Path):
    """仅打印 Payload，不写文件"""
    print("\n=== [Dry Run] ===")
    corpus = load_pending_corpus(corpus_path)
    size = corpus_path.stat().st_size if corpus_path.exists() else 0
    print(f"语料文件: {corpus_path} ({size} bytes)")
    print(f"\n【待发送 System Prompt】\n{EVOLUTION_PROMPT}\n")
    print(f"【待发送 User Message（语料）】\n{corpus[:500]}...\n")
    print(f"API: {LLM_BASE_URL} | Model: {LLM_MODEL}")


def main():
    parser = argparse.ArgumentParser(description="演化流水线 — 纯 API 驱动")
    parser.add_argument("--upload", action="store_true", help="执行 NotebookLM 上传预检")
    parser.add_argument("--analyze", action="store_true", help="执行策略分析并写出配置")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="语料文件路径")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出 strategy_config.json 路径")
    parser.add_argument("--notebooklm-context", type=Path, default=DEFAULT_NOTEBOOKLM_CONTEXT, help="NotebookLM 辅助上下文 JSON")
    parser.add_argument("--dry-run", action="store_true", help="打印 Payload，不写文件")
    args = parser.parse_args()

    if args.dry_run:
        dry_run(args.corpus)
        return

    run_upload = args.upload
    run_analyze = args.analyze or not args.upload

    if run_upload:
        print("=== Upload 预检 ===")
        if not validate_corpus(args.corpus):
            sys.exit(1)
        if not run_analyze:
            return

    print("=== 演化流水线 ===")
    corpus = load_pending_corpus(args.corpus)
    notebooklm_context = load_notebooklm_context(args.notebooklm_context)
    print(f"已加载语料: {len(corpus)} chars")
    if notebooklm_context:
        print(f"已加载 NotebookLM 辅助上下文: {len(notebooklm_context)} chars")

    analysis = call_llm分析的(corpus, notebooklm_context=notebooklm_context)
    if not analysis:
        print("[Error] LLM 分析失败")
        sys.exit(1)

    update_strategy_config(
        analysis,
        args.output,
        notebooklm_payload=load_notebooklm_context_payload(args.notebooklm_context),
    )
    archive_corpus(args.corpus)
    print("\n✅ 演化流水线完成")


if __name__ == "__main__":
    main()
