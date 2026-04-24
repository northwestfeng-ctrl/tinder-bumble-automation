#!/usr/bin/env python3
"""
strategy_config.json 加载器
优先读取 shared_assets/strategy_config.json，避免 Tinder/Bumble 在回复策略上分叉。
"""
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHARED_CONFIG_PATH = PROJECT_ROOT.parent / "shared_assets" / "strategy_config.json"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "strategy_config.json"
CONFIG_PATH = Path(os.environ.get("UNIFIED_STRATEGY_PATH", str(SHARED_CONFIG_PATH)))


def load_strategy():
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}
