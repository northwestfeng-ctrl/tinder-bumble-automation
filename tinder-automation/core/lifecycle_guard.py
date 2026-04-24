#!/usr/bin/env python3
"""
Tinder 自动化 - 模块5: 生命周期与熔断保护
功能：
- 每日操作上限熔断
- 物理作息模拟
- 异常行为检测
"""
import json
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ============ 阈值配置 ============

# 每日限制
MAX_DAILY_ACTIONS = 40  # 每日最大操作数
MAX_SWIPES_PER_DAY = 100  # 最大滑动次数
MAX_MESSAGES_PER_DAY = 50  # 最大消息数

# 时间窗口（秒）
ACTION_COOLDOWN = 30  # 操作间最小间隔
BURST_THRESHOLD = 10  # 触发长时间暂停的操作数
BURST_PAUSE_MIN = 5 * 60  # 5分钟
BURST_PAUSE_MAX = 20 * 60  # 20分钟

# 作息时间（小时）
ACTIVE_HOURS = (0, 24)  # 活跃时间 0:00 - 23:59（全天24小时）
SLEEP_HOURS = (22, 8)  # 睡眠时间

# 状态文件
STATE_FILE = Path("~/.tinder-automation/state.json").expanduser()


class LifecycleGuard:
    """
    生命周期守卫
    管理操作计数、时间限制、熔断机制
    """
    
    def __init__(self, account_id: str, state_file: str = None):
        self.account_id = account_id
        self.state_file = Path(state_file or STATE_FILE)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 加载或初始化状态
        self.state = self._load_state()
        
        # 检查是否需要重置（新月）
        self._check_daily_reset()
    
    def _load_state(self) -> dict:
        """加载状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    return state
            except Exception:
                pass
        
        return self._init_state()
    
    def _init_state(self) -> dict:
        """初始化新状态"""
        return {
            "account_id": self.account_id,
            "created_at": datetime.now().isoformat(),
            "daily": {
                "date": datetime.now().date().isoformat(),
                "action_count": 0,
                "swipe_count": 0,
                "message_count": 0,
                "last_action_at": None,
            },
            "lifecycle": {
                "total_actions": 0,
                "total_sessions": 0,
                "session_start": None,
                "last_session_end": None,
                "consecutive_bursts": 0,
            },
            "blocked_until": None,
            "errors": [],
        }
    
    def _save_state(self):
        """保存状态"""
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
    
    def _check_daily_reset(self):
        """检查是否需要重置每日计数"""
        today = datetime.now().date().isoformat()
        if self.state["daily"]["date"] != today:
            print(f"[Lifecycle] 新的一天，重置计数")
            self.state["daily"] = {
                "date": today,
                "action_count": 0,
                "swipe_count": 0,
                "message_count": 0,
                "last_action_at": None,
            }
            self._save_state()
    
    def _is_active_hours(self) -> bool:
        """检查是否在活跃时间"""
        now = datetime.now().hour
        start, end = ACTIVE_HOURS
        
        if start <= end:
            return start <= now < end
        else:
            return now >= start or now < end
    
    def _should_sleep(self) -> bool:
        """检查是否应该进入睡眠模式"""
        # 检查是否被强制暂停
        if self.state.get("blocked_until"):
            blocked_time = datetime.fromisoformat(self.state["blocked_until"])
            if datetime.now() < blocked_time:
                remaining = (blocked_time - datetime.now()).seconds
                print(f"[Lifecycle] 暂停中，剩余 {remaining} 秒")
                return True
            else:
                # 暂停结束
                self.state["blocked_until"] = None
                print("[Lifecycle] 暂停结束")
        
        # 检查作息时间
        if not self._is_active_hours():
            print("[Lifecycle] 非活跃时间，进入休息")
            return True
        
        return False
    
    def record_action(self, action_type: str = "general"):
        """记录一个动作"""
        self._check_daily_reset()
        
        daily = self.state["daily"]
        lifecycle = self.state["lifecycle"]
        
        # 更新计数
        daily["action_count"] += 1
        lifecycle["total_actions"] += 1
        daily["last_action_at"] = datetime.now().isoformat()
        
        # 按类型计数
        if action_type == "swipe":
            daily["swipe_count"] += 1
        elif action_type == "message":
            daily["message_count"] += 1
        
        # 检查是否触发熔断
        self._check_thresholds()
        
        self._save_state()
    
    def _check_thresholds(self):
        """检查阈值，触发熔断"""
        daily = self.state["daily"]
        lifecycle = self.state["lifecycle"]
        
        # 检查每日上限
        if daily["action_count"] >= MAX_DAILY_ACTIONS:
            self._trigger_sleep(f"达到每日操作上限 ({MAX_DAILY_ACTIONS})")
            return
        
        if daily["swipe_count"] >= MAX_SWIPES_PER_DAY:
            self._trigger_sleep(f"达到每日滑动上限 ({MAX_SWIPES_PER_DAY})")
            return
        
        if daily["message_count"] >= MAX_MESSAGES_PER_DAY:
            self._trigger_sleep(f"达到每日消息上限 ({MAX_MESSAGES_PER_DAY})")
            return
        
        # 检查连续操作爆发
        if daily["action_count"] >= BURST_THRESHOLD:
            # 随机触发长暂停
            if random.random() < 0.3:  # 30% 概率
                pause_min = max(BURST_PAUSE_MIN, daily["action_count"] * 60)
                pause_duration = random.randint(pause_min, BURST_PAUSE_MAX)
                lifecycle["consecutive_bursts"] += 1
                
                self._trigger_burst_pause(pause_duration)
    
    def _trigger_sleep(self, reason: str):
        """触发睡眠"""
        print(f"[Lifecycle] 熔断触发: {reason}")
        
        # 计算到明天活跃时间的间隔
        now = datetime.now()
        active_start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        
        if now.hour >= 22:
            # 今天已经结束，等到明天8点
            pass
        elif now.hour < 8:
            # 还没到活跃时间
            active_start = now.replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            # 强制结束今天，等明天
            active_start = active_start + timedelta(days=1)
        
        self.state["blocked_until"] = active_start.isoformat()
        self._save_state()
    
    def _trigger_burst_pause(self, duration: int):
        """触发爆发暂停"""
        blocked_until = datetime.now() + timedelta(seconds=duration)
        self.state["blocked_until"] = blocked_until.isoformat()
        print(f"[Lifecycle] 爆发暂停 {duration} 秒")
        self._save_state()
    
    def can_proceed(self) -> tuple:
        """
        检查是否可以继续执行
        返回: (can_proceed: bool, reason: str)
        """
        # 检查暂停
        if self._should_sleep():
            return False, "系统暂停中"
        
        # 检查活跃时间
        if not self._is_active_hours():
            return False, "非活跃时间"
        
        return True, "OK"
    
    def wait_if_needed(self):
        """如果需要则等待"""
        can_proceed, reason = self.can_proceed()
        
        if not can_proceed:
            if "非活跃时间" in reason:
                # 等到活跃时间
                now = datetime.now()
                active_start = now.replace(hour=8, minute=0, second=0, microsecond=0)
                if now.hour >= 22:
                    active_start += timedelta(days=1)
                
                wait_seconds = (active_start - now).total_seconds()
                print(f"[Lifecycle] 等待活跃时间: {int(wait_seconds)} 秒")
                time.sleep(min(wait_seconds, 3600))  # 最多等1小时
            
            elif "暂停中" in reason:
                time.sleep(60)  # 等1分钟再检查
        
        return self.can_proceed()[0]
    
    def get_status(self) -> dict:
        """获取当前状态"""
        self._check_daily_reset()
        
        daily = self.state["daily"]
        lifecycle = self.state["lifecycle"]
        
        return {
            "account_id": self.account_id,
            "date": daily["date"],
            "daily_actions": f"{daily['action_count']}/{MAX_DAILY_ACTIONS}",
            "daily_swipes": f"{daily['swipe_count']}/{MAX_SWIPES_PER_DAY}",
            "daily_messages": f"{daily['message_count']}/{MAX_MESSAGES_PER_DAY}",
            "total_actions": lifecycle["total_actions"],
            "total_sessions": lifecycle["total_sessions"],
            "is_active": self._is_active_hours(),
            "is_blocked": self.state.get("blocked_until") is not None,
        }
    
    def start_session(self):
        """开始新会话"""
        self.state["lifecycle"]["session_start"] = datetime.now().isoformat()
        self.state["lifecycle"]["total_sessions"] += 1
        self._save_state()
    
    def end_session(self):
        """结束会话"""
        self.state["lifecycle"]["last_session_end"] = datetime.now().isoformat()
        self.state["lifecycle"]["session_start"] = None
        self._save_state()


class ActionCooldown:
    """操作冷却管理器"""
    
    def __init__(self, min_interval: int = None):
        self.min_interval = min_interval or ACTION_COOLDOWN
        self.last_action_time = 0
    
    def wait_if_needed(self):
        """如果没过冷却期则等待"""
        elapsed = time.time() - self.last_action_time
        
        if elapsed < self.min_interval:
            wait_time = self.min_interval - elapsed
            # 添加随机抖动
            wait_time *= random.uniform(0.8, 1.5)
            print(f"[Cooldown] 等待冷却: {int(wait_time)} 秒")
            time.sleep(wait_time)
        
        self.last_action_time = time.time()


def create_guard(account_id: str) -> LifecycleGuard:
    """创建生命周期守卫"""
    return LifecycleGuard(account_id)


if __name__ == "__main__":
    print("=== Lifecycle Guard 测试 ===")
    guard = create_guard("test_account")
    print("状态:", guard.get_status())
    print("可执行:", guard.can_proceed())
