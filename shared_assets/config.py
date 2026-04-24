#!/usr/bin/env python3
"""
统一配置管理模块
使用 Pydantic 加载和验证配置，支持环境变量覆盖
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator

# ── 加载 .env 文件（如果存在）─────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_file, override=False)


class LLMConfig(BaseModel):
    """LLM 配置"""
    api_key: str = Field(..., description="LLM API Key")
    model: str = Field(default="MiniMax-M2.7", description="模型名称")
    base_url: str = Field(
        default="https://api.minimax.chat/v1/text/chatcompletion_v2",
        description="API 端点"
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=300, ge=1, le=4096)
    timeout: int = Field(default=30, ge=5, le=120, description="请求超时（秒）")
    max_retries: int = Field(default=4, ge=1, le=10, description="最大重试次数")
    max_workers: int = Field(default=3, ge=1, le=16, description="批处理最大并发数")
    batch_size: int = Field(default=5, ge=1, le=50, description="批处理单批请求数")
    fallback_api_key: str = Field(default="", description="超时降级用的备用 API Key")
    fallback_model: str = Field(default="deepseek-chat", description="备用模型名称")
    fallback_base_url: str = Field(default="https://api.deepseek.com/v1", description="备用 API 端点")


class BrowserConfig(BaseModel):
    """浏览器配置"""
    headless: bool = Field(default=True, description="无头模式")
    viewport_width: int = Field(default=1280, ge=800, le=1920)
    viewport_height: int = Field(default=800, ge=600, le=1080)
    user_agent: Optional[str] = Field(default=None, description="自定义 User-Agent")
    
    @field_validator("user_agent")
    @classmethod
    def validate_user_agent(cls, v):
        if v and len(v) < 10:
            raise ValueError("User-Agent 长度过短")
        return v


class ProxyConfig(BaseModel):
    """代理配置"""
    enabled: bool = Field(default=False)
    server: Optional[str] = Field(default=None, description="代理服务器 ip:port")
    username: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    sticky_duration: int = Field(default=300, ge=60, description="会话粘性时长（秒）")


class TinderConfig(BaseModel):
    """Tinder 平台配置"""
    enabled: bool = Field(default=True)
    profile_dir: Path = Field(
        default_factory=lambda: Path.home() / ".tinder-automation" / "browser-profile"
    )
    url: str = Field(default="https://tinder.com")
    max_session_actions: int = Field(default=20, ge=1, le=100)
    cooldown_minutes: int = Field(default=1, ge=1, le=60)


class BumbleConfig(BaseModel):
    """Bumble 平台配置"""
    enabled: bool = Field(default=True)
    profile_dir: Path = Field(
        default_factory=lambda: Path.home() / ".bumble-automation" / "test-profile"
    )
    url: str = Field(default="https://bumble.com/app/connections")
    max_session_actions: int = Field(default=20, ge=1, le=100)
    cooldown_minutes: int = Field(default=1, ge=1, le=60)


class LogConfig(BaseModel):
    """日志配置"""
    level: str = Field(default="INFO", description="日志级别")
    max_bytes: int = Field(default=10*1024*1024, description="单文件最大字节数")
    backup_count: int = Field(default=5, ge=1, le=20, description="备份文件数")
    
    @field_validator("level")
    @classmethod
    def validate_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"日志级别必须是 {valid_levels} 之一")
        return v.upper()


class AppConfig(BaseModel):
    """应用全局配置"""
    # 环境
    env: str = Field(default="production", description="运行环境")
    debug: bool = Field(default=False, description="调试模式")
    
    # 路径
    workspace_dir: Path = Field(
        default_factory=lambda: Path.home() / ".openclaw" / "workspace" / "projects"
    )
    shared_db_path: Path = Field(
        default_factory=lambda: Path.home() / ".openclaw" / "conversation_log.db",
        description="Tinder/Bumble 共享对话语料 SQLite 路径",
    )
    
    # 子配置
    llm: LLMConfig
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    tinder: TinderConfig = Field(default_factory=TinderConfig)
    bumble: BumbleConfig = Field(default_factory=BumbleConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    
    # 演化流水线
    evolution_hour: int = Field(default=3, ge=0, le=23, description="演化流水线执行时间（小时）")
    curfew_start: int = Field(default=0, ge=0, le=23, description="宵禁开始时间")
    curfew_end: int = Field(default=8, ge=0, le=23, description="宵禁结束时间")
    
    class Config:
        env_prefix = "APP_"
        case_sensitive = False


def load_config_from_env() -> AppConfig:
    """
    从环境变量加载配置
    
    环境变量命名规则：
    - APP_DEBUG=true
    - APP_LLM__API_KEY=sk-xxx
    - APP_LLM__MODEL=MiniMax-M2.7
    - APP_BROWSER__HEADLESS=false
    - APP_TINDER__ENABLED=true
    """
    # LLM 配置（必需）
    llm_api_key = os.getenv("UNIFIED_LLM_API_KEY") or os.getenv("APP_LLM__API_KEY")
    if not llm_api_key:
        raise RuntimeError(
            "LLM API Key is required.\n"
            "Set environment variable: UNIFIED_LLM_API_KEY or APP_LLM__API_KEY"
        )
    
    llm_config = LLMConfig(
        api_key=llm_api_key,
        model=os.getenv("APP_LLM__MODEL", "MiniMax-M2.7"),
        base_url=os.getenv(
            "APP_LLM__BASE_URL",
            "https://api.minimax.chat/v1/text/chatcompletion_v2"
        ),
        temperature=float(os.getenv("APP_LLM__TEMPERATURE", "0.7")),
        max_tokens=int(os.getenv("APP_LLM__MAX_TOKENS", "300")),
        timeout=int(os.getenv("APP_LLM__TIMEOUT", "30")),
        max_retries=int(os.getenv("APP_LLM__MAX_RETRIES", "4")),
        max_workers=int(os.getenv("APP_LLM__MAX_WORKERS", "3")),
        batch_size=int(os.getenv("APP_LLM__BATCH_SIZE", "5")),
        fallback_api_key=os.getenv("UNIFIED_LLM_FALLBACK_API_KEY") or os.getenv("APP_LLM__FALLBACK_API_KEY") or "",
        fallback_model=os.getenv("UNIFIED_LLM_FALLBACK_MODEL") or os.getenv("APP_LLM__FALLBACK_MODEL") or "deepseek-chat",
        fallback_base_url=os.getenv("UNIFIED_LLM_FALLBACK_BASE_URL") or os.getenv("APP_LLM__FALLBACK_BASE_URL") or "https://api.deepseek.com/v1",
    )
    
    # 浏览器配置
    browser_config = BrowserConfig(
        headless=os.getenv("APP_BROWSER__HEADLESS", "true").lower() == "true",
        viewport_width=int(os.getenv("APP_BROWSER__VIEWPORT_WIDTH", "1280")),
        viewport_height=int(os.getenv("APP_BROWSER__VIEWPORT_HEIGHT", "800")),
        user_agent=os.getenv("APP_BROWSER__USER_AGENT"),
    )
    
    # 代理配置
    proxy_enabled = os.getenv("APP_PROXY__ENABLED", "false").lower() == "true"
    proxy_config = ProxyConfig(
        enabled=proxy_enabled,
        server=os.getenv("APP_PROXY__SERVER"),
        username=os.getenv("APP_PROXY__USERNAME"),
        password=os.getenv("APP_PROXY__PASSWORD"),
        sticky_duration=int(os.getenv("APP_PROXY__STICKY_DURATION", "300")),
    )
    
    # Tinder 配置
    tinder_config = TinderConfig(
        enabled=os.getenv("APP_TINDER__ENABLED", "true").lower() == "true",
        profile_dir=Path(os.getenv(
            "APP_TINDER__PROFILE_DIR",
            str(Path.home() / ".tinder-automation" / "browser-profile")
        )),
        url=os.getenv("APP_TINDER__URL", "https://tinder.com"),
        max_session_actions=int(os.getenv("APP_TINDER__MAX_SESSION_ACTIONS", "20")),
        cooldown_minutes=int(os.getenv("APP_TINDER__COOLDOWN_MINUTES", "1")),
    )
    
    # Bumble 配置
    bumble_config = BumbleConfig(
        enabled=os.getenv("APP_BUMBLE__ENABLED", "true").lower() == "true",
        profile_dir=Path(os.getenv(
            "APP_BUMBLE__PROFILE_DIR",
            str(Path.home() / ".bumble-automation" / "test-profile")
        )),
        url=os.getenv("APP_BUMBLE__URL", "https://bumble.com/app/connections"),
        max_session_actions=int(os.getenv("APP_BUMBLE__MAX_SESSION_ACTIONS", "20")),
        cooldown_minutes=int(os.getenv("APP_BUMBLE__COOLDOWN_MINUTES", "1")),
    )
    
    # 日志配置
    log_config = LogConfig(
        level=os.getenv("APP_LOG__LEVEL", "INFO"),
        max_bytes=int(os.getenv("APP_LOG__MAX_BYTES", str(10*1024*1024))),
        backup_count=int(os.getenv("APP_LOG__BACKUP_COUNT", "5")),
    )
    
    # 组装完整配置
    return AppConfig(
        env=os.getenv("APP_ENV", "production"),
        debug=os.getenv("APP_DEBUG", "false").lower() == "true",
        workspace_dir=Path(os.getenv(
            "APP_WORKSPACE_DIR",
            str(Path.home() / ".openclaw" / "workspace" / "projects")
        )),
        shared_db_path=Path(os.getenv(
            "APP_SHARED_DB_PATH",
            os.getenv(
                "APP_DATABASE__SHARED_DB_PATH",
                str(Path.home() / ".openclaw" / "conversation_log.db"),
            ),
        )),
        llm=llm_config,
        browser=browser_config,
        proxy=proxy_config,
        tinder=tinder_config,
        bumble=bumble_config,
        log=log_config,
        evolution_hour=int(os.getenv("APP_EVOLUTION_HOUR", "3")),
        curfew_start=int(os.getenv("APP_CURFEW_START", "0")),
        curfew_end=int(os.getenv("APP_CURFEW_END", "8")),
    )


def load_config_from_file(config_path: Path) -> AppConfig:
    """从 JSON/YAML 文件加载配置（可选）"""
    import json
    
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    return AppConfig(**data)


# 全局配置单例
_config: Optional[AppConfig] = None
_config_lock = threading.RLock()


def get_config() -> AppConfig:
    """获取全局配置单例"""
    global _config
    if _config is not None:
        return _config
    with _config_lock:
        if _config is None:
            _config = load_config_from_env()
        return _config


def reload_config():
    """重新加载配置（用于热重载）"""
    global _config
    with _config_lock:
        _config = load_config_from_env()
        return _config
