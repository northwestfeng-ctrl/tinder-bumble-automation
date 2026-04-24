#!/usr/bin/env python3
"""
配置验证测试脚本
用于验证配置加载是否正常
"""
import sys
from pathlib import Path

# 添加 shared_assets 到路径
sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import get_config
    
    def test_config():
        print("=" * 60)
        print("配置验证测试")
        print("=" * 60)
        
        try:
            config = get_config()
            print("\n✅ 配置加载成功\n")
        except Exception as e:
            print(f"\n❌ 配置加载失败: {e}\n")
            print("请检查:")
            print("  1. 是否设置了 UNIFIED_LLM_API_KEY 环境变量")
            print("  2. 是否创建了 .env 文件")
            print("  3. .env 文件格式是否正确")
            return False
        
        # LLM 配置
        print("[LLM 配置]")
        print(f"  API Key: {config.llm.api_key[:20]}...{config.llm.api_key[-10:]}")
        print(f"  Model: {config.llm.model}")
        print(f"  Base URL: {config.llm.base_url}")
        print(f"  Temperature: {config.llm.temperature}")
        print(f"  Max Tokens: {config.llm.max_tokens}")
        print(f"  Timeout: {config.llm.timeout}s")
        print(f"  Max Retries: {config.llm.max_retries}")
        
        # 浏览器配置
        print(f"\n[浏览器配置]")
        print(f"  Headless: {config.browser.headless}")
        print(f"  Viewport: {config.browser.viewport_width}x{config.browser.viewport_height}")
        print(f"  User-Agent: {config.browser.user_agent or '(默认)'}")
        
        # 代理配置
        print(f"\n[代理配置]")
        print(f"  Enabled: {config.proxy.enabled}")
        if config.proxy.enabled:
            print(f"  Server: {config.proxy.server}")
            print(f"  Username: {config.proxy.username or '(未设置)'}")
            print(f"  Sticky Duration: {config.proxy.sticky_duration}s")
        
        # Tinder 配置
        print(f"\n[Tinder 配置]")
        print(f"  Enabled: {config.tinder.enabled}")
        print(f"  Profile Dir: {config.tinder.profile_dir}")
        print(f"  Profile Exists: {config.tinder.profile_dir.exists()}")
        print(f"  URL: {config.tinder.url}")
        print(f"  Max Session Actions: {config.tinder.max_session_actions}")
        print(f"  Cooldown: {config.tinder.cooldown_minutes} min")
        
        # Bumble 配置
        print(f"\n[Bumble 配置]")
        print(f"  Enabled: {config.bumble.enabled}")
        print(f"  Profile Dir: {config.bumble.profile_dir}")
        print(f"  Profile Exists: {config.bumble.profile_dir.exists()}")
        print(f"  URL: {config.bumble.url}")
        print(f"  Max Session Actions: {config.bumble.max_session_actions}")
        print(f"  Cooldown: {config.bumble.cooldown_minutes} min")
        
        # 日志配置
        print(f"\n[日志配置]")
        print(f"  Level: {config.log.level}")
        print(f"  Max Bytes: {config.log.max_bytes / 1024 / 1024:.1f} MB")
        print(f"  Backup Count: {config.log.backup_count}")
        
        # 应用配置
        print(f"\n[应用配置]")
        print(f"  Environment: {config.env}")
        print(f"  Debug: {config.debug}")
        print(f"  Workspace Dir: {config.workspace_dir}")
        print(f"  Evolution Hour: {config.evolution_hour}:00")
        print(f"  Curfew: {config.curfew_start}:00 - {config.curfew_end}:00")
        
        # 验证关键路径
        print(f"\n[路径验证]")
        paths_to_check = [
            ("Workspace", config.workspace_dir),
            ("Tinder Profile", config.tinder.profile_dir),
            ("Bumble Profile", config.bumble.profile_dir),
        ]
        
        for name, path in paths_to_check:
            exists = path.exists()
            status = "✅" if exists else "⚠️  (将自动创建)"
            print(f"  {status} {name}: {path}")
        
        print("\n" + "=" * 60)
        print("✅ 配置验证通过")
        print("=" * 60)
        
        return True
    
    if __name__ == "__main__":
        success = test_config()
        sys.exit(0 if success else 1)

except ImportError as e:
    print(f"❌ 导入失败: {e}")
    print("\n请先安装依赖:")
    print("  pip install pydantic")
    sys.exit(1)
