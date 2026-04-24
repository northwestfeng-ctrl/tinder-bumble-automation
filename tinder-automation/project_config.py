#!/usr/bin/env python3
"""
Tinder 项目本地配置助手。

目标：
- 从环境变量和可选 .env 文件加载配置，避免在源码里硬编码敏感信息。
- 统一浏览器启动参数，减少 manual_login.py 与 core/tinder_bot.py 的漂移。
"""
from __future__ import annotations

import os
import random
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SHARED_ASSETS_ROOT = PROJECT_ROOT.parent / "shared_assets"
DEFAULT_PROFILE_DIR = Path.home() / ".tinder-automation" / "browser-profile"

STEALTH_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-battery",
    "--disable-software-rasterizer",
    "--disable-gpu-sandbox",
    "--ignore-certificate-errors",
    "--disable-setuid-sandbox",
    "--disable-web-security",
]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


for env_path in (SHARED_ASSETS_ROOT / ".env", PROJECT_ROOT / ".env"):
    _load_env_file(env_path)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def build_real_user_agent() -> str:
    chrome_versions = ["124.0.6367.78", "123.0.6312.86", "122.0.6261.124"]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{random.choice(chrome_versions)} "
        "Safari/537.36"
    )


def build_browser_launch_options(headless: bool | None = None) -> dict:
    return {
        "headless": env_bool("TINDER_BROWSER_HEADLESS", True) if headless is None else headless,
        "ignore_default_args": ["--enable-automation"],
        "args": list(STEALTH_BROWSER_ARGS),
        "viewport": {
            "width": env_int("TINDER_VIEWPORT_WIDTH", 1280),
            "height": env_int("TINDER_VIEWPORT_HEIGHT", 800),
        },
        "user_agent": os.getenv("TINDER_BROWSER_USER_AGENT") or build_real_user_agent(),
        "locale": os.getenv("TINDER_BROWSER_LOCALE", "zh-CN"),
        "timezone_id": os.getenv("TINDER_BROWSER_TIMEZONE", "Asia/Shanghai"),
        "permissions": ["geolocation", "notifications"],
        "device_scale_factor": 1.0,
    }


def build_tinder_config(strategy: dict | None = None) -> dict:
    strategy = strategy or {}
    success_patterns = strategy.get("success_patterns", [])
    corpus_examples = [
        item["example"] if isinstance(item, dict) else item
        for item in success_patterns
    ]

    return {
        "account_id": os.getenv("TINDER_ACCOUNT_ID", "tinder_main"),
        "tinder_url": os.getenv("TINDER_URL", "https://tinder.com"),
        "country": os.getenv("TINDER_COUNTRY", "JP"),
        "proxy": None,
        "proxy_sticky_duration": env_int("TINDER_PROXY_STICKY_DURATION", 300),
        "telegram_bot_token": os.getenv("TINDER_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.getenv("TINDER_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID"),
        "llm_api_key": (
            os.getenv("TINDER_LLM_API_KEY")
            or os.getenv("UNIFIED_LLM_API_KEY")
            or os.getenv("APP_LLM__API_KEY")
        ),
        "llm_model": os.getenv("TINDER_LLM_MODEL", "MiniMax-M2.7"),
        "corpus_examples": corpus_examples,
        "llm_temperature": float(os.getenv("TINDER_LLM_TEMPERATURE", "0.7")),
        "llm_top_p": float(os.getenv("TINDER_LLM_TOP_P", "0.9")),
        "llm_presence_penalty": float(os.getenv("TINDER_LLM_PRESENCE_PENALTY", "0.6")),
        "llm_frequency_penalty": float(os.getenv("TINDER_LLM_FREQUENCY_PENALTY", "0.5")),
        "auto_like": env_bool("TINDER_AUTO_LIKE", False),
        "auto_message": env_bool("TINDER_AUTO_MESSAGE", True),
        "max_session_actions": env_int("TINDER_MAX_SESSION_ACTIONS", 20),
        "user_data_dir": os.getenv("TINDER_PROFILE_DIR", str(DEFAULT_PROFILE_DIR)),
        "browser_headless": env_bool("TINDER_BROWSER_HEADLESS", True),
    }


def validate_runtime_config(config: dict) -> list[str]:
    warnings: list[str] = []
    if not config.get("llm_api_key"):
        warnings.append("未检测到 LLM API Key；自动回复功能可能无法生成文案。")
    if not Path(config.get("user_data_dir", str(DEFAULT_PROFILE_DIR))).exists():
        warnings.append("浏览器登录目录尚不存在；首次运行前通常需要先执行 manual_login.py。")
    return warnings
