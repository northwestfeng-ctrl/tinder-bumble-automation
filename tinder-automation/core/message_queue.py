#!/usr/bin/env python3
"""
Tinder Bot 消息队列 - 双阶段扫描机制
阶段1: 优先处理未读红点新消息
阶段2: 补齐已读未回对话
"""
import sys
import time
import random
from pathlib import Path
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from core.tinder_bot import TinderBot, CONFIG

MAX_SCAN_DEPTH = 50   # 扫描前50个对话卡片
TARGET_REPLY_COUNT = 10  # 达到10个回复后停止

@dataclass
class QueuedMessage:
    card_index: int
    sender_name: str
    preview: str
    bot: TinderBot
    conversation_element: Any = None


def handle_single_chat(bot: TinderBot, card, processed_ids: set) -> tuple:
    """
    处理单个对话
    返回: (success: bool, should_stop: bool, reason: str)
    """
    try:
        href = card.get_attribute('href')
        if href in processed_ids:
            return False, False, "已处理过"
        
        # 获取名字
        sender = "未知用户"
        try:
            name_elem = card.locator('[class*="name"], [class*="matchName"], strong').first
            if name_elem.is_visible():
                sender = name_elem.inner_text()
        except:
            pass
        
        # 获取预览
        preview = ""
        try:
            preview_elem = card.locator('[class*="preview"], [class*="lastMessage"]').first
            if preview_elem.is_visible():
                preview = preview_elem.inner_text()
        except:
            pass
        
        print(f"\n    联系人: {sender}")
        print(f"    预览: {preview[:30]}...")
        
        # 点击进入对话
        card.click(force=True)
        bot.page.wait_for_timeout(2000)
        
        # 读取消息
        messages = bot.read_conversation(card)
        
        if not messages:
            return False, False, "无消息内容"
        
        print(f"    获取到 {len(messages)} 条消息")
        
        # 智能判断
        should_reply, reason = bot.should_reply(messages)
        
        if not should_reply:
            print(f"    跳过: {reason}")
            processed_ids.add(href)
            return False, False, reason
        
        # 生成回复
        reply = bot.generate_reply(messages)
        
        if not reply or not reply.strip():
            print(f"    LLM 未生成回复")
            processed_ids.add(href)
            return False, False, "LLM无回复"
        
        print(f"    回复: {reply}")
        
        # 发送
        success = bot.send_reply(reply, messages=messages)
        
        if success:
            print(f"    ✅ 发送成功")
            processed_ids.add(href)
            return True, False, "成功"
        else:
            print(f"    ❌ 发送失败")
            processed_ids.add(href)
            return False, False, "发送失败"
        
    except Exception as e:
        print(f"    ❌ 处理异常: {e}")
        return False, False, str(e)


def process_queue(bot: TinderBot, max_process: int = MAX_SCAN_DEPTH, target_replies: int = TARGET_REPLY_COUNT):
    """
    兼容入口。

    旧版队列脚本依赖 card 列表与 read_conversation 等历史接口，
    但项目主流程已经收敛到 TinderBot.process_unread_messages()。
    这里统一复用主流程，避免脚本间逻辑再次漂移。
    """
    del max_process, target_replies

    print("=" * 50)
    print("Tinder Bot 队列处理")
    print("目标: 复用统一巡检链路处理待回复对话")
    print("=" * 50)

    replied_total = bot.process_unread_messages()

    print("\n" + "=" * 50)
    print("📊 队列处理完成报告")
    print("=" * 50)
    print(f"本轮成功回复: {replied_total} 人")
    print("=" * 50)

    return {
        "new_replied": replied_total,
        "target_replies": replied_total,
    }


def run_queue_cli():
    """命令行入口"""
    print("=" * 50)
    print("Tinder Bot 双阶段队列处理")
    print(f"目标: 回复 {TARGET_REPLY_COUNT} 人")
    print("=" * 50)
    
    bot = TinderBot(CONFIG)
    
    try:
        print("\n[1] 初始化浏览器...")
        bot.setup()
        
        print("\n[2] 打开 Tinder...")
        bot.page.goto("https://tinder.com", timeout=30000)
        bot.page.wait_for_timeout(3000)
        
        if "login" in bot.page.url.lower():
            print("\n❌ 未登录，请先执行登录持久化")
            return
        
        print("\n✅ 已登录")
        
        result = process_queue(bot)
        
    except Exception as e:
        print(f"\n❌ 队列执行失败: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        print("\n浏览器保持打开...")
        try:
            input("按 Enter 关闭...")
        except EOFError:
            pass
        bot.cleanup()


if __name__ == "__main__":
    run_queue_cli()
