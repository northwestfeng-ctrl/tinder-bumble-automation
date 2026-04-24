#!/usr/bin/env python3
"""
Tinder Bot 批量队列处理脚本
扫描所有对话 -> 过滤已回复/带微信的 -> 自动回复 -> 随机休眠
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.message_queue import run_queue_cli

if __name__ == "__main__":
    run_queue_cli()
