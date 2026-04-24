#!/usr/bin/env python3
"""
Tinder 自动化 - 模块2: CDP原生事件映射
功能：
- 物理级坐标点击（非element.click()）
- 随机打字延迟模拟人类输入
- 快捷键模拟
"""
import random
import time
from typing import Tuple

# ============ 人类行为参数 ============

# 鼠标点击
CLICK_DELAY_RANGE = (50, 150)  # 毫秒
CLICK_POSITION_JITTER = 3  # 像素位置抖动

# 打字参数
TYPE_DELAY_RANGE = (100, 350)  # 每个字符间隔 ms
TYPE_START_DELAY = (200, 500)  # 开始输入前延迟
TYPE_END_DELAY = (100, 300)   # 输入完成后延迟

# 滚动
SCROLL_STEP_RANGE = (100, 300)  # 每步像素
SCROLL_PAUSE_RANGE = (500, 1500)  # 每步之间停顿

# 悬停
HOVER_DURATION_RANGE = (500, 2000)  # 悬停持续时间


class HumanClicker:
    """模拟人类点击行为"""
    
    def __init__(self, page):
        self.page = page
    
    def click_element(self, selector: str, timeout: int = 5000):
        """点击元素（带随机偏移）"""
        element = self.page.wait_for_selector(selector, timeout=timeout)
        box = element.bounding_box()
        
        if not box:
            raise ValueError(f"无法获取元素位置: {selector}")
        
        # 计算中心点 + 随机偏移
        x = box["x"] + box["width"] / 2 + random.randint(-CLICK_POSITION_JITTER, CLICK_POSITION_JITTER)
        y = box["y"] + box["height"] / 2 + random.randint(-CLICK_POSITION_JITTER, CLICK_POSITION_JITTER)
        
        # 物理点击
        self._physical_click(x, y)
        
        return element
    
    def click_coordinates(self, x: int, y: int):
        """点击指定坐标"""
        self._physical_click(x, y)
    
    def _physical_click(self, x: float, y: float):
        """使用 CDP 物理点击"""
        # 移动到目标位置
        self.page.mouse.move(x, y)
        
        # 按下
        self.page.mouse.down()
        
        # 随机延迟
        time.sleep(random.uniform(*CLICK_DELAY_RANGE) / 1000)
        
        # 释放
        self.page.mouse.up()
        
        # 额外延迟
        time.sleep(random.uniform(0.05, 0.15))
    
    def double_click(self, selector: str):
        """双击"""
        element = self.wait_for_selector(selector)
        box = element.bounding_box()
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        
        self._physical_click(x, y)
        time.sleep(random.uniform(0.1, 0.2))
        self._physical_click(x, y)


class HumanTyper:
    """模拟人类打字行为"""
    
    def __init__(self, page):
        self.page = page
    
    def type_text(self, selector: str, text: str, clear_first: bool = True):
        """输入文本（模拟人类打字）"""
        # 点击输入框
        self.page.click(selector)
        time.sleep(random.uniform(*TYPE_START_DELAY) / 1000)
        
        # 清空现有内容
        if clear_first:
            self.page.keyboard.press("Control+a")
            time.sleep(random.uniform(50, 150) / 1000)
            self.page.keyboard.press("Backspace")
            time.sleep(random.uniform(50, 150) / 1000)
        
        # 逐字输入
        for char in text:
            self._type_char(char)
        
        # 输入后停顿
        time.sleep(random.uniform(*TYPE_END_DELAY) / 1000)
    
    def _type_char(self, char: str):
        """输入单个字符"""
        if char == " ":
            self.page.keyboard.press("Space")
        elif char == "\n":
            self.page.keyboard.press("Enter")
        elif char.isupper() or char in '!@#$%^&*()_+{}|:"<>?':
            # 需要 Shift 的字符
            self.page.keyboard.down("Shift")
            self.page.keyboard.press(char)
            self.page.keyboard.up("Shift")
        else:
            self.page.keyboard.press(char)
        
        # 随机间隔
        time.sleep(random.uniform(*TYPE_DELAY_RANGE) / 1000)
    
    def paste_text(self, text: str):
        """粘贴文本（更快但可能被检测）"""
        self.page.click(selector)
        time.sleep(0.2)
        self.page.keyboard.press("Control+a")
        time.sleep(0.1)
        self.page.keyboard.press("Backspace")
        time.sleep(0.1)
        
        # 使用 clipboard
        self.page.evaluate(f"""
            navigator.clipboard.writeText({repr(text)})
        """)
        self.page.keyboard.press("Control+v")
        time.sleep(random.uniform(*TYPE_END_DELAY) / 1000)


class HumanScroller:
    """模拟人类滚动行为"""
    
    def __init__(self, page):
        self.page = page
    
    def scroll_to_element(self, selector: str, smooth: bool = True):
        """滚动到元素可见"""
        element = self.page.wait_for_selector(selector, state="attached")
        
        if smooth:
            # 分段滚动模拟人类
            for _ in range(random.randint(3, 6)):
                self.page.mouse.wheel(0, random.randint(*SCROLL_STEP_RANGE))
                time.sleep(random.uniform(*SCROLL_PAUSE_RANGE) / 1000)
        else:
            element.scroll_into_view_if_needed()
    
    def scroll_down(self, pixels: int = None):
        """向下滚动"""
        if pixels is None:
            pixels = random.randint(*SCROLL_STEP_RANGE)
        
        for _ in range(random.randint(2, 4)):
            self.page.mouse.wheel(0, pixels)
            time.sleep(random.uniform(*SCROLL_PAUSE_RANGE) / 1000)
    
    def scroll_up(self, pixels: int = None):
        """向上滚动"""
        if pixels is None:
            pixels = random.randint(*SCROLL_STEP_RANGE)
        
        for _ in range(random.randint(2, 4)):
            self.page.mouse.wheel(0, -pixels)
            time.sleep(random.uniform(*SCROLL_PAUSE_RANGE) / 1000)


class HumanHover:
    """模拟人类悬停行为"""
    
    def __init__(self, page):
        self.page = page
    
    def hover_element(self, selector: str):
        """悬停在元素上"""
        element = self.page.wait_for_selector(selector)
        box = element.bounding_box()
        
        x = box["x"] + box["width"] / 2 + random.randint(-3, 3)
        y = box["y"] + box["height"] / 2 + random.randint(-3, 3)
        
        self.page.mouse.move(x, y)
        time.sleep(random.uniform(*HOVER_DURATION_RANGE) / 1000)


# ============ 快捷键 ============

def send_shortcut(page, *keys):
    """发送快捷键组合"""
    for key in keys[:-1]:
        page.keyboard.down(key)
        time.sleep(random.uniform(30, 80) / 1000)
    
    page.keyboard.press(keys[-1])
    
    for key in reversed(keys[:-1]):
        page.keyboard.up(key)
        time.sleep(random.uniform(30, 80) / 1000)


def escape(page):
    """按 Escape"""
    page.keyboard.press("Escape")
    time.sleep(random.uniform(0.1, 0.3))


def enter(page):
    """按 Enter"""
    page.keyboard.press("Enter")
    time.sleep(random.uniform(0.1, 0.3))


if __name__ == "__main__":
    print("=== CDP Events 模块测试 ===")
    print("需要配合 browser 工具使用")
    print("点击延迟范围:", CLICK_DELAY_RANGE)
    print("打字延迟范围:", TYPE_DELAY_RANGE)
