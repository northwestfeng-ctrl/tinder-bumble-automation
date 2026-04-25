#!/usr/bin/env python3
"""
unified_reply_engine.py
=========================
跨平台统一回复生成引擎。
同时适用于 Tinder / Bumble / 任意 Imitative-SPA 社交平台。

核心职责
--------
1. generate_reply()   — 生成单条回复（核心入口）
2. extract_reasoning() — 从 MiniMax-M2.7 reasoning_content 提取实际回复
3. build_prompt()    — 构造 LLM Prompt（含 platform 变量）

共享配置
--------
默认读取 shared_assets/strategy_config.json
可通过 env[UNIFIED_STRATEGY_PATH] 覆盖。

平台适配层（Platform Adapter）
-------------------------------
click_contact(page, contact_entry) — 平台特有的点进对话逻辑
  Tinder : 列表式 element.click()，无需偏移
  Bumble : overlay 式，需 data-qa-uid 精准点击 + 通知面板关闭
           坐标需左偏约 120px（sidebar 遮挡问题）

Prompt 硬性规则（双平台统一）
------------------------------
- 语言跟随对方，口语化自然
- 极简短句，可使用 / 分隔多句
- 格式：极简短句。必须保证句子结构完整，严禁话说一半中断。句末不要加标点，句中可使用空格或逗号分隔。
- 禁止解释性、自证清白、寻求认可的话语
- 低需求感，不追问

LLM 推理模型处理
-----------------
MiniMax-M2.7 : content 字段为空，实际回复在 reasoning_content 末尾
abab6.5s-chat : content 字段直接有值
其余模型    : 优先 content，fallback 到 reasoning_content
"""
from __future__ import annotations

import os
import re
import time
import json
import urllib.request
import logging
from typing import Optional
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# ─────────────────────────────────────────────────────────────────
# 统一配置
# ─────────────────────────────────────────────────────────────────
from config import get_config

config = get_config()

SCRIPT_DIR = Path(__file__).parent
SHARED_CFG = Path(os.environ.get(
    "UNIFIED_STRATEGY_PATH",
    str(SCRIPT_DIR / "strategy_config.json")
))

# 从统一配置获取 LLM 参数
LLM_API_KEY = config.llm.api_key
LLM_MODEL = config.llm.model
LLM_BASE_URL = config.llm.base_url
LLM_TIMEOUT = config.llm.timeout
LLM_MAX_RETRIES = config.llm.max_retries

log = logging.getLogger("URE")


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


SAFE_FALLBACK_REPLY = "这会儿有点忙，晚点聊"
REACTION_LIKE_CANONICAL_TEXT = "[liked your message]"
DEFAULT_REACTIVATION_MIN_DORMANT_HOURS = _env_int("APP_REACTIVATION__MIN_DORMANT_HOURS", 24)
DEFAULT_REACTIVATION_GAP_HOURS = _env_int("APP_REACTIVATION__MIN_REACTIVATION_GAP_HOURS", 72)
DEFAULT_REACTIVATION_MAX_ATTEMPTS = _env_int("APP_REACTIVATION__MAX_ATTEMPTS", 2)
LOW_INFO_REPLY_TEXTS = {"哈哈", "哈哈哈", "呵呵", "呵呵呵", "嗯", "嗯嗯", "哦", "噢", "奥", "hmm", "emm"}
AFFIRMATIVE_REPLY_TEXTS = {"好", "好的", "好啊", "好滴", "行", "行啊", "可以", "可以啊", "都行", "ok", "okay", "okie"}
GREETING_REPLY_TEXTS = {"hi", "hello", "hey", "heyy", "heyy", "嗨", "哈喽", "嗨👋", "hello👋", "hey👋"}
CONTACT_CONFIRM_REPLY_TEXTS = {"加了", "已加", "通过了", "加你了", "加啦"}
POSITIVE_EMOJI_REPLY_TEXTS = {"😂", "🤣", "😆", "😁", "😄", "😅", "😊", "😉", "🤭", "🙈", "🥹"}
CONTACT_REQUEST_PATTERN = re.compile(
    r'(?:(?:你有|有)(?:微信|wechat|vx)|怎么联系|'
    r'(?:加个?|留个?|换个?|给我|发我).{0,6}(?:微信|wechat|vx|联系方式)|'
    r'(?:微信|wechat|vx)(?:吗|呢|\?)|加我(?:微信|vx)?)',
    re.IGNORECASE,
)
CONTACT_STAGE_PATTERN = re.compile(
    r'(?:(?:我的微信|我微信是|微信号|vx号|wechat id)|'
    r'(?:加个微信|留个微信|换微信|用微信聊|上微信聊|加我微信|发我微信|微信聊)|'
    r'(?:微信|wechat|vx)[:：]\s*\w+)',
    re.IGNORECASE,
)
REJECT_PATTERN = re.compile(r'(不用|算了吧|不想|不需要|没兴趣|886|拜拜)')
END_PATTERN = re.compile(r'(我先睡了|睡了|去洗澡了?|先这样吧?|回头聊|下次再说|改天聊|晚点聊|先忙了|先去忙了|我先撤了|先走了)', re.IGNORECASE)
CONTINUE_SIGNAL_PATTERN = re.compile(
    r'(但|不过|不过也|不过呢|不过还|周末可以|明天可以|改天也行|有空|忙完|下班[后了]?)'
    r'|(等会|等我|待会|回头继续|之后继续|回来说|再说说|再讲讲|继续聊|回来聊)',
    re.IGNORECASE,
)
SYSTEM_CONTEXT_TAG_PATTERN = re.compile(r"^\s*\[[^\[\]\n]{1,80}\]\s*$")
SYSTEM_CONTEXT_TEXTS = {
    REACTION_LIKE_CANONICAL_TEXT.lower(),
    "[赞了你的消息]",
    "赞了你的消息",
    "点赞了你的消息",
    "liked your message",
    "liked one of your messages",
    "reacted to your message",
    "your move",
    "轮到您了",
    "轮到你了",
}
SYSTEM_CONTEXT_FRAGMENT_PATTERN = re.compile(
    r"(liked (?:your|one of your) messages?|reacted to your message|赞了你的消息|点赞了你的消息|"
    r"your move|轮到[您你]了|has extended the match|extended the match|"
    r"match (?:expires|extended)|chat (?:expires|will expire)|el chat finaliza)",
    re.IGNORECASE,
)


def _looks_english_text(text: str) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return False
    latin = len(re.findall(r"[A-Za-z]", candidate))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", candidate))
    return latin > 0 and cjk == 0


def _is_system_context_text(text: str) -> bool:
    """Reaction/system labels are metadata, not the partner's natural language."""
    candidate = re.sub(r"\s+", " ", str(text or "")).strip()
    if not candidate:
        return True
    lowered = candidate.lower()
    return (
        bool(SYSTEM_CONTEXT_TAG_PATTERN.fullmatch(candidate))
        or lowered in SYSTEM_CONTEXT_TEXTS
        or (len(candidate) <= 100 and bool(SYSTEM_CONTEXT_FRAGMENT_PATTERN.search(candidate)))
    )


def _is_contextual_partner_message(item: dict) -> bool:
    """True only for real partner text that should influence language/context heuristics."""
    msg = item or {}
    if msg.get("sender") == "me" or msg.get("is_mine") is True:
        return False
    meta_type = str(msg.get("meta_type", "") or "").strip().lower()
    if meta_type in {"reaction_like", "system", "system_message", "status"}:
        return False
    return not _is_system_context_text(msg.get("text", ""))


def _clip_reply(text: str, max_len: int = 50) -> str:
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if len(candidate) > max_len:
        candidate = candidate[:max_len].strip()
    return candidate


def _configured_default_opener(strategy: Optional[dict], platform: str, max_len: int = 50) -> str:
    """Return a hard safe opener for new matches when LLM/profile context is unavailable."""
    strategy = strategy or {}
    platform_key = str(platform or "tinder").lower()
    candidates = strategy.get("default_openers") or strategy.get("fallback_openers") or {}
    value = ""
    if isinstance(candidates, dict):
        value = str(candidates.get(platform_key) or candidates.get("default") or "").strip()
    elif isinstance(candidates, str):
        value = candidates.strip()
    if not value:
        value = "先打个招呼 你这张有点会拍"
    return _clip_reply(value, max_len)


def _configured_contextual_fallback(
    last_text: str,
    recent_partner_text: str,
    *,
    platform: str = "tinder",
    max_len: int = 50,
) -> Optional[str]:
    """Strategy-configurable high-confidence fallback rules for known compact contexts."""
    try:
        strategy = load_strategy()
    except Exception:
        strategy = {}
    rules = strategy.get("contextual_fallbacks") or []
    if not isinstance(rules, list):
        return None

    context = re.sub(r"\s+", " ", recent_partner_text or "").strip()
    latest = re.sub(r"\s+", " ", last_text or "").strip()
    platform_key = str(platform or "").lower()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_platform = str(rule.get("platform", "") or "").lower()
        if rule_platform and rule_platform not in {platform_key, "all", "*"}:
            continue
        reply = str(rule.get("reply", "") or "").strip()
        if not reply:
            continue
        target = latest if str(rule.get("scope", "latest")).lower() == "latest" else f"{context} {latest}".strip()
        pattern = str(rule.get("trigger_regex", "") or "").strip()
        try:
            if pattern and re.search(pattern, target, re.IGNORECASE):
                return _clip_reply(reply, max_len)
        except re.error as exc:
            log.warning(f"[URE] contextual_fallbacks 正则无效: {pattern} ({exc})")
            continue
    return None


def _has_continue_signal(text: str) -> bool:
    return bool(CONTINUE_SIGNAL_PATTERN.search(re.sub(r"\s+", " ", (text or "")).strip()))


def _is_contact_request_message(text: str) -> bool:
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not candidate:
        return False
    return bool(CONTACT_REQUEST_PATTERN.search(candidate))


def _has_contact_stage_signal(text: str) -> bool:
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not candidate:
        return False
    return bool(CONTACT_STAGE_PATTERN.search(candidate))


def _is_reject_message(text: str) -> bool:
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not candidate or not REJECT_PATTERN.search(candidate):
        return False
    if _has_continue_signal(candidate):
        return False
    return True


def _is_ending_message(text: str) -> bool:
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not candidate or not END_PATTERN.search(candidate):
        return False
    if _has_continue_signal(candidate):
        return False
    return True


def build_contextual_fallback_reply(
    messages: list[dict],
    bio: str = "",
    age: int = 0,
    platform: str = "tinder",
    max_len: int = 50,
) -> Optional[str]:
    """
    分场景兜底：
    - 新配对：基于 bio 给一个轻量开场，不再硬发“忙晚点聊”
    - 已有对话：尽量给轻接话；若没有把握，再返回 None 交由上层跳过
    """
    del age

    bio_text = re.sub(r"\s+", " ", (bio or "")).strip()
    platform_key = str(platform or "").lower()
    if not messages:
        cue_text = bio_text.lower()
        english_bio = _looks_english_text(bio_text)
        if "声控" in bio_text or "voice" in cue_text or "music" in cue_text:
            return _clip_reply("Voice note energy already helps" if english_bio else "声控这句有点加分 我先来打个招呼", max_len)
        if any(token in cue_text for token in ("hr", "director", "top 500", "top500", "recruit")):
            return _clip_reply("You don't look easy to impress" if english_bio else "你这简介一看就不太好糊弄", max_len)
        if any(token in bio_text for token in ("长期", "认真", "结婚")) or any(
            token in cue_text for token in ("seriously", "long-term", "long term", "intentional")
        ):
            return _clip_reply("You seem pretty intentional already" if english_bio else "你这简介挺认真 我先来打个招呼", max_len)
        if any(token in bio_text for token in ("咖啡", "旅行", "狗")) or any(
            token in cue_text for token in ("coffee", "travel", "dog")
        ):
            return _clip_reply("Your profile does paint a picture" if english_bio else "先打个招呼 你这简介还挺有画面", max_len)
        if english_bio:
            return _clip_reply("You do have a memorable profile", max_len)
        return _clip_reply("先打个招呼 你这张有点会拍", max_len)

    partner_messages = [
        item or {}
        for item in (messages or [])
        if _is_contextual_partner_message(item or {})
    ]
    if not partner_messages:
        return None

    last_msg = partner_messages[-1]
    last_text = re.sub(r"\s+", " ", (last_msg.get("text", "") or "")).strip()
    if not last_text:
        return None

    recent_partner_text = " ".join(
        re.sub(r"\s+", " ", str((item or {}).get("text", "") or "")).strip()
        for item in partner_messages[-4:]
    )
    english = _looks_english_text(last_text) and not _contains_cjk_text(recent_partner_text or last_text)
    normalized = last_text.lower()

    if _contains_cjk_text(last_text) and "戒烟" in last_text:
        return _clip_reply("还在努力 你这监督来得挺及时", max_len)

    if any(token in last_text for token in ("谢谢", "多谢")) or "thank" in normalized:
        return _clip_reply("I'll take that" if english else "这句我先收下", max_len)

    if _contains_cjk_text(recent_partner_text) and any(token in recent_partner_text for token in ("照片", "本人", "可爱")):
        return _clip_reply("本人 但你这么夸我有点难接", max_len)

    if _contains_cjk_text(last_text) and "什么梗" in last_text:
        return _clip_reply("就是字面意思 看你愿不愿意接", max_len)

    if _contains_cjk_text(last_text) and "不证明" in last_text:
        return _clip_reply("行 那就慢慢看", max_len)

    configured = _configured_contextual_fallback(
        last_text,
        recent_partner_text,
        platform=platform_key,
        max_len=max_len,
    )
    if configured:
        return configured

    if (
        any(token in last_text for token in ("哪", "谁", "猜", "选哪个", "哪张"))
        or any(token in normalized.split() for token in ("who", "which"))
    ):
        return _clip_reply("I'll make you work a little for that" if english else "这个先卖个关子 你再猜一下", max_len)

    if any(token in normalized for token in ("hi", "hello", "hey")) or any(token in last_text for token in ("嗨", "哈喽")):
        if platform_key == "bumble" and any(token in bio_text for token in ("牛肉丸", "肉丸")):
            return _clip_reply("your name just made me hungry" if english else "你这个名字有点太下饭了", max_len)
        if platform_key == "bumble":
            return _clip_reply("hey, good timing" if english else "来得刚好 我接住了", max_len)
        return _clip_reply("Right on time" if english else "来得刚好 我接住了", max_len)

    # Do not send generic filler like "你这句有点意思" after LLM/repair failure.
    # Existing chats should either hit a strong context fallback above or skip.
    return None


def build_reactivation_fallback_reply(
    messages: list[dict],
    bio: str = "",
    platform: str = "tinder",
    max_len: int = 50,
) -> Optional[str]:
    """
    沉睡联系人激活用的保守兜底。
    只在已经判断“允许激活”的对话上使用。
    """
    del platform
    sanitized = sanitize_messages_for_context(messages)
    partner_text = _last_partner_text(sanitized)
    if not partner_text:
        return None

    bio_text = re.sub(r"\s+", " ", (bio or "")).strip()
    bio_lower = bio_text.lower()
    english = _looks_english_text(partner_text) or (
        bool(bio_text) and _looks_english_text(bio_text) and not re.search(r"[\u4e00-\u9fff]", partner_text)
    )
    lowered = partner_text.lower()

    if any(token in partner_text for token in ("咖啡", "coffee")):
        return _clip_reply("still blaming coffee for everything?" if english else "还在把锅甩给咖啡吗", max_len)
    if any(token in partner_text for token in ("旅行", "trip", "travel")):
        return _clip_reply("you still owe me that travel version" if english else "你还欠我一个旅行版答案", max_len)
    if any(token in partner_text for token in ("工作", "上班", "work")):
        return _clip_reply("so did work finally let you breathe?" if english else "所以工作后来肯放你一马了吗", max_len)

    if bio_text:
        if "声控" in bio_text or any(token in bio_lower for token in ("voice", "music")):
            return _clip_reply("still keeping the voice-note standard high?" if english else "声控标准现在还在线吗", max_len)
        if any(token in bio_text for token in ("长期", "认真", "结婚")) or any(
            token in bio_lower for token in ("seriously", "long-term", "long term", "intentional")
        ):
            return _clip_reply("still taking this seriously?" if english else "认真找这件事还在继续吗", max_len)
        if any(token in bio_text for token in ("咖啡", "旅行", "狗")) or any(
            token in bio_lower for token in ("coffee", "travel", "trip", "dog")
        ):
            return _clip_reply("you still owe me the profile version of that story" if english else "你这简介后面应该还有故事", max_len)
        if any(token in bio_lower for token in ("hr", "director", "top 500", "top500", "recruit")):
            return _clip_reply("did work ever give you a quiet minute?" if english else "工作后来给你留安静时间了吗", max_len)

    return _clip_reply("still owe me the rest of that story" if english else "你还欠我后半段故事", max_len)


def is_fallback_reply(text: str) -> bool:
    """判断一条消息是否为统一兜底回复。"""
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    return normalized == SAFE_FALLBACK_REPLY or normalized.lower() in {
        "right on time",
        "hey, good timing",
        "你这句有点意思",
        "这个想法有意思",
        "这句我先收下",
    }


def is_like_reaction_message(message_or_text) -> bool:
    """判断一条消息是否表示“对方点赞了我的消息”这类互动事件。"""
    if isinstance(message_or_text, dict):
        if str(message_or_text.get("meta_type", "") or "").strip() == "reaction_like":
            return True
        text = message_or_text.get("text", "")
    else:
        text = message_or_text
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return normalized in {
        REACTION_LIKE_CANONICAL_TEXT.lower(),
        "[赞了你的消息]",
        "赞了你的消息",
        "liked your message",
        "liked one of your messages",
        "reacted to your message",
    }


def build_reaction_ack_reply(
    messages: list[dict],
    *,
    max_len: int = 50,
) -> Optional[str]:
    """
    针对“点赞/反应”这类轻互动的专门回复。
    不再把它当普通文本硬喂给 LLM，避免出现奇怪残句。
    按语境分成三档：
    - 弱确认：普通点赞，轻轻接住
    - 轻调侃：对方点赞的是一句有梗/带玩笑感的话
    - 顺势接球：对话已经有来有回，点赞更像轻鼓励
    """
    sanitized = sanitize_messages_for_context(messages)
    if not sanitized or not is_like_reaction_message(sanitized[-1]):
        return None

    history = sanitized[:-1]
    english = False
    last_me_text = ""
    last_partner_text = ""
    partner_turns = 0
    non_reaction_count = 0

    for item in history:
        candidate = re.sub(r"\s+", " ", (item.get("text", "") or "")).strip()
        if not candidate or is_like_reaction_message(item):
            continue
        non_reaction_count += 1
        if item.get("sender") == "them":
            partner_turns += 1

    for item in reversed(history):
        candidate = re.sub(r"\s+", " ", (item.get("text", "") or "")).strip()
        if not candidate or is_like_reaction_message(item):
            continue
        english = _looks_english_text(candidate)
        break

    for item in reversed(history):
        candidate = re.sub(r"\s+", " ", (item.get("text", "") or "")).strip()
        if not candidate or is_like_reaction_message(item):
            continue
        if not last_me_text and (item.get("sender") == "me" or item.get("is_mine") is True):
            last_me_text = candidate
        elif not last_partner_text and item.get("sender") == "them":
            last_partner_text = candidate
        if last_me_text and last_partner_text:
            break

    last_me_lower = last_me_text.lower()
    playful_cues = (
        any(
            token in last_me_lower
            for token in (
                "approval", "vote", "deal", "trouble", "danger", "game",
                "quietly", "secretly", "bet", "plot", "cheeky", "owe",
            )
        )
        or any(
            token in last_me_text
            for token in ("算", "投票", "批准", "默认", "游戏", "赌", "评", "欠我", "偷偷")
        )
    )
    warm_context = (
        partner_turns >= 3
        and non_reaction_count >= 6
        and bool(last_partner_text)
        and len(last_partner_text) >= 4
        and last_partner_text not in LOW_INFO_REPLY_TEXTS
        and last_partner_text not in CONTACT_CONFIRM_REPLY_TEXTS
        and not CONTACT_REQUEST_PATTERN.search(last_partner_text)
        and not CONTACT_STAGE_PATTERN.search(last_partner_text)
        and not REJECT_PATTERN.search(last_partner_text)
        and not END_PATTERN.search(last_partner_text)
    )

    if playful_cues:
        if english:
            return _clip_reply("okay that felt like a quiet vote of confidence", max_len)
        return _clip_reply("行 这算你偷偷投了赞成票", max_len)

    if warm_context:
        if english:
            return _clip_reply("noted, I'll take that as encouragement", max_len)
        return _clip_reply("收到 那我就继续按这个方向发挥了", max_len)

    if english:
        return _clip_reply("I'll count that as a yes then", max_len)
    return _clip_reply("那我先当你默认了", max_len)


def _contains_prompt_marker(text: str) -> bool:
    candidate = str(text or "")
    if not candidate:
        return False
    markers = (
        "# 最近对话",
        "# 历史摘要",
        "# 对方资料",
        "# 对方年龄",
        "【任务】",
        "【语言规则】",
    )
    if any(marker in candidate for marker in markers):
        return True
    if re.search(r"(^|\s)#\s*(最近对话|历史摘要|对方资料|对方年龄)\b", candidate):
        return True
    return False


def _is_bumble_light_greeting_followup(
    messages: list[dict],
    *,
    last_text: str,
    platform: str | None = None,
) -> bool:
    """Bumble marks these as "Your Move"; a simple greeting is still actionable."""
    if str(platform or "").lower() != "bumble":
        return False
    normalized = re.sub(r"\s+", " ", str(last_text or "")).strip().lower()
    if normalized not in {item.lower() for item in GREETING_REPLY_TEXTS}:
        return False
    if len(messages) < 2:
        return True
    prev_msg = messages[-2] or {}
    if prev_msg.get("sender") != "me" and prev_msg.get("is_mine") is not True:
        return False
    prev_text = re.sub(r"\s+", "", str(prev_msg.get("text", "") or ""))
    # Only rescue the opening/low-commitment turn; longer prior messages keep
    # the normal low-info anti-chasing guard.
    return len(prev_text) <= 8


def should_skip_low_info_followup(messages: list[dict], *, platform: str | None = None) -> tuple[bool, str]:
    """
    统一的低信息回复拦截。

    规则：
    - 最后一条若是对方发来的单字/极短敷衍回复
    - 且倒数第二条是我方发送
    - 则判定为不继续追问
    """
    if not messages:
        return False, "无历史消息记录"

    last_msg = messages[-1] or {}
    if last_msg.get("sender") == "me" or last_msg.get("is_mine") is True:
        return False, "最后一条为我方发送"

    last_text = re.sub(r"\s+", " ", (last_msg.get("text", "") or "")).strip()
    if not last_text:
        return False, "最后一条为空"

    normalized = last_text.lower()
    is_affirmative = normalized in {item.lower() for item in AFFIRMATIVE_REPLY_TEXTS}
    stripped = re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)
    is_emoji_or_symbol_only = bool(normalized) and not stripped
    is_greeting_only = normalized in {item.lower() for item in GREETING_REPLY_TEXTS}
    is_contact_confirm = last_text in CONTACT_CONFIRM_REPLY_TEXTS
    # 只把“明确敷衍词 / 单字回复 / 纯招呼 / 纯 emoji / 联系方式确认”当成低信息，
    # 避免把“工作 / 上班”这类 2 字实义回答继续误杀。
    is_low_info = (
        last_text in LOW_INFO_REPLY_TEXTS
        or is_greeting_only
        or is_contact_confirm
        or is_emoji_or_symbol_only
        or (len(last_text) == 1 and not is_affirmative)
    )
    if not is_low_info:
        return False, "最后一条不是低信息回复"

    if is_greeting_only and _is_bumble_light_greeting_followup(messages, last_text=last_text, platform=platform):
        return False, "Bumble轻招呼入站，允许接住"

    if is_emoji_or_symbol_only and _is_warm_positive_emoji_followup(messages, last_text=last_text):
        return False, "正向emoji接住话题，允许继续互动"

    if len(messages) >= 2:
        prev_msg = messages[-2] or {}
        if prev_msg.get("sender") == "me" or prev_msg.get("is_mine") is True:
            return True, "对方敷衍且上一条也为我方发送，跳过追问"

    return False, "上一条不是我方发送"


def _is_warm_positive_emoji_followup(messages: list[dict], *, last_text: str = "") -> bool:
    """
    对已热起来对话里的正向单个 emoji 做轻量放行。

    目标：
    - 冷对话里的单个 emoji 继续按低信息处理
    - 有来有回后，😂 / 🤣 这类正向 emoji 视为轻互动信号，而不是直接判死
    """
    sanitized = sanitize_messages_for_context(messages)
    if len(sanitized) < 4:
        return False

    compact_last = re.sub(r"\s+", "", last_text or re.sub(r"\s+", " ", str((sanitized[-1] or {}).get("text", "") or "")).strip())
    if compact_last not in POSITIVE_EMOJI_REPLY_TEXTS:
        return False

    history = sanitized[:-1]
    me_turns = 0
    partner_turns = 0
    meaningful_partner_turns = 0
    last_me_text = ""

    for item in history:
        text = re.sub(r"\s+", " ", str((item or {}).get("text", "") or "")).strip()
        if not text or is_like_reaction_message(item):
            continue
        sender = item.get("sender", "")
        if sender == "me" or item.get("is_mine") is True:
            me_turns += 1
            last_me_text = text
            continue
        if sender != "them":
            continue
        partner_turns += 1
        compact = re.sub(r"\s+", "", text)
        normalized = text.lower()
        stripped = re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)
        is_emoji_only = bool(normalized) and not stripped
        if (
            text not in LOW_INFO_REPLY_TEXTS
            and text not in CONTACT_CONFIRM_REPLY_TEXTS
            and not is_emoji_only
            and len(compact) >= 2
            and not CONTACT_REQUEST_PATTERN.search(text)
            and not CONTACT_STAGE_PATTERN.search(text)
            and not _is_reject_message(text)
            and not _is_ending_message(text)
        ):
            meaningful_partner_turns += 1

    if me_turns < 2 or partner_turns < 2 or meaningful_partner_turns < 2:
        return False
    if not last_me_text or len(re.sub(r"\s+", "", last_me_text)) < 4:
        return False
    if _is_ending_message(last_me_text):
        return False
    return True


def should_reply_to_messages(messages: list[dict], *, platform: str | None = None) -> tuple[bool, str]:
    """
    双平台统一的业务级回复判断。

    规则：
    - 最后一条必须是对方发的
    - 单字/敷衍低信息回复不追问
    - 已进入联系方式阶段不自动接话
    - 拒绝意图/结束语不强行推进
    """
    if not messages:
        return False, "无历史消息记录"

    last_msg = messages[-1] or {}
    if last_msg.get("sender") == "me" or last_msg.get("is_mine") is True:
        return False, "最后一条为我方发送，跳过（防连发）"

    should_skip, reason = should_skip_low_info_followup(messages, platform=platform)
    if should_skip:
        return False, reason

    recent_text = " ".join(
        re.sub(r"\s+", " ", (item.get("text", "") or "")).strip()
        for item in messages[-4:]
        if (item.get("text", "") or "").strip()
    )
    last_text = re.sub(r"\s+", " ", (last_msg.get("text", "") or "")).strip()

    if _is_reject_message(last_text):
        return False, "探测到拒绝意图"

    if _is_contact_request_message(last_text):
        return False, "对方询问联系方式"

    if _has_contact_stage_signal(recent_text):
        return False, "近期已涉及微信互加阶段"

    if _is_ending_message(last_text):
        return False, "对方发结束语，不强行接话"

    return True, "允许回复"


def classify_partner_followup_quality(messages: list[dict], *, platform: str | None = None) -> tuple[str, str]:
    """
    对“我方发出后，对方后续真实回应”做本地质量标签。

    返回：
    - partner_followup_low_info
    - partner_followup_question
    - partner_followup_engaged
    - partner_followup_basic
    """
    if not messages:
        return "partner_followup_basic", "无历史消息"

    last_msg = messages[-1] or {}
    if last_msg.get("sender") == "me" or last_msg.get("is_mine") is True:
        return "partner_followup_basic", "最后一条不是对方消息"

    should_skip, reason = should_skip_low_info_followup(messages, platform=platform)
    if should_skip:
        return "partner_followup_low_info", reason

    last_text = re.sub(r"\s+", " ", (last_msg.get("text", "") or "")).strip()
    lowered = last_text.lower()
    if not last_text:
        return "partner_followup_low_info", "对方回复为空"

    if _is_warm_positive_emoji_followup(messages, last_text=last_text):
        return "partner_followup_basic", "对方用正向emoji接住当前氛围"

    question_mark = "?" in last_text or "？" in last_text
    question_cues = (
        "吗", "呢", "怎么", "为什么", "啥", "什么", "how", "why", "what", "when", "where",
        "which", "who", "u ", "you "
    )
    if question_mark or any(cue in lowered for cue in question_cues):
        return "partner_followup_question", "对方继续提问或追问"

    non_space_len = len(re.sub(r"\s+", "", last_text))
    inbound_count = sum(
        1
        for item in messages
        if item.get("sender") != "me" and item.get("is_mine") is not True and str(item.get("text", "")).strip()
    )
    engaged_cues = ("because", "but", "哈哈", "lol", "actually", "不过", "但是", "所以", "then")
    if non_space_len >= 10 or inbound_count >= 2 or any(cue in lowered for cue in engaged_cues):
        return "partner_followup_engaged", "对方给出较完整的继续互动回复"

    return "partner_followup_basic", "对方有正常回应"


def _parse_iso_datetime(raw: str) -> Optional[datetime]:
    candidate = str(raw or "").strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except Exception:
        return None


def should_attempt_reactivation(
    messages: list[dict],
    *,
    dormant_since: str = "",
    last_reactivation_at: str = "",
    reactivation_attempt_count: int = 0,
    min_dormant_hours: int = DEFAULT_REACTIVATION_MIN_DORMANT_HOURS,
    min_reactivation_gap_hours: int = DEFAULT_REACTIVATION_GAP_HOURS,
    max_reactivation_attempts: int = DEFAULT_REACTIVATION_MAX_ATTEMPTS,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """
    判断一段“我方最后发言”的沉睡对话是否值得尝试重新激活。

    设计目标：
    - 与正常“未回复消息”链路分开
    - 只允许低压力、顺着旧话题的轻量重启
    - 明确排除联系方式、拒绝、结束等不该再追的对话
    """
    if not messages:
        return False, "无历史消息记录"

    now_dt = now or datetime.now()

    sanitized = sanitize_messages_for_context(messages)
    if len(sanitized) < 2:
        return False, "消息轮次过少，不做激活"

    last_msg = sanitized[-1] or {}
    if last_msg.get("sender") != "me" and last_msg.get("is_mine") is not True:
        return False, "最后一条不是我方发送，不属于沉睡激活"

    last_text = re.sub(r"\s+", " ", (last_msg.get("text", "") or "")).strip()
    if not last_text or is_fallback_reply(last_text):
        return False, "最后一条不适合拿来做激活承接"

    partner_messages = [
        msg for msg in sanitized
        if msg.get("sender") == "them" and re.sub(r"\s+", " ", (msg.get("text", "") or "")).strip()
    ]
    if len(partner_messages) < 1:
        return False, "对方没有有效历史消息，不做激活"

    if len(sanitized) < 4:
        return False, "对话过短，激活价值不足"

    recent_history_text = "\n".join(
        re.sub(r"\s+", " ", (msg.get("text", "") or "")).strip()
        for msg in sanitized[-6:]
        if re.sub(r"\s+", " ", (msg.get("text", "") or "")).strip()
    )
    if _has_contact_stage_signal(recent_history_text):
        return False, "已进入联系方式阶段，不做激活"
    if any(_is_contact_request_message(re.sub(r"\s+", " ", (msg.get("text", "") or "")).strip()) for msg in sanitized[-4:]):
        return False, "近期已进入联系方式阶段，不做激活"
    if any(_is_reject_message(re.sub(r"\s+", " ", (msg.get("text", "") or "")).strip()) for msg in sanitized[-4:]):
        return False, "存在拒绝信号，不做激活"
    if _is_ending_message(last_text):
        return False, "我方最后一句已是结束语，不再追发"

    latest_partner_text = re.sub(r"\s+", " ", (partner_messages[-1].get("text", "") or "")).strip()
    low_info_blocked = latest_partner_text.lower() in {item.lower() for item in CONTACT_CONFIRM_REPLY_TEXTS}
    if latest_partner_text in LOW_INFO_REPLY_TEXTS or low_info_blocked:
        return False, "最近一次对方回复过低信息，不做激活"

    if reactivation_attempt_count >= max_reactivation_attempts:
        return False, f"激活次数已达上限({max_reactivation_attempts})"

    dormant_since_dt = _parse_iso_datetime(dormant_since)
    if dormant_since_dt is None:
        return False, f"沉睡计时未开始，等待{min_dormant_hours}h"

    if dormant_since_dt.tzinfo is not None and now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=dormant_since_dt.tzinfo)
    elif dormant_since_dt.tzinfo is None and now_dt.tzinfo is not None:
        dormant_since_dt = dormant_since_dt.replace(tzinfo=now_dt.tzinfo)

    dormant_age = now_dt - dormant_since_dt
    if dormant_age < timedelta(hours=min_dormant_hours):
        hours_left = max(1, int((timedelta(hours=min_dormant_hours) - dormant_age).total_seconds() // 3600))
        return False, f"沉睡时长不足{min_dormant_hours}h，还需约{hours_left}h"

    last_reactivation_dt = _parse_iso_datetime(last_reactivation_at)
    if last_reactivation_dt is not None:
        if last_reactivation_dt.tzinfo is not None and now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=last_reactivation_dt.tzinfo)
        elif last_reactivation_dt.tzinfo is None and now_dt.tzinfo is not None:
            last_reactivation_dt = last_reactivation_dt.replace(tzinfo=now_dt.tzinfo)

        reactivation_gap = now_dt - last_reactivation_dt
        if reactivation_gap < timedelta(hours=min_reactivation_gap_hours):
            hours_left = max(1, int((timedelta(hours=min_reactivation_gap_hours) - reactivation_gap).total_seconds() // 3600))
            return False, f"距离上次激活不足{min_reactivation_gap_hours}h，还需约{hours_left}h"

    return True, "允许激活"

# ─────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────
@dataclass
class PlatformAdapter:
    """平台特有的 UI 交互逻辑"""
    name: str                     # "tinder" | "bumble" | ...
    click_selector: str            # 进入对话的主选择器
    click_offset_x: int = 0       # 坐标偏移（px）
    click_offset_y: int = 0
    requires_uid: bool = False     # 是否需要 data-qa-uid 精准定位
    notification_panel_sel: str = ""  # 通知面板选择器（需关闭）


ADAPTERS: dict[str, PlatformAdapter] = {
    "tinder": PlatformAdapter(
        name="tinder",
        click_selector=".matchListItem",
        click_offset_x=0,
        click_offset_y=0,
        requires_uid=False,
    ),
    "bumble": PlatformAdapter(
        name="bumble",
        click_selector="[data-qa-uid]",        # 精确到 uid
        click_offset_x=-120,                   # 左偏 120px 避开 sidebar 遮挡
        click_offset_y=0,
        requires_uid=True,
        notification_panel_sel=".page__request-panel",
    ),
}


# ─────────────────────────────────────────────────────────────────
# 策略配置加载
# ─────────────────────────────────────────────────────────────────
def load_strategy() -> dict:
    """从共享路径加载 strategy_config.json"""
    if not SHARED_CFG.exists():
        log.warning(f"策略文件不存在: {SHARED_CFG}，使用空配置")
        return {}
    with open(SHARED_CFG, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_examples(strategy: dict) -> list[dict]:
    """从 success_patterns 提取示例（兼容结构化与自然语言说明两种格式）"""
    examples = []
    for p in strategy.get("success_patterns", []):
        if isinstance(p, dict) and p.get("example"):
            examples.append({
                "them": p.get("pattern", ""),
                "me":   p.get("example", ""),
            })
        elif isinstance(p, str):
            # 兼容当前 strategy_config.json 中的自然语言总结格式
            them = ""
            me = ""

            me_match = re.search(r"Me用[\"'‘“](.+?)[\"'’”]", p)
            if me_match:
                me = me_match.group(1).strip()

            them_match = re.search(r"Them对[\"'‘“](.+?)[\"'’”]", p)
            if them_match:
                them = them_match.group(1).strip()

            if not them:
                if "加班" in p:
                    them = "还在加班"
                elif "干鲑鱼" in p:
                    them = "干鲑鱼"
                else:
                    desc = p.split("：", 1)[-1].strip() if "：" in p else p.strip()
                    them = desc[:24]

            if not me and "共情" in p and them:
                me = "你这描述还挺有画面"

            if me:
                examples.append({
                    "them": them,
                    "me": me,
                })
    # 兼容旧格式（顶级 success_examples 列表）
    for ex in strategy.get("success_examples") or []:
        if isinstance(ex, dict) and "pattern" in ex:
            examples.append({
                "them": ex.get("pattern", ""),
                "me":   ex.get("example", ""),
            })
    return examples[:5]  # 最多 5 条


def _get_failure_examples(strategy: dict) -> list[dict]:
    """从 failure_patterns 提取高信号反例，供 prompt 做轻量负面约束。"""
    examples = []
    for item in strategy.get("failure_patterns", []):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", "") or "").strip()
        root_cause = str(item.get("root_cause", "") or "").strip()
        example = str(item.get("example", "") or "").strip()
        if not pattern or not example:
            continue
        examples.append({
            "pattern": pattern,
            "example": example,
            "root_cause": root_cause,
        })
    return examples[:3]


def _merge_system_prompt_rules(strategy: dict) -> tuple[str, list[str], list[str]]:
    """
    合并硬编码默认规则与 strategy.system_prompt 可选覆写。
    strategy 允许补充 role/core_rules/forbidden_tones，但不再悄悄失效。
    """
    default_role = (
        os.getenv("APP_PERSONA_ROLE")
        or "你是一个务实内敛、表达简练、低需求感的中文社交聊天助手。"
        "沟通风格成熟稳重，带有适当冷幽默。绝对不用轻浮、讨好或过度文艺的辞藻。"
        "用词平实，陈述客观事实，绝不说教。"
    )
    default_core_rules = [
        "无需求感：不解释、不自证、不讨好",
        "两极化：直接展示态度，不惧拒绝",
        "否定法：用“不”或反问回应测试，不陷入解释模式",
        "格式：极简短句。必须保证句子结构完整，严禁话说一半中断。句末不要加标点，句中可使用空格或逗号分隔。",
        "低需求感，优先不追问；需要提问时最多一个轻量自然的问题",
        "语言必须跟随对方最近一条消息或资料的主要语言",
    ]
    default_forbidden = [
        "禁止解释性、自证清白、寻求认可的话语",
    ]

    strategy_prompt = strategy.get("system_prompt")
    if not isinstance(strategy_prompt, dict):
        return default_role, default_core_rules, default_forbidden

    role = strategy_prompt.get("role") or default_role

    def _merge(defaults: list[str], extra) -> list[str]:
        merged = list(defaults)
        if isinstance(extra, list):
            for item in extra:
                if isinstance(item, str) and item.strip() and item not in merged:
                    merged.append(item.strip())
        return merged

    core_rules = _merge(default_core_rules, strategy_prompt.get("core_rules"))
    forbidden = _merge(default_forbidden, strategy_prompt.get("forbidden_tones"))
    return role, core_rules, forbidden


def _build_profile_prompt_summary() -> str:
    """
    将 PROFILE.md 提炼成更适合 system prompt 的短摘要，避免把整份分析文档原样注入。
    """
    profile_path = Path(__file__).parent.parent / "tinder-automation" / "PROFILE.md"
    if not profile_path.exists():
        return ""

    try:
        raw = profile_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    def _pick(pattern: str) -> str:
        match = re.search(pattern, raw, re.MULTILINE)
        return match.group(1).strip() if match else ""

    name = _pick(r'\| 名字 \| ([^|]+) \|')
    age = _pick(r'\| 年龄 \| ([^|]+) \|')
    location = _pick(r'\| 地点 \| ([^|]+) \|')
    intention = _pick(r'\| 择偶意向 \| ([^|]+) \|')
    tags = _pick(r'## 兴趣爱好标签\s+([^\n]+)')

    bio_1 = _pick(r'\*\*Bio 1（破冰句）：\*\*\s*>\s*"([^"]+)"')
    bio_2_q = _pick(r'\*\*Bio 2（Q&A）：\*\*\s*>\s*Q：([^\n]+)')
    bio_2_a = _pick(r'\*\*Bio 2（Q&A）：\*\*[\s\S]*?>\s*A：([^\n]+)')

    lines = []
    if name or age or location or intention:
        lines.append(f"- 基本信息：{name or '我'} {age or ''} {location or ''} {intention or ''}".strip())
    if tags:
        lines.append(f"- 兴趣标签：{tags}")
    if bio_1:
        lines.append(f"- 公开 bio：{bio_1}")
    if bio_2_q or bio_2_a:
        lines.append(f"- Q&A 风格：{bio_2_q} / {bio_2_a}".strip(" /"))

    lines.extend([
        "- 人设气质：务实内敛 冷幽默 不讨好 偏精神层面的交流",
        "- 表达边界：平实直接 不轻浮 不过度文艺 不说教",
        "- 引用边界：个人设定只用于内在心智模型，除非对方明确提问，否则不要主动抛出或硬引申",
    ])

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# Prompt 构造
# ─────────────────────────────────────────────────────────────────
def build_static_system_prompt(
    platform: str,
    strategy: dict,
) -> str:
    """
    构建静态系统前缀（字节级一致，用于 Context Cache 命中）。
    以硬编码默认规则为主，允许 strategy.system_prompt 做补充配置。
    """
    examples = _get_examples(strategy)
    failure_examples = _get_failure_examples(strategy)
    role, core_rules, forbidden = _merge_system_prompt_rules(strategy)
    profile_summary = _build_profile_prompt_summary()
    few_shot = "\n".join(
        "对方: " + ex.get('them', '') + "\n" + "我: " + ex.get('me', '')
        for ex in examples[:3]
    )
    sections = [
        f"[Platform: {platform.upper()}]",
        "# 角色定义\n" + role,
    ]

    if profile_summary:
        sections.append("# 个人资料摘要\n" + profile_summary)

    rules_block = "".join(f"- {rule}\n" for rule in core_rules + forbidden).rstrip()
    sections.append("# 核心规则\n" + rules_block)

    if few_shot:
        sections.append("# Few-Shot 示例（从 strategy 提取）\n" + few_shot)

    if failure_examples:
        anti_examples = []
        for item in failure_examples:
            line = f"- 避免：{item.get('pattern', '')}；不要回：{item.get('example', '')}"
            root_cause = item.get("root_cause", "")
            if root_cause:
                line += f"；原因：{root_cause}"
            anti_examples.append(line)
        sections.append("# Failure Patterns（避免失败反例）\n" + "\n".join(anti_examples))

    sections.append(
        "【输出格式】\n"
        "仅返回一个 JSON 对象：{\"reply\":\"最终回复\"}\n"
        "- 第一个字符必须是 {，最后一个字符必须是 }，禁止输出任何前缀或后缀\n"
        "- JSON 对象外禁止出现任何文字、Markdown、代码块、注释\n"
        "- 只允许一个根键：reply\n"
        "- reply 的值只能是最终回复字符串，禁止分析、解释、建议、步骤、示例\n"
        "- 禁止出现明显分析元话术：比如、例如、建议、我认为、首先、然后、最后、综上、结论、分析\n"
        "- 禁止输出英文脚手架元文本：Possible responses、Options、Let me go with、Here are\n"
        "- 禁止回显 prompt 片段或标题：# 最近对话、# 对方资料、# 对方年龄、【任务】、【语言规则】\n"
        "- 禁止出现：「」【】《》（）等包裹分析文本的符号\n"
        "- 回复长度必须 ≤ 50 个字符\n"
        "- 仅允许口语化短句，语言必须跟随对方最近一条消息\n"
        "- 对方说英文就必须回英文\n"
        "- 若无法安全完成，也只能输出：{\"reply\":\"这会儿有点忙，晚点聊\"}\n"
        "格式示例：{\"reply\":\"这个想法有意思\"}"
    )

    return "\n\n".join(sections)


def build_dynamic_user_prompt(
    messages: list[dict],
    bio: str,
    age: int,
    platform: str,
    strategy: dict,
    intent: str = "reply",
) -> str:
    """
    构建动态用户内容（每次调用都变化，作为 messages 数组尾端）。
    包含：聊天历史、bio、任务指令、滚动摘要缓冲。
    """
    sanitized_messages = _sanitize_messages_for_prompt(messages)

    # ── 滚动摘要缓冲（memory/recent_summary.md）───────────────
    summary_content = ""
    summary_path = Path(__file__).parent.parent / "memory" / "recent_summary.md"
    if summary_path.exists():
        try:
            summary_content = summary_path.read_text().strip()
        except Exception:
            summary_content = ""

    if not sanitized_messages:
        chat_history = "状态: 新配对，暂无聊天记录。"
        task_directive = (
            "当前是新配对，请仅根据对方资料（bio），"
            "生成一句幽默、简短或引发好奇的破冰开场白。"
            "优先调侃或制造悬念，也允许使用一个轻量、自然的问题，但不要像审问。"
        )
        language_rule = "无聊天记录时，以对方资料（bio）的主要语言回复。"
    else:
        chat_history = "\n".join(
            f"{'我' if m['sender'] == 'me' else '对方'}: {m['text']}"
            for m in sanitized_messages[-8:]
        )
        if intent == "reactivation":
            task_directive = (
                "这段对话已经沉默一段时间，最后一句是我方发出的。"
                "请顺着旧话题，生成一条低压力、自然、轻量的重新激活消息。"
                "不要重复上一句，不要说在吗，不要解释消失，不要显得催促。"
                "优先像轻接梗/轻续话，而不是重新自我介绍。"
            )
            language_rule = "有聊天记录时，以对方最近一条有效消息的主要语言回复；对方说英文就必须回英文。"
        else:
            task_directive = "根据上述最近对话上下文，生成一条自然的回复。"
            language_rule = "有聊天记录时，以对方最近一条消息的主要语言回复；对方说英文就必须回英文。"

    # ── 组装动态 Prompt ──────────────────────────────────────
    parts = [
        "# 最近对话\n",
        chat_history,
    ]
    if summary_content:
        parts.extend(["\n\n# 历史摘要（已压缩）\n", summary_content])
    # 年龄注入（仅当 age > 0 时）
    age_line = f"\n\n# 对方年龄\n{age}岁" if age > 0 else ""

    parts.extend([
        "\n\n# 对方资料\n",
        bio or "无",
        age_line,
        "\n\n【任务】\n",
        task_directive,
        "\n\n【语言规则】\n",
        language_rule,
        "先判断对方主要语言，再直接输出最终回复，不要补充解释。",
    ])
    return "".join(parts)


def build_prompt(
    messages: list[dict],
    bio: str,
    age: int,
    platform: str,
    strategy: dict,
    intent: str = "reply",
) -> str:
    """
    构造完整的 LLM Prompt（兼容旧调用方式）。
    """
    static_sp = build_static_system_prompt(platform, strategy)
    dynamic_up = build_dynamic_user_prompt(messages, bio, age, platform, strategy, intent=intent)
    return static_sp + "\n\n" + dynamic_up


def _sanitize_messages_for_prompt(messages: list[dict]) -> list[dict]:
    """过滤历史中的脏回复/元文本，避免污染下一轮生成。"""
    sanitized: list[dict] = []
    removed = 0

    for item in messages or []:
        sender = item.get("sender", "")
        text = re.sub(r"\s+", " ", (item.get("text", "") or "")).strip()
        if not text:
            continue

        should_drop = False
        if sender == "me":
            normalized = text.lower().strip()
            if (
                is_fallback_reply(text)
                or _contains_prompt_marker(text)
                or _looks_like_analysis(text)
                or normalized.startswith("possible responses")
                or normalized.startswith("possible response")
                or normalized.startswith("response options")
                or normalized.startswith("options:")
                or normalized.startswith("let me go with")
                or normalized.startswith("here are")
                or normalized.startswith("let me analyze")
            ):
                should_drop = True

        if should_drop:
            removed += 1
            continue

        sanitized.append({
            **item,
            "text": text,
        })

    if removed:
        log.warning(f"[URE] 过滤历史污染消息 {removed} 条，避免进入下一轮 prompt")

    return sanitized


def sanitize_messages_for_context(messages: list[dict]) -> list[dict]:
    """对运行时上下文做统一清洗，过滤历史污染/脚手架/兜底残留。"""
    return _sanitize_messages_for_prompt(messages)


# ─────────────────────────────────────────────────────────────────
# LLM 调用（统一推理模型处理）
# ─────────────────────────────────────────────────────────────────
def _call_llm(
    static_system_prompt: str,
    dynamic_user_prompt: str,
) -> str:
    """
    发送 LLM 请求，返回原始文本。
    Payload 结构：
      [0] {"role": "system",  "content": <静态系统前缀>}
      [1] {"role": "user",     "content": <动态业务数据>}
    两段内容严格分离，确保 MiniMax Context Cache 命中小节。
    """
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": static_system_prompt},
            {"role": "user",   "content": dynamic_user_prompt},
        ],
        "max_tokens": config.llm.max_tokens,
        "temperature": config.llm.temperature,
    }).encode()

    last_err = None
    for attempt in range(LLM_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                LLM_BASE_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                data = json.loads(resp.read())
                msg  = data["choices"][0]["message"]
                rc      = msg.get("reasoning_content") or ""
                content = msg.get("content") or ""
                full = "\n".join(part for part in (rc, content) if part).strip()
                reply = _extract_llm_reply(rc, content)
                if reply == SAFE_FALLBACK_REPLY and full:
                    repaired = _repair_reply_output(
                        static_system_prompt,
                        dynamic_user_prompt,
                        full,
                    )
                    if repaired:
                        return repaired
                return reply

        except Exception as e:
            last_err = e
            # 扩展重试逻辑：支持 529、超时、网络错误
            should_retry = (
                attempt < LLM_MAX_RETRIES - 1 and (
                    "529" in str(e) or
                    "timeout" in str(e).lower() or
                    "connection" in str(e).lower() or
                    "network" in str(e).lower()
                )
            )
            if should_retry:
                delay = 5 * (2 ** attempt)
                log.warning(f"[URE] LLM 错误 (attempt {attempt+1}/{LLM_MAX_RETRIES}): {e}, 等待 {delay}s")
                time.sleep(delay)
            else:
                log.error(f"[URE] LLM 错误: {e}")
                break

    # ── 所有重试耗尽，尝试 DeepSeek fallback ──────────────
    fallback_key = getattr(config.llm, 'fallback_api_key', '')
    if fallback_key:
        log.warning(f"[URE] 主模型超时，尝试 DeepSeek fallback...")
        fallback_payload = json.dumps({
            "model": getattr(config.llm, 'fallback_model', 'deepseek-chat'),
            "messages": [
                {"role": "system", "content": static_system_prompt},
                {"role": "user",   "content": dynamic_user_prompt},
            ],
            "max_tokens": config.llm.max_tokens,
            "temperature": config.llm.temperature,
        }).encode()
        fb_url = str(getattr(config.llm, 'fallback_base_url', 'https://api.deepseek.com/v1') or "").rstrip("/")
        if fb_url.endswith("/v1"):
            fb_url = f"{fb_url}/chat/completions"
        try:
            req = urllib.request.Request(
                fb_url,
                data=fallback_payload,
                headers={
                    "Authorization": f"Bearer {fallback_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                msg = data["choices"][0]["message"]
                rc = msg.get("reasoning_content", "") or ""
                content = data["choices"][0]["message"].get("content", "") or ""
                full = "\n".join(part for part in (rc, content) if part).strip()
                reply = _extract_llm_reply(rc, content)
                if reply == SAFE_FALLBACK_REPLY and full:
                    repaired = _repair_reply_output(
                        static_system_prompt,
                        dynamic_user_prompt,
                        full,
                        model=getattr(config.llm, 'fallback_model', 'deepseek-chat'),
                        base_url=fb_url,
                        api_key=fallback_key,
                        timeout=60,
                    )
                    if repaired:
                        log.warning(f"[URE] ✅ DeepSeek fallback 修复输出格式成功")
                        return repaired
                if reply:
                    log.warning(f"[URE] ✅ DeepSeek fallback 成功")
                    return reply
        except Exception as fb_err:
            log.error(f"[URE] DeepSeek fallback 也失败: {fb_err}")

    raise RuntimeError(f"LLM 调用失败: {last_err}")


def _extract_structured_reply(full_text: str) -> str:
    """优先从结构化 JSON 输出中提取 reply 字段。"""
    if not full_text:
        return ""

    def _parse_candidate(candidate: str) -> str:
        text = str(candidate or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except Exception:
            return ""
        if not isinstance(payload, dict):
            return ""
        reply = payload.get("reply")
        if not isinstance(reply, str):
            return ""
        return reply.strip()

    candidates: list[str] = []
    stripped = full_text.strip()
    if stripped:
        candidates.append(stripped)

    for fenced in re.findall(r"```(?:json)?\s*([\s\S]*?)```", full_text, re.IGNORECASE):
        if fenced.strip():
            candidates.append(fenced.strip())

    start = None
    depth = 0
    for idx, ch in enumerate(full_text):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                snippet = full_text[start:idx + 1].strip()
                if "\"reply\"" in snippet or "'reply'" in snippet:
                    candidates.append(snippet)
                start = None

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        reply = _parse_candidate(candidate)
        if reply:
            return reply

    return ""


def _extract_llm_reply(reasoning_content: str, content: str, max_len: int = 50) -> str:
    """统一主模型与 fallback 的回复提取逻辑。优先要求结构化 reply 字段。"""
    full = "\n".join(part for part in (reasoning_content, content) if part).strip()
    structured = _extract_structured_reply(full)
    if structured:
        return sanitize_reply_for_send(structured, max_len=max_len)

    log.warning(f"[URE] ⚠️ LLM 输出缺少结构化 reply 字段，直接降级: {full[:120]}")
    return SAFE_FALLBACK_REPLY


def _repair_reply_output(
    static_system_prompt: str,
    dynamic_user_prompt: str,
    broken_output: str,
    max_len: int = 50,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int | float | None = None,
) -> str:
    """
    当模型没有按结构化 reply 协议输出时，追加一次超短修复请求，
    把错误输出压缩成唯一的可发送回复；修不出来则返回空串。
    """
    if not broken_output.strip():
        return ""

    repair_system_prompt = (
        f"{static_system_prompt}\n\n"
        "【格式修复】\n"
        "你上一次输出了分析或格式错误文本。\n"
        f"现在只能返回一个 JSON 对象：{{\"reply\":\"最终回复\"}}，且 reply 不超过 {max_len} 个字符。\n"
        f"如果无法从错误输出中恢复有效回复，就返回 {{\"reply\":\"{SAFE_FALLBACK_REPLY}\"}}。"
    )
    repair_user_prompt = (
        f"{dynamic_user_prompt}\n\n"
        "【上一次错误输出】\n"
        f"{broken_output}\n\n"
        "【现在只做一件事】\n"
        "删除所有分析、列表、标题、解释、元文本。\n"
        "只保留一个适合直接发送给对方的最终回复，并输出为严格 JSON：{\"reply\":\"...\"}。"
    )

    payload = json.dumps({
        "model": model or LLM_MODEL,
        "messages": [
            {"role": "system", "content": repair_system_prompt},
            {"role": "user", "content": repair_user_prompt},
        ],
        "max_tokens": min(config.llm.max_tokens, 256),
        "temperature": min(config.llm.temperature, 0.4),
    }).encode()

    try:
        req = urllib.request.Request(
            base_url or LLM_BASE_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key or LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or min(LLM_TIMEOUT, 45)) as resp:
            data = json.loads(resp.read())
            msg = data["choices"][0]["message"]
            rc = msg.get("reasoning_content") or ""
            content = msg.get("content") or ""
            repair_full = "\n".join(part for part in (rc, content) if part).strip()
            repaired = _extract_structured_reply(repair_full)
            if not repaired:
                log.warning(f"[URE] 修复输出仍缺少结构化 reply 字段，放弃采纳: {repair_full[:120]}")
            if not repaired:
                return ""
            cleaned = sanitize_reply_for_send(repaired, max_len=max_len)
            if cleaned and cleaned != SAFE_FALLBACK_REPLY:
                log.warning(f"[URE] ✅ LLM 输出格式修复成功: {cleaned[:40]}")
                return cleaned
    except Exception as repair_err:
        log.warning(f"[URE] 输出格式修复失败: {repair_err}")

    return ""

# ─────────────────────────────────────────────────────────────────
# 清洗规范
# ─────────────────────────────────────────────────────────────────
CLEAN_RE = [
    (re.compile(r'^[\[【]?(Me|Them|我|他)[\]:]\s*'), ""),
    (re.compile(r'^[\[【]?(回复?|Response)[\]:]\s*'), ""),
    (re.compile(r'^[【\[](.*?)[】\]]$'), r"\1"),
]


def clean_reply(raw: str) -> str:
    """清洗 LLM 原始输出为合规回复文本。"""
    text = raw.strip().strip("\n").strip('"').strip("'")
    # 替换换行为 / 分隔
    text = re.sub(r"\s*\n\s*", " / ", text)
    # 应用各条正则
    for pattern, replacement in CLEAN_RE:
        text = pattern.sub(replacement, text)
    return text.strip() if text else ""


def _strip_analysis_prefix(text: str) -> str:
    """尝试去掉前置分析话术，保留可能的最终回复。"""
    if not text:
        return ""

    candidate = clean_reply(text)
    candidate = re.sub(r'</?reply>', '', candidate, flags=re.IGNORECASE).strip()
    candidate = re.sub(r'保持简短.*?以内[。，]?', '', candidate)
    candidate = re.sub(r'keep it under\s*\d+\s*characters\.?\s*', '', candidate, flags=re.IGNORECASE)
    candidate = re.sub(r'under\s*\d+\s*characters\.?\s*', '', candidate, flags=re.IGNORECASE)
    candidate = candidate.replace('<', '').replace('>', '').strip()

    prefix_patterns = [
        r'^(?:思路|想法|分析|回复|策略|结论|总结)[：:,， ]*',
        r'^["\-].{0,20}(?:思路|想法|分析|回复|策略|决定|考虑)[：:,， ]*',
        r'^(?:我决定|我选择|我现在意识到)[：:,， ]*',
        r'^(?:我可以|我会这样回|我会这样说)[：:,， ]+',
        r'^(?:考虑到|注意到|发现)[：:,， ]*',
        r'^(?:Possible responses?|Possible reply|Response options?|Options?)[：:,， ]*',
        r'^(?:Let me go with|I[\' ]?ll go with|Here(?: are|\'s))(?:[：:,， ]+)?',
    ]
    for pattern in prefix_patterns:
        stripped = re.sub(pattern, '', candidate).strip()
        if stripped and stripped != candidate:
            log.warning(f"[URE] ⚠️ 截断分析前缀: {candidate[:40]} -> {stripped[:40]}")
            candidate = stripped
            break

    explanatory_patterns = [
        r'^(?:她是在|对方是在|她可能是|对方可能是|说明她|说明对方|表示她|表示对方).{0,24}?\s+',
        r'^(?:这是在|这说明|这表示).{0,24}?\s+',
    ]
    for pattern in explanatory_patterns:
        stripped = re.sub(pattern, '', candidate).strip()
        if stripped and stripped != candidate:
            log.warning(f"[URE] ⚠️ 剥离说明句前缀: {candidate[:40]} -> {stripped[:40]}")
            candidate = stripped
            break

    return candidate


def _looks_like_analysis(text: str) -> bool:
    """判断文本是否仍像思维链/策略分析，而不是可直接发送的回复。"""
    if not text:
        return False

    candidate = re.sub(r"\s+", " ", text).strip()
    if not candidate:
        return False

    analysis_patterns = [
        r'^\s*[-*•]\s*',
        r'^\s*\d+\.\s*',
        r'(^|\s)#\s*(最近对话|历史摘要|对方资料|对方年龄)\b',
        r'(# 最近对话|# 历史摘要|# 对方资料|# 对方年龄|【任务】|【语言规则】)',
        r'^(?:思路|想法|分析|策略|结论|总结|推理)(?:[：:,， ]|$)',
        r'(?:思路|想法|分析|策略|结论|总结|推理)[：:]',
        r'(我决定|我选择|我现在意识到|考虑到|说明她|说明对方|发现她|她可能|她是在)',
        r'^我可以[：:]',
        r'(?:我|更)倾向于(?:选择|用|说)',
        r'^(?:我会选择|我会说|我会回)[：:,， ]?',
        r'(回应.*话题|关于.*话题|不要太主动|自然的方式|冷淡但有点幽默|用一种)',
        r'(可以用|比如|例如|综上)',
        r'(疑问|困惑|调侃|测试|展示态度|低需求感|破冰|推进)',
        r'["“][^"”]{2,20}["”].{0,20}(说明|表示|意味着)',
        r'^(possible responses?|possible reply|response options?|options?)\s*:?\s*$',
        r'^(possible responses?|response options?|options?)\s*:',
        r'^(let me go with|i[\' ]?ll go with|here(?: are|\'s))\b',
        r'\b(candidate|draft reply|final reply|recommended reply)\b',
        r'keep it under\s*\d+\s*characters',
    ]
    if any(re.search(pattern, candidate, re.IGNORECASE) for pattern in analysis_patterns):
        return True

    if _contains_prompt_marker(candidate):
        return True

    if candidate.endswith(":") or candidate.endswith("："):
        return True

    low_information_meta = {
        "possible responses",
        "possible response",
        "possible reply",
        "response options",
        "options",
        "let me go with",
        "i'll go with",
        "ill go with",
        "here are",
        "here's",
    }
    normalized = re.sub(r"[:：\s]+", " ", candidate.lower()).strip(" .,!?")
    if normalized in low_information_meta:
        return True

    # 真实回复一般不会又长又像完整说明句
    if len(candidate) > 50:
        return True

    return False


ENGLISH_SENTENCE_VERBS = {
    "am", "is", "are", "was", "were", "be", "being", "been",
    "do", "does", "did", "done", "doing",
    "have", "has", "had",
    "can", "could", "will", "would", "should", "may", "might", "must",
    "like", "love", "hate", "want", "need", "mean", "guess", "know", "take",
    "get", "got", "gotta", "understand", "care", "prefer", "tolerate",
    "sounds", "sound", "feels", "feel", "looks", "look", "seems", "seem",
}

ENGLISH_SHORT_REPLY_ALLOWLIST = {
    "fair point",
    "good point",
    "good call",
    "good one",
    "you got me",
    "right on time",
    "i'll take that",
    "ill take that",
    "that tracks",
    "same here",
    "sounds about right",
}

ENGLISH_ABSTRACT_HEADS = {
    "quality", "energy", "vibe", "point", "choice", "taste",
    "line", "spark", "move", "mood", "type", "thing",
}

ENGLISH_CONTEXT_STOPWORDS = {
    "the", "and", "but", "you", "your", "yours", "for", "with", "that", "this",
    "have", "has", "had", "are", "was", "were", "they", "them", "then", "just",
    "about", "really", "more", "than", "most", "from", "into", "onto", "over",
    "under", "like", "love", "hate", "what", "when", "where", "which", "who",
    "how", "why", "there", "their", "would", "could", "should", "been", "being",
    "some", "much", "very", "okay", "ok", "yeah", "yep", "cup", "time",
}

CHINESE_SHORT_REPLY_ALLOWLIST = {
    "这倒是",
    "那倒是",
    "有点意思",
    "说来听听",
    "你赢了",
    "这我信",
    "也行",
    "算你狠",
    "来得刚好",
    "我接住了",
    "这句我先收下",
    "先卖个关子",
}

CHINESE_ABSTRACT_TAILS = {
    "质量", "品质", "能量", "氛围", "状态", "气质", "水平", "感觉", "味道", "风格", "类型",
}

CHINESE_REPLY_VERB_CUES = {
    "是", "有点", "有空", "想", "要", "会", "能", "懂", "知道", "觉得", "算", "当", "看", "说",
    "聊", "发", "接", "认", "欠", "放", "忙", "猜", "信", "收", "上班", "工作",
}

CHINESE_CONNECTOR_CUES = (
    "那", "那就", "所以", "原来", "难怪", "看来", "这么说", "行", "收到", "还",
    "也", "先", "就", "那你", "那我", "所以你", "所以我",
)

CHINESE_PARTICLE_ENDINGS = ("啊", "呀", "呢", "吧", "嘛", "啦", "喽")


def _english_words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", str(text or "").lower())


def _contains_cjk_text(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _cjk_compact(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff]", str(text or "")))


def _cjk_bigrams(text: str) -> set[str]:
    compact = _cjk_compact(text)
    if len(compact) < 2:
        return set()
    return {compact[idx:idx + 2] for idx in range(len(compact) - 1)}


def _last_partner_text(messages: Optional[list[dict]]) -> str:
    for msg in reversed(messages or []):
        if not _is_contextual_partner_message(msg or {}):
            continue
        text = re.sub(r"\s+", " ", (msg.get("text", "") or "")).strip()
        if text:
            return text
    return ""


def _english_reply_quality_score(text: str, messages: Optional[list[dict]] = None) -> tuple[int, list[str]]:
    """
    对英文回复做轻量评分，而不是一刀切：
    - 承接上下文
    - 具备回复功能
    - 基本像一句自然聊天
    """
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    lowered = candidate.lower().strip(" .,!?:;")
    words = _english_words(lowered)
    reasons: list[str] = []

    if lowered in ENGLISH_SHORT_REPLY_ALLOWLIST:
        if messages:
            partner_last = ""
            for msg in reversed(messages):
                if msg.get("sender") in ("them", "other"):
                    partner_last = str(msg.get("text", "") or "").lower()
                    break
            opinion_signals = (
                "think", "feel", "guess", "believe", "point",
                "agree", "wrong", "right", "sure", "maybe",
                "actually", "suppose", "reckon", "probably",
                "seems", "sounds", "feels", "understand",
            )
            if partner_last and not any(signal in partner_last for signal in opinion_signals):
                return 3, ["allowlist_weak_context"]
        return 5, ["allowlist"]

    if not words:
        return -5, ["no-words"]

    score = 0
    has_question = "?" in candidate or bool(re.search(r"\b(why|who|what|where|when|which|how)\b", lowered))
    has_pronoun = bool(re.search(r"\b(i|you|we|they|he|she|it|that|this|there)\b", lowered))
    has_verb = any(word in ENGLISH_SENTENCE_VERBS for word in words)
    has_finite_shape = has_pronoun or has_verb or has_question

    if has_question:
        score += 2
        reasons.append("question")
    if has_pronoun:
        score += 2
        reasons.append("pronoun")
    if has_verb:
        score += 2
        reasons.append("verb")
    elif any(word.endswith("ing") or word.endswith("ed") for word in words):
        score += 1
        reasons.append("verb-shape")
    if re.search(r"\b(then|so|but|still|because|maybe|guess)\b", lowered):
        score += 1
        reasons.append("connector")
    if len(words) >= 4:
        score += 1
        reasons.append("fuller-length")

    last_text = _last_partner_text(messages)
    if _looks_english_text(last_text):
        last_words = {w for w in _english_words(last_text) if len(w) > 2 and w not in ENGLISH_CONTEXT_STOPWORDS}
        current_words = {w for w in words if len(w) > 2 and w not in ENGLISH_CONTEXT_STOPWORDS}
        overlap = last_words & current_words
        if overlap:
            score += 1
            reasons.append(f"context-overlap:{','.join(sorted(overlap))}")

    if len(words) == 1:
        score -= 3
        reasons.append("single-word")

    if len(words) <= 3 and not has_finite_shape:
        score -= 2
        reasons.append("no-sentence-frame")

    if words and words[-1] in ENGLISH_ABSTRACT_HEADS and not has_finite_shape:
        score -= 2
        reasons.append("abstract-head")

    return score, reasons


def _chinese_reply_quality_score(text: str, messages: Optional[list[dict]] = None) -> tuple[int, list[str]]:
    """
    对中文回复做本地上下文评分：
    - 先看有没有基本接话功能
    - 再看是否承接上一句
    - 最后才看句子是否像完整自然聊天
    """
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    compact = _cjk_compact(candidate)
    reasons: list[str] = []

    if candidate in CHINESE_SHORT_REPLY_ALLOWLIST:
        return 5, ["allowlist"]

    if not compact:
        return -5, ["no-cjk"]

    score = 0
    cjk_len = len(compact)
    has_question = bool(re.search(r"[？?]", candidate)) or bool(
        re.search(r"(怎么|什么|谁|哪|哪儿|为啥|为什么|是不是|要不要|行不行|真的假的|对不对)$", compact)
    )
    has_pronoun = bool(re.search(r"[我你他她这那咱]", candidate))
    has_connector = any(compact.startswith(prefix) or prefix in compact for prefix in CHINESE_CONNECTOR_CUES)
    has_reply_verb = any(token in compact for token in CHINESE_REPLY_VERB_CUES)
    has_particle = compact.endswith(CHINESE_PARTICLE_ENDINGS)

    if has_question:
        score += 2
        reasons.append("question")
    if has_pronoun:
        score += 1
        reasons.append("pronoun")
    if has_connector:
        score += 2
        reasons.append("connector")
    if has_reply_verb:
        score += 2
        reasons.append("reply-verb")
    if has_particle:
        score += 1
        reasons.append("particle")
    if 4 <= cjk_len <= 18:
        score += 1
        reasons.append("natural-length")
    elif cjk_len > 18:
        score += 1
        reasons.append("fuller-length")

    last_text = _last_partner_text(messages)
    if _contains_cjk_text(last_text):
        overlap = _cjk_bigrams(last_text) & _cjk_bigrams(candidate)
        if overlap:
            score += 1
            reasons.append(f"context-overlap:{','.join(sorted(overlap)[:3])}")

    has_sentence_frame = has_question or has_connector or has_reply_verb or has_particle
    if cjk_len <= 3 and not has_sentence_frame:
        score -= 3
        reasons.append("short-fragment")
    if cjk_len <= 4 and not (has_sentence_frame or has_pronoun):
        score -= 2
        reasons.append("no-sentence-frame")
    if any(compact.endswith(token) for token in CHINESE_ABSTRACT_TAILS) and not has_sentence_frame:
        score -= 2
        reasons.append("abstract-tail")

    return score, reasons


def _looks_like_weak_english_reply(text: str, messages: Optional[list[dict]] = None) -> bool:
    """
    用评分制拦截“格式合法但质量明显不够”的英文回复。
    重点处理 rare quality / good energy / interesting point 这类抽象短语，
    同时避免误伤 fair point / right on time 这类自然短句。
    """
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not _looks_english_text(candidate):
        return False

    score, reasons = _english_reply_quality_score(candidate, messages)
    if score < 2:
        log.warning(f"[URE] 英文回复评分过低 score={score} reasons={reasons}: {candidate[:80]}")
        return True
    return False


def _looks_like_weak_chinese_reply(text: str, messages: Optional[list[dict]] = None) -> bool:
    """
    用本地评分制拦截“字面干净但没接住上下文”的中文残句，
    减少对普通口语词的硬关键词误杀。
    """
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not _contains_cjk_text(candidate) or _looks_english_text(candidate):
        return False

    score, reasons = _chinese_reply_quality_score(candidate, messages)
    if score < 1:
        log.warning(f"[URE] 中文回复评分过低 score={score} reasons={reasons}: {candidate[:80]}")
        return True
    return False


def _looks_like_ai_refusal_reply(text: str) -> bool:
    candidate = re.sub(r"\s+", " ", str(text or "")).strip()
    if not candidate:
        return False
    refusal_re = re.compile(
        r"(?:\bas an ai(?:\b| language model)|\bai language model\b|\bi'?m an ai\b|"
        r"\bi'?m sorry,? but i (?:cannot|can'?t|am unable to) (?:help|assist|comply|provide|fulfill|complete)\b|"
        r"\bi (?:cannot|can'?t|am unable to) (?:help|assist|comply|provide|fulfill|complete)\b|"
        r"作为(?:一个)?(?:ai|人工智能|语言模型)|我是(?:一个)?(?:ai|人工智能|语言模型)|"
        r"(?:我|本模型|这个模型|系统)(?:无法|不能|不适合)(?:提供|满足|协助|完成|回答)|"
        r"无法(?:为你|帮你|协助你)(?:提供|完成|回答)|"
        r"抱歉[，,]?(?:我)?(?:无法|不能)(?:提供|协助|完成|回答)|违反.*(?:内容)?政策|content policy)",
        re.IGNORECASE,
    )
    return bool(refusal_re.search(candidate))


def _violates_recent_partner_language(text: str, messages: Optional[list[dict]] = None) -> bool:
    """Hard-stop obvious language drift after a recent CJK partner message."""
    candidate = re.sub(r"\s+", " ", (text or "")).strip()
    if not candidate or not messages:
        return False
    last_text = _last_partner_text(messages)
    if not _contains_cjk_text(last_text):
        return False
    if _contains_cjk_text(candidate):
        return False
    if _looks_english_text(candidate):
        log.warning(f"[URE] 回复语言未跟随最近中文入站: last={last_text[:40]} reply={candidate[:80]}")
        return True
    return False


def _safety_filter_reply(text: str, max_len: int, messages: Optional[list[dict]] = None) -> str:
    """最终出站安全过滤，可疑内容直接降级为安全短句。"""
    if not text:
        return SAFE_FALLBACK_REPLY

    clean = _strip_analysis_prefix(text)
    clean = re.sub(r'\s+', ' ', clean).strip().strip('"').strip("'")

    if _looks_like_analysis(clean):
        log.warning(f"[URE] ⚠️ 检测到思维链/分析文本，改用安全兜底: {clean[:80]}")
        return SAFE_FALLBACK_REPLY

    if _looks_like_ai_refusal_reply(clean):
        log.warning(f"[URE] ⚠️ 检测到 AI 拒答/身份泄露话术，拒绝发送: {clean[:80]}")
        return SAFE_FALLBACK_REPLY

    if _violates_recent_partner_language(clean, messages):
        log.warning(f"[URE] ⚠️ 检测到回复语言漂移，拒绝发送: {clean[:80]}")
        return SAFE_FALLBACK_REPLY

    if _looks_like_weak_english_reply(clean, messages):
        log.warning(f"[URE] ⚠️ 检测到英文残句/抽象短语，拒绝发送: {clean[:80]}")
        return SAFE_FALLBACK_REPLY

    if _looks_like_weak_chinese_reply(clean, messages):
        log.warning(f"[URE] ⚠️ 检测到中文残句/抽象短语，拒绝发送: {clean[:80]}")
        return SAFE_FALLBACK_REPLY

    if len(clean) > max_len:
        clean = clean[:max_len].strip()

    return clean or SAFE_FALLBACK_REPLY


def sanitize_reply_for_send(text: str, max_len: int = 50, messages: Optional[list[dict]] = None) -> str:
    """供各发送路径复用的统一最终安全过滤。"""
    return _safety_filter_reply(text, max_len, messages)


# ─────────────────────────────────────────────────────────────────
# 核心入口
# ─────────────────────────────────────────────────────────────────
def generate_reply(
    messages: list[dict],
    bio: str = "",
    age: int = 0,
    platform: str = "tinder",
    strategy: Optional[dict] = None,
    max_len: int = 50,
    intent: str = "reply",
) -> Optional[str]:
    """
    生成一条回复。

    参数
    ----
    messages : [{"sender": "me"|"them", "text": str}, ...]
    bio      : 对方资料
    age      : 对方年龄（岁）
    platform : tinder | bumble | ...
    strategy : 策略 dict（None 时自动加载）
    max_len  : 最大字符数

    返回
    ----
    str | None : 清洗后的回复文本，失败返回 None
    """
    if strategy is None:
        strategy = load_strategy()

    if not messages and not (bio or "").strip():
        return _configured_default_opener(strategy, platform, max_len)

    reaction_reply = build_reaction_ack_reply(messages, max_len=max_len)
    if reaction_reply:
        return reaction_reply

    if intent == "reactivation":
        contextual_fallback = build_reactivation_fallback_reply(
            messages,
            bio=bio,
            platform=platform,
            max_len=max_len,
        )
    else:
        contextual_fallback = build_contextual_fallback_reply(
            messages,
            bio=bio,
            age=age,
            platform=platform,
            max_len=max_len,
        )

    try:
        static_sp = build_static_system_prompt(platform, strategy)
        dynamic_up = build_dynamic_user_prompt(messages, bio, age, platform, strategy, intent=intent)
        raw = _call_llm(static_sp, dynamic_up)
        clean = _safety_filter_reply(raw, max_len, messages)

        if clean == SAFE_FALLBACK_REPLY and contextual_fallback:
            log.warning(f"[URE] 使用上下文兜底替代固定忙线文案: {contextual_fallback[:40]}")
            return contextual_fallback
        if clean == SAFE_FALLBACK_REPLY and messages:
            log.warning("[URE] 上下文不足以安全接话，本轮跳过发送")
            return None

        log.debug(f"[URE] platform={platform} raw={raw[:60]} clean={clean[:40]}")
        return clean if clean else None

    except Exception as e:
        log.error(f"[URE] generate_reply 异常: {e}")
        if messages:
            return None
        return contextual_fallback


# ─────────────────────────────────────────────────────────────────
# 平台适配层
# ─────────────────────────────────────────────────────────────────
def click_contact(page, entry: dict, platform: str = "tinder") -> bool:
    """
    点击进入对话。

    Tinder    : 用 entry["locator"] 直接 .click()
    Bumble    : 用 data-qa-uid 精准点击，左偏 120px，关闭通知面板

    参数
    ----
    page   : Playwright Page 对象
    entry  : {"uid": str, "x": float, "y": float, "name": str, ...}
    platform : tinder | bumble | ...

    返回
    ----
    bool : True 进入成功
    """
    adapter = ADAPTERS.get(platform)
    if not adapter:
        raise ValueError(f"未知平台: {platform}")

    if platform == "tinder":
        # Tinder：直接用 locator 点击（无偏移）
        locator_str = entry.get("locator")
        if not locator_str:
            raise ValueError("Tinder entry 缺少 locator")
        page.locator(locator_str).first.click(force=True, timeout=5000)
        return True

    elif platform == "bumble":
        uid = entry.get("uid", "")
        name = str(entry.get("name", "") or "").strip()
        cx  = entry.get("x", 0) + adapter.click_offset_x
        cy  = entry.get("y", 0) + adapter.click_offset_y

        def _click_uid(timeout: int = 5000) -> bool:
            if not (adapter.requires_uid and uid):
                return False
            locator = page.locator(f'[data-qa-uid="{uid}"]').first
            locator.wait_for(state="attached", timeout=timeout)
            locator.scroll_into_view_if_needed(timeout=timeout)
            locator.click(force=True, timeout=timeout)
            return True

        def _click_name(timeout: int = 5000) -> bool:
            if not name:
                return False
            locator = page.locator(".contact").filter(has_text=re.compile(re.escape(name))).first
            locator.wait_for(state="attached", timeout=timeout)
            locator.scroll_into_view_if_needed(timeout=timeout)
            locator.click(force=True, timeout=timeout)
            return True

        try:
            if not _click_uid():
                page.mouse.click(cx, cy)
        except Exception as first_exc:
            log.warning(f"[URE] Bumble 联系人点击失效，刷新列表后重抓: {name or uid} | {first_exc}")
            try:
                target_url = page.url if "/app/connections" in (page.url or "") else "https://eu1.bumble.com/app/connections"
                page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
                if not _click_uid(timeout=7000) and not _click_name(timeout=5000):
                    page.mouse.click(cx, cy)
            except Exception as retry_exc:
                log.warning(f"[URE] Bumble 联系人重试点击失败: {name or uid} | {retry_exc}")
                return False

        # 关闭通知面板（如果有）
        try:
            page.wait_for_selector(
                adapter.notification_panel_sel, timeout=4000
            )
            page.keyboard.press("Escape")
            time.sleep(1)
        except Exception:
            pass

        return True

    raise ValueError(f"未实现的平台: {platform}")


def wait_for_chat_ready(
    page,
    platform: str = "tinder",
    timeout: float = 15.0,
) -> bool:
    """
    等待对话界面完全加载。

    Tinder    : 等待 .matchedCard 或 textarea 出现
    Bumble    : 等待 .page--chat 下的气泡元素或 textarea
    """
    from playwright.sync_api import TimeoutError

    start = time.time()
    if platform == "tinder":
        while time.time() - start < timeout:
            if page.locator("textarea").count() > 0:
                return True
            time.sleep(0.5)
        return False

    elif platform == "bumble":
        # 等待气泡出现
        while time.time() - start < timeout:
            try:
                bubble_count = page.evaluate(
                    """() => {
                        const pc = document.querySelector('.page--chat');
                        if (!pc) return 0;
                        const layout = pc.children[0];
                        if (!layout) return 0;
                        for (const child of layout.children) {
                            const cls = child.className || '';
                            if (cls.includes('sidebar') || cls.includes('contact-tabs') || cls.includes('request-panel')) continue;
                            if (child.querySelector('[class*="bubble"]')) return 1;
                        }
                        return 0;
                    }"""
                )
                if bubble_count > 0:
                    time.sleep(1)
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    return True


def back_to_list(page, platform: str = "tinder") -> None:
    """
    返回消息列表。
    """
    try:
        if platform == "bumble":
            page.keyboard.press("Escape")
            time.sleep(2)
            if "/connections" in page.url:
                page.goto(
                    "https://bumble.com/app/connections",
                    timeout=20000,
                    wait_until="domcontentloaded",
                )
            page.wait_for_selector(".contact", timeout=15000)
            time.sleep(3)
        elif platform == "tinder":
            page.goto("https://tinder.com", timeout=20000)
    except Exception as e:
        log.warning(f"[URE] back_to_list {platform}: {e}")
        time.sleep(5)


# ─────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────
def is_curfew() -> bool:
    """03:00 - 08:00 宵禁检查"""
    h = time.localtime().tm_hour
    return 3 <= h < 8


def next_wake_time() -> float:
    """宵禁期间距本次 08:00 的秒数；非宵禁时返回 0。"""
    import datetime
    now = datetime.datetime.now()
    if not is_curfew():
        return 0.0
    wake = now.replace(hour=8, minute=0, second=0, microsecond=0)
    return max(0.0, (wake - now).total_seconds())
