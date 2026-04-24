#!/usr/bin/env python3
"""
Tinder 自动化 - 模块4: 网络环境与地理隔离
功能：
- 住宅代理绑定
- IP 地理位置验证
- 网络上下文一致性
"""
import json
import random
import subprocess
import time
from typing import Optional
from urllib.request import urlopen
from urllib.error import URLError

# ============ 代理配置模板 ============

PROXY_CONFIG = {
    "japan": {
        "server": "proxy.example.com:8080",  # 需替换真实代理
        "username": "user",
        "password": "pass",
        "country": "JP",
        "timezone": "Asia/Tokyo",
        "locale": "ja-JP",
    },
    "usa": {
        "server": "us.proxy.example.com:8080",
        "username": "user",
        "password": "pass",
        "country": "US",
        "timezone": "America/New_York",
        "locale": "en-US",
    },
}


def check_proxy_connectivity(proxy: dict) -> bool:
    """检查代理连通性"""
    try:
        import requests
        proxies = {
            "http": f"http://{proxy['username']}:{proxy['password']}@{proxy['server']}",
            "https": f"http://{proxy['username']}:{proxy['password']}@{proxy['server']}",
        }
        response = requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10
        )
        ip_data = response.json()
        return ip_data.get("ip") is not None
    except Exception as e:
        print(f"代理连通性检查失败: {e}")
        return False


def get_current_ip_info() -> dict:
    """获取当前 IP 信息"""
    try:
        import requests
        response = requests.get("https://ipapi.co/json/", timeout=10)
        return response.json()
    except Exception:
        return {"error": "无法获取IP信息"}


def verify_geographic_consistency(proxy: dict, expected_country: str) -> bool:
    """
    验证地理一致性
    检查 IP 归属地是否与预期一致
    """
    try:
        ip_info = get_current_ip_info()
        
        if "error" in ip_info:
            print(f"IP 信息获取失败: {ip_info['error']}")
            return False
        
        actual_country = ip_info.get("country_code", "")
        
        if actual_country != expected_country:
            print(f"地理不一致: 预期 {expected_country}, 实际 {actual_country}")
            return False
        
        print(f"地理验证通过: {ip_info.get('city')}, {ip_info.get('country_name')}")
        return True
        
    except Exception as e:
        print(f"地理验证异常: {e}")
        return False


class ProxyRotator:
    """
    代理轮换器 - 支持会话粘性
    
    【改进】Session Sticky:
    - 同一个聊天对象，5分钟内保持同一IP
    - IP高频跨区跳动是封号最敏感触发条件
    """
    
    def __init__(self, proxies: list):
        self.proxies = proxies
        self.current_index = 0
        self.usage_count = {}
        self.sticky_session = None  # 当前粘性会话
        self.sticky_expires = 0  # 粘性过期时间
        
        for i, p in enumerate(proxies):
            self.usage_count[i] = 0
    
    def get_proxy_for_session(self, session_id: str = None, sticky_duration: int = 300) -> Optional[dict]:
        """
        获取代理 - 支持会话粘性
        
        Args:
            session_id: 会话标识（聊天对象ID等）
            sticky_duration: 粘性保持秒数（默认5分钟）
        
        同一个session_id在sticky_duration内返回同一代理
        """
        now = time.time()
        
        # 检查当前粘性是否有效
        if (self.sticky_session == session_id 
            and self.sticky_expires > now
            and self.current_index < len(self.proxies)):
            print(f"[Proxy] 保持粘性会话 {session_id}，剩余 {int(self.sticky_expires - now)}s")
            return self.proxies[self.current_index]
        
        # 获取新代理
        proxy = self.get_next_proxy()
        
        if proxy:
            self.sticky_session = session_id
            self.sticky_expires = now + sticky_duration
            print(f"[Proxy] 新建粘性会话 {session_id}，持续 {sticky_duration}s")
        
        return proxy
    
    def get_next_proxy(self) -> Optional[dict]:
        """获取下一个代理（轮换）"""
        if not self.proxies:
            return None
        
        proxy = self.proxies[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.proxies)
        self.usage_count[self.current_index] += 1
        
        return proxy
    
    def get_least_used_proxy(self) -> Optional[dict]:
        """获取使用次数最少的代理"""
        if not self.proxies:
            return None
        
        min_usage = min(self.usage_count.values())
        for i, count in self.usage_count.items():
            if count == min_usage:
                return self.proxies[i]
        
        return self.proxies[0]
    
    def mark_failed(self, proxy: dict):
        """标记失败的代理"""
        for i, p in enumerate(self.proxies):
            if p["server"] == proxy["server"]:
                print(f"代理 {proxy['server']} 标记为失败")


class NetworkContext:
    """
    网络上下文一致性管理器
    确保 IP、时区、语言设置一致
    """
    
    def __init__(self, country: str = "JP", proxy: dict = None):
        self.country = country
        self.proxy = proxy
        self.context_config = self._build_context_config()
    
    def _build_context_config(self) -> dict:
        """构建浏览器上下文配置"""
        configs = {
            "JP": {
                "locale": "ja-JP",
                "timezone": "Asia/Tokyo",
                "geolocation": {"longitude": 139.6917, "latitude": 35.6895},
                "languages": ["ja-JP", "ja", "en"],
                "http_headers": {
                    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            },
            "US": {
                "locale": "en-US",
                "timezone": "America/New_York",
                "geolocation": {"longitude": -73.9857, "latitude": 40.7484},
                "languages": ["en-US", "en"],
                "http_headers": {
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            },
        }
        
        config = configs.get(self.country, configs["US"]).copy()
        
        # 随机化屏幕分辨率
        resolutions = {
            "JP": [(1280, 800), (1366, 768), (1440, 900)],
            "US": [(1280, 720), (1366, 768), (1920, 1080)],
        }
        width, height = random.choice(resolutions.get(self.country, [(1366, 768)]))
        config["viewport"] = {
            "width": width,
            "height": height,
            "device_scale_factor": random.choice([1, 1.25, 1.5]),
        }
        
        return config
    
    def get_browser_context_options(self) -> dict:
        """获取浏览器上下文创建参数"""
        options = {
            "viewport": self.context_config["viewport"],
            "locale": self.context_config["locale"],
            "timezone_id": self.context_config["timezone"],
            "geolocation": self.context_config.get("geolocation"),
            "permissions": ["geolocation"],
            "extra_http_headers": self.context_config["http_headers"],
            "user_agent": self._generate_user_agent(),
        }
        
        if self.proxy:
            options["proxy"] = {
                "server": f"http://{self.proxy['username']}:{self.proxy['password']}@{self.proxy['server']}"
            }
        
        return options
    
    def _generate_user_agent(self) -> str:
        """生成匹配的 User-Agent"""
        chrome_versions = ["124.0.6367.78", "123.0.6312.86", "122.0.6261.124"]
        os_options = {
            "JP": "Macintosh; Intel Mac OS X 10_15_7",
            "US": "Windows NT 10.0; Win64; x64",
        }
        os_str = os_options.get(self.country, os_options["US"])
        
        return (
            f"Mozilla/5.0 ({os_str}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{random.choice(chrome_versions)} "
            f"Safari/537.36"
        )
    
    def verify_context(self) -> bool:
        """验证当前上下文一致性"""
        try:
            # 检查 IP
            ip_info = get_current_ip_info()
            if "error" in ip_info:
                return False
            
            # IP 国家和预期不一致
            if self.country == "JP" and ip_info.get("country_code") != "JP":
                return False
            if self.country == "US" and ip_info.get("country_code") != "US":
                return False
            
            return True
            
        except Exception as e:
            print(f"上下文验证失败: {e}")
            return False


def create_stealth_context(page, network_context: NetworkContext):
    """为已有页面应用网络上下文"""
    context_opts = network_context.get_browser_context_options()
    
    # 更新 extra HTTP headers
    page.set_extra_http_headers(context_opts["extra_http_headers"])
    
    # 设置 User-Agent
    page.set_user_agent(context_opts.get("user_agent", ""))
    
    return context_opts


if __name__ == "__main__":
    print("=== Network Isolation 模块测试 ===")
    print("当前 IP 信息:", get_current_ip_info())
