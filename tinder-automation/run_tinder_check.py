#!/usr/bin/env python3
"""独立的 Tinder 巡检脚本，用于隔离 asyncio/event loop 环境。"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))


def main() -> int:
    from core.tinder_bot import TinderBot, CONFIG, TinderBackendError
    try:
        bot = TinderBot(CONFIG)
        try:
            bot.setup()
            reply_count = bot.process_unread_messages()
        finally:
            bot.cleanup()
        print(f"[Bot] process_unread_messages returned: {reply_count}", file=sys.stderr)
        print(f"REPLY_COUNT:{int(reply_count or 0)}")
        return 0
    except TinderBackendError as e:
        print(f"BACKEND_ERROR:{e}", file=sys.stderr)
        print("REPLY_COUNT:0")
        return 0  # 后端问题不算错误，不触发告警
    except Exception as e:
        print(f"ERROR:{e}", file=sys.stderr)
        print("REPLY_COUNT:0")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
