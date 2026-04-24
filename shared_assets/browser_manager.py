#!/usr/bin/env python3
"""
浏览器实例管理器
提供浏览器实例的复用、健康检查、自动重建功能
"""
from __future__ import annotations

import os
import sys
import time
import logging
import importlib.util
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("BrowserManager")


_shared_playwright: Any = None
_STALE_LOCK_PATTERNS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _load_tinder_project_config():
    module_name = "tinder_project_config"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    module_path = Path(__file__).resolve().parent.parent / "tinder-automation" / "project_config.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 Tinder project_config: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class BrowserInstance:
    """浏览器实例包装"""
    playwright: Any
    context: Any
    page: Any
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    error_count: int = 0
    
    def is_alive(self) -> bool:
        """检查浏览器是否存活"""
        try:
            if not self.page or self.page.is_closed():
                return False
            # 移除 evaluate 探针，防止遇到 WAF 验证时引发 30s 阻塞死锁
            return True
        except Exception:
            return False
    
    def mark_used(self):
        """标记使用时间"""
        self.last_used = time.time()
    
    def mark_error(self):
        """标记错误"""
        self.error_count += 1
    
    def reset_errors(self):
        """重置错误计数"""
        self.error_count = 0
    
    def age_seconds(self) -> float:
        """实例年龄（秒）"""
        return time.time() - self.created_at
    
    def idle_seconds(self) -> float:
        """空闲时长（秒）"""
        return time.time() - self.last_used


class BrowserManager:
    """
    浏览器实例管理器
    
    功能：
    - 实例复用（避免频繁启动/关闭）
    - 健康检查（自动检测失活实例）
    - 自动重建（失败时重新创建）
    - 配置化（从统一配置读取）
    """
    
    def __init__(self, platform: str, config: Any):
        """
        初始化管理器
        
        Args:
            platform: 平台名称（tinder/bumble）
            config: 统一配置对象
        """
        self.platform = platform
        self.config = config
        self.instance: Optional[BrowserInstance] = None
        
        # 配置参数
        self.max_age_seconds = 3600 * 2  # 2小时后强制重建
        self.max_errors = 3  # 连续3次错误后重建
        self.idle_timeout = 600  # 10分钟空闲后可回收
        
        log.info(f"[{platform}] BrowserManager 初始化")

    def get_instance(self) -> BrowserInstance:
        """
        获取浏览器实例对象（向后兼容旧调用方）。
        """
        self.get_browser()
        return self.instance
    
    def get_browser(self) -> tuple[Any, Any]:
        """
        获取浏览器实例（复用或新建）
        
        Returns:
            (context, page) 元组
        """
        # 检查现有实例
        if self.instance:
            # 健康检查
            if self._should_rebuild():
                log.info(f"[{self.platform}] 实例需要重建")
                self._cleanup_instance()
            elif self.instance.is_alive():
                log.debug(f"[{self.platform}] 复用现有实例")
                self.instance.mark_used()
                self.instance.reset_errors()
                return self.instance.context, self.instance.page
            else:
                log.warning(f"[{self.platform}] 实例已失活，重建")
                self._cleanup_instance()
        
        # 创建新实例
        log.info(f"[{self.platform}] 创建新浏览器实例")
        self.instance = self._create_instance()
        return self.instance.context, self.instance.page
    
    def mark_error(self):
        """标记当前实例发生错误"""
        if self.instance:
            self.instance.mark_error()
            log.warning(
                f"[{self.platform}] 实例错误计数: "
                f"{self.instance.error_count}/{self.max_errors}"
            )
    
    def cleanup(self):
        """清理所有资源"""
        if self.instance:
            log.info(f"[{self.platform}] 清理浏览器实例")
            self._cleanup_instance()
    
    def _should_rebuild(self) -> bool:
        """判断是否需要重建实例"""
        if not self.instance:
            return True
        
        # 年龄过大
        if self.instance.age_seconds() > self.max_age_seconds:
            log.info(
                f"[{self.platform}] 实例年龄过大 "
                f"({self.instance.age_seconds():.0f}s > {self.max_age_seconds}s)"
            )
            return True
        
        # 错误过多
        if self.instance.error_count >= self.max_errors:
            log.info(
                f"[{self.platform}] 错误次数过多 "
                f"({self.instance.error_count} >= {self.max_errors})"
            )
            return True
        
        return False
    
    def _create_instance(self) -> BrowserInstance:
        """创建新的浏览器实例"""
        from playwright.sync_api import sync_playwright
        
        # 获取平台配置
        if self.platform == "tinder":
            platform_config = self.config.tinder
        elif self.platform == "bumble":
            platform_config = self.config.bumble
        else:
            raise ValueError(f"未知平台: {self.platform}")

        self._cleanup_stale_profile_locks(Path(platform_config.profile_dir))
        
        # Playwright driver 在进程内共享，避免双平台重复 start() 触发冲突
        global _shared_playwright
        if _shared_playwright is None:
            _shared_playwright = sync_playwright().start()
        playwright = _shared_playwright

        headless = self._resolve_headless()

        # 代理配置
        proxy = None
        if self.config.proxy.enabled and self.config.proxy.server:
            proxy = {
                "server": self.config.proxy.server,
            }
            if self.config.proxy.username:
                proxy["username"] = self.config.proxy.username
            if self.config.proxy.password:
                proxy["password"] = self.config.proxy.password

        context_kwargs = {
            "headless": headless,
            "proxy": proxy,
            "viewport": {
                'width': self.config.browser.viewport_width,
                'height': self.config.browser.viewport_height,
            },
            "user_agent": self.config.browser.user_agent,
        }

        # Tinder 对启动参数更敏感：尽量与项目本地配置保持一致，降低无头模式卡在 loading screen 的概率。
        launch_args = ["--headless=new" if headless else ""]
        launch_args = [arg for arg in launch_args if arg]
        if self.platform == "tinder":
            try:
                tinder_options = _load_tinder_project_config().build_browser_launch_options(headless=headless)
                merged_args = tinder_options.get("args", []) + launch_args
                deduped_args = list(dict.fromkeys(arg for arg in merged_args if arg))
                context_kwargs.update({
                    "ignore_default_args": tinder_options.get("ignore_default_args"),
                    "args": deduped_args,
                    "viewport": tinder_options.get("viewport", context_kwargs["viewport"]),
                    "user_agent": tinder_options.get("user_agent", context_kwargs["user_agent"]),
                    "locale": tinder_options.get("locale"),
                    "timezone_id": tinder_options.get("timezone_id"),
                    "permissions": tinder_options.get("permissions"),
                    "device_scale_factor": tinder_options.get("device_scale_factor"),
                })
            except Exception as exc:
                log.warning(f"[{self.platform}] 载入 Tinder 启动参数失败，回退通用配置: {exc}")
                context_kwargs["args"] = ["--disable-blink-features=AutomationControlled", *launch_args]
        else:
            context_kwargs["args"] = ["--disable-blink-features=AutomationControlled", *launch_args]
        
        # 启动持久化上下文
        try:
            context = playwright.chromium.launch_persistent_context(
                str(platform_config.profile_dir),
                **context_kwargs,
            )
        except Exception:
            # 若首次启动失败且当前没有任何活跃实例，回收共享 driver，避免污染后续平台启动
            if not any(manager.instance for manager in _managers.values()):
                try:
                    playwright.stop()
                except Exception:
                    pass
                _shared_playwright = None
            raise
        
        # 获取或创建页面
        page = context.pages[0] if context.pages else context.new_page()
        
        log.info(
            f"[{self.platform}] 浏览器实例创建成功 "
            f"(headless={headless}, "
            f"proxy={bool(proxy)})"
        )
        
        return BrowserInstance(
            playwright=playwright,
            context=context,
            page=page,
        )

    def _cleanup_stale_profile_locks(self, profile_dir: Path) -> None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        for pattern in _STALE_LOCK_PATTERNS:
            for path in profile_dir.glob(pattern):
                try:
                    path.unlink(missing_ok=True)
                    log.info(f"[{self.platform}] 已清理残留锁文件: {path.name}")
                except OSError as exc:
                    log.warning(f"[{self.platform}] 清理锁文件失败 {path.name}: {exc}")

    def _resolve_headless(self) -> bool:
        """
        解析平台实际使用的 headless 配置。

        默认遵循平台配置；如需临时覆盖，可显式设置
        <PLATFORM>_BROWSER_HEADLESS 环境变量。
        """
        env_name = f"{self.platform.upper()}_BROWSER_HEADLESS"
        raw_value = os.getenv(env_name)
        if raw_value is not None:
            return raw_value.strip().lower() in {"1", "true", "yes", "on"}

        return self.config.browser.headless
    
    def _cleanup_instance(self):
        """清理当前实例"""
        if not self.instance:
            return
        
        try:
            if self.instance.context:
                self.instance.context.close()
        except Exception as e:
            log.error(f"[{self.platform}] 清理实例时出错: {e}")
        finally:
            self.instance = None


# 全局管理器实例（单例）
_managers: Dict[str, BrowserManager] = {}


def get_browser_manager(platform: str, config: Any) -> BrowserManager:
    """
    获取平台的浏览器管理器（单例）
    
    Args:
        platform: 平台名称（tinder/bumble）
        config: 统一配置对象
    
    Returns:
        BrowserManager 实例
    """
    if platform not in _managers:
        _managers[platform] = BrowserManager(platform, config)
    return _managers[platform]


def cleanup_all_managers():
    """清理所有管理器"""
    global _shared_playwright
    for platform, manager in _managers.items():
        log.info(f"清理 {platform} 管理器")
        manager.cleanup()
    _managers.clear()
    if _shared_playwright is not None:
        try:
            _shared_playwright.stop()
        except Exception as e:
            log.error(f"清理共享 Playwright 时出错: {e}")
        finally:
            _shared_playwright = None
