#!/usr/bin/env python3
"""
Tinder 自动化 - 模块3: 人类行为非线性化
功能：
- 贝塞尔曲线鼠标轨迹
- 非线性随机等待时间
- 人类不可预测行为模拟
"""
import math
import random
import time
from typing import List, Tuple

# ============ 轨迹参数 ============

# 鼠标移动
MOUSE_SPEED_MIN = 0.5  # 像素/毫秒 最低速度
MOUSE_SPEED_MAX = 2.5  # 像素/毫秒 最高速度
TRAJECTORY_POINTS = 20  # 轨迹点数

# 等待时间（毫秒）
THINK_MIN = 3000   # 思考
THINK_MAX = 15000  # 最大思考时间
ACTION_MIN = 500   # 动作间
ACTION_MAX = 3000  # 最大动作间隔
BURST_PAUSE = (30000, 120000)  # 连续操作后长时间暂停

# 滑动
SWIPE_DURATION_MIN = 300  # 滑动持续时间 ms
SWIPE_DURATION_MAX = 800
SWIPE_STEPS = 30  # 滑动步数


def bezier_curve(p0: Tuple[float, float], p1: Tuple[float, float], 
                 p2: Tuple[float, float], p3: Tuple[float, float], 
                 t: float) -> Tuple[float, float]:
    """三次贝塞尔曲线计算"""
    x = (1-t)**3 * p0[0] + 3*(1-t)**2*t * p1[0] + 3*(1-t)*t**2 * p2[0] + t**3 * p3[0]
    y = (1-t)**3 * p0[1] + 3*(1-t)**2*t * p1[1] + 3*(1-t)*t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def generate_human_trajectory(start: Tuple[float, float], 
                               end: Tuple[float, float],
                               curvature: float = None) -> List[Tuple[float, float]]:
    """
    生成人类风格的鼠标轨迹
    使用三次贝塞尔曲线 + 随机控制点
    
    Args:
        start: 起始坐标 (x, y)
        end: 终止坐标 (x, y)
        curvature: 曲率 (0=直线, 0.5=自然曲线, 1=极度弯曲)
    
    Returns:
        轨迹点列表 [(x1,y1), (x2,y2), ...]
    """
    if curvature is None:
        curvature = random.uniform(0.2, 0.6)
    
    # 计算方向和距离
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    distance = math.sqrt(dx**2 + dy**2)
    
    # 生成随机控制点
    # P1: 起始点偏移
    p1 = (
        start[0] + dx * curvature * random.uniform(0.3, 0.7),
        start[1] + dy * curvature * random.uniform(-0.5, 0.5) + random.uniform(-50, 50)
    )
    
    # P2: 终止点偏移
    p2 = (
        end[0] - dx * curvature * random.uniform(0.3, 0.7),
        end[1] - dy * curvature * random.uniform(-0.5, 0.5) + random.uniform(-50, 50)
    )
    
    # 生成轨迹点
    points = []
    num_points = max(TRAJECTORY_POINTS, int(distance / 20))
    
    for i in range(num_points + 1):
        t = i / num_points
        point = bezier_curve(start, p1, p2, end, t)
        points.append(point)
    
    # 添加微小抖动
    jittered = []
    for i, (x, y) in enumerate(points):
        if i > 0 and i < len(points) - 1:  # 不抖动首尾
            jitter_x = random.uniform(-1, 1)
            jitter_y = random.uniform(-1, 1)
            x += jitter_x
            y += jitter_y
        jittered.append((x, y))
    
    return jittered


class HumanTrajectory:
    """人类轨迹执行器"""
    
    def __init__(self, page):
        self.page = page
        self.last_position = (0, 0)
    
    def move_to(self, target_x: float, target_y: float, 
                 duration_ms: int = None) -> None:
        """
        以人类轨迹移动到目标位置
        
        Args:
            target_x: 目标 X 坐标
            target_y: 目标 Y 坐标
            duration_ms: 移动持续时间（自动计算）
        """
        # 从当前位置开始
        if self.last_position == (0, 0):
            current = self.page.evaluate("() => ({ x: mouseX || 0, y: mouseY || 0 })")
            self.last_position = (current.get("x", 400), current.get("y", 300))
        
        start = self.last_position
        
        # 计算距离和速度
        dx = target_x - start[0]
        dy = target_y - start[1]
        distance = math.sqrt(dx**2 + dy**2)
        
        if distance < 5:
            return  # 太近，跳过
        
        # 自动计算持续时间（速度 = 1-2 px/ms）
        if duration_ms is None:
            speed = random.uniform(MOUSE_SPEED_MIN, MOUSE_SPEED_MAX)
            duration_ms = int(distance / speed)
            duration_ms = max(100, min(duration_ms, 2000))  # 限制范围
        
        # 生成轨迹
        trajectory = generate_human_trajectory(start, (target_x, target_y))
        
        # 执行轨迹
        step_delay = duration_ms / len(trajectory)
        
        for x, y in trajectory:
            self.page.mouse.move(x, y)
            time.sleep(step_delay / 1000)
        
        self.last_position = (target_x, target_y)
    
    def reset_position(self):
        """重置位置记录"""
        self.last_position = (0, 0)


class HumanDelay:
    """人类化随机延迟"""
    
    @staticmethod
    def think() -> float:
        """思考延迟（处理信息时的停顿）"""
        delay = random.uniform(THINK_MIN, THINK_MAX) / 1000
        time.sleep(delay)
        return delay
    
    @staticmethod
    def action() -> float:
        """动作间延迟"""
        delay = random.uniform(ACTION_MIN, ACTION_MAX) / 1000
        time.sleep(delay)
        return delay
    
    @staticmethod
    def burst() -> float:
        """连续操作后长暂停"""
        delay = random.uniform(*BURST_PAUSE) / 1000
        time.sleep(delay)
        return delay
    
    @staticmethod
    def typing() -> float:
        """打字间隔"""
        delay = random.uniform(100, 350) / 1000
        time.sleep(delay)
        return delay
    
    @staticmethod
    def click() -> float:
        """点击后延迟"""
        delay = random.uniform(50, 150) / 1000
        time.sleep(delay)
        return delay
    
    @staticmethod
    def random_exponential() -> float:
        """指数分布随机延迟（模拟泊松过程）"""
        import random
        mean = (THINK_MIN + THINK_MAX) / 2 / 1000
        delay = -mean * math.log(random.random())
        return delay


class ActionRhythm:
    """操作节奏编排"""
    
    def __init__(self, page):
        self.page = page
        self.action_count = 0
        self.session_start = time.time()
    
    def execute_action(self, action_name: str, func, *args, **kwargs):
        """
        执行带节奏感的动作
        自动插入随机延迟
        """
        # 动作前思考
        HumanDelay.think()
        
        # 执行动作
        result = func(*args, **kwargs)
        
        # 动作计数
        self.action_count += 1
        
        # 动作后延迟
        HumanDelay.action()
        
        # 检测是否需要暂停
        if self.action_count % 15 == 0:
            print(f"[节奏] 已执行 {self.action_count} 个动作，强制暂停")
            HumanDelay.burst()
        
        return result
    
    def get_stats(self) -> dict:
        """获取节奏统计"""
        elapsed = time.time() - self.session_start
        return {
            "action_count": self.action_count,
            "elapsed_seconds": int(elapsed),
            "avg_actions_per_minute": self.action_count / (elapsed / 60) if elapsed > 0 else 0
        }


# ============ 滑动模拟 ============

class SwipeSimulator:
    """滑动操作模拟器"""
    
    def __init__(self, page):
        self.page = page
    
    def swipe_left(self, start_x: int = None, start_y: int = None,
                   distance: int = 300, duration: int = None):
        """向左滑动（不喜欢）"""
        if start_x is None:
            start_x = random.randint(500, 600)
        if start_y is None:
            start_y = random.randint(400, 500)
        
        end_x = start_x - distance
        self._swipe(start_x, start_y, end_x, start_y, duration)
    
    def swipe_right(self, start_x: int = None, start_y: int = None,
                    distance: int = 300, duration: int = None):
        """向右滑动（喜欢）"""
        if start_x is None:
            start_x = random.randint(500, 600)
        if start_y is None:
            start_y = random.randint(400, 500)
        
        end_x = start_x + distance
        self._swipe(start_x, start_y, end_x, start_y, duration)
    
    def swipe_up(self, start_x: int = None, start_y: int = None,
                 distance: int = 400, duration: int = None):
        """向上滑动（超级喜欢）"""
        if start_x is None:
            start_x = random.randint(500, 600)
        if start_y is None:
            start_y = random.randint(500, 600)
        
        end_y = start_y - distance
        self._swipe(start_x, start_y, start_x, end_y, duration)
    
    def _swipe(self, start_x: int, start_y: int, end_x: int, end_y: int,
               duration: int = None):
        """执行滑动"""
        if duration is None:
            duration = random.randint(SWIPE_DURATION_MIN, SWIPE_DURATION_MAX)
        
        # 生成平滑轨迹
        trajectory = generate_human_trajectory(
            (start_x, start_y), 
            (end_x, end_y),
            curvature=random.uniform(0.1, 0.3)
        )
        
        step_delay = duration / len(trajectory)
        
        # 按下
        self.page.mouse.move(start_x, start_y)
        self.page.mouse.down()
        time.sleep(random.uniform(0.05, 0.1))
        
        # 移动
        for x, y in trajectory:
            self.page.mouse.move(x, y)
            time.sleep(step_delay / 1000)
        
        # 释放
        time.sleep(random.uniform(0.05, 0.1))
        self.page.mouse.up()


if __name__ == "__main__":
    print("=== Human Behavior 模块测试 ===")
    print("轨迹点:", TRAJECTORY_POINTS)
    print("速度范围:", MOUSE_SPEED_MIN, "-", MOUSE_SPEED_MAX, "px/ms")
    print("思考时间:", THINK_MIN/1000, "-", THINK_MAX/1000, "秒")
