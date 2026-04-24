#!/usr/bin/env python3
"""
etl_corpus.py
清洗历史语料 → 输出干净 Markdown
去除：代码标签、隐私信息、干扰字符
按转化结果打标：success / fail / unknown
"""
import json
import re
from pathlib import Path

INPUT  = Path(__file__).parent / "corpus_history.json"
OUTPUT = Path(__file__).parent / "corpus_markdown.md"

# 隐私敏感词（清洗）
PRIVACY_PATTERNS = [
    re.compile(r'\b\d{6,}\b'),          # 6位以上数字（疑似手机号）
    re.compile(r'1[3-9]\d{9}'),          # 手机号
    re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),  # 邮箱
]

# 干扰字符
NOISE_PATTERNS = [
    re.compile(r'^\[.*?\]\s*'),          # [评分] 前缀
    re.compile(r'\s{2,}'),               # 连续空格
    re.compile(r'(%[0-9A-Fa-f]{2}){5,}'), # URL 编码残渣（连续5个以上 %XX）
    re.compile(r'[A-Za-z0-9+/=]{50,}'),  # base64 长串残留
]

# 成功标记（主动留微信号 / 说"微信"关键词）
SUCCESS_SIGNALS = ['wechat', '微信', 'vx', 'v:', '微信号']


def anonymize(text: str) -> str:
    """脱敏处理"""
    for p in PRIVACY_PATTERNS:
        text = p.sub('[数字]', text)
    return text


def is_success(messages: list) -> str:
    """判断对话是否成功加微"""
    texts = ' '.join(m['text'].lower() for m in messages)
    return 'success' if any(s in texts for s in SUCCESS_SIGNALS) else 'unknown'


def clean_message(text: str) -> str:
    """清洗单条消息"""
    text = text.strip()
    for p in NOISE_PATTERNS:
        text = p.sub(' ', text)
    text = anonymize(text)
    return text


def build_markdown_entry(conversation: dict) -> str:
    name  = conversation.get('match_name') or conversation.get('match_name', 'unknown_match')
    index = conversation.get('match_index', '?')
    msgs  = conversation.get('messages', [])
    label = is_success(msgs)

    lines = []
    lines.append(f'## {name}  | 对话 {index} | {label}\n')

    for m in msgs:
        who  = '我' if m.get('sender') == 'me' else '对方'
        text = clean_message(m.get('text', ''))
        lines.append(f'* **{who}**: {text}')

    lines.append('')
    return '\n'.join(lines)


def run():
    raw = json.load(open(INPUT, encoding='utf-8'))

    # ── 兼容层：统一转换为 [{match_name,match_index,messages},...] ──
    data = []
    for item in raw:
        # 新格式（字典，有 messages）
        if isinstance(item, dict) and 'messages' in item:
            item.setdefault('match_name', item.get('match_name', 'unknown_match'))
            item.setdefault('match_index', item.get('match_index', 0))
            data.append(item)
        # 旧格式（纯列表 [[q,a], [q,a], ...]）→ 跳过（无 match_name/index）
        elif isinstance(item, list):
            continue  # 无唯一标识符，不符合 etl 契约，直接丢弃
        else:
            continue  # 未知结构，跳过

    # 统计
    success = sum(1 for c in data if is_success(c.get('messages', [])) == 'success')
    unknown = len(data) - success

    header = (
        '# Tinder 清洗后语料库\n\n'
        f'## 统计\n- 对话总数：{len(data)}\n'
        f'- 成功加微：{success}\n'
        f'- 未标注：{unknown}\n\n'
        '---\n\n'
    )

    body = header + '\n'.join(build_markdown_entry(c) for c in data)

    OUTPUT.write_text(body, encoding='utf-8')
    print(f'[etl_corpus] 清洗完成 → {OUTPUT}')
    print(f'  成功: {success} / {len(data)}')


if __name__ == '__main__':
    run()
