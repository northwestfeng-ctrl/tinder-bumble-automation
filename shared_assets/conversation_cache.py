#!/usr/bin/env python3
"""
对话历史缓存模块
提供基于 TTL 的对话历史缓存，避免重复抓取
"""
from __future__ import annotations

import time
import json
import hashlib
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime

log = logging.getLogger("ConversationCache")


@dataclass
class CachedConversation:
    """缓存的对话数据"""
    contact_id: str
    messages: List[Dict[str, Any]]
    bio: str
    cached_at: float = field(default_factory=time.time)
    content_hash: str = ""
    hit_count: int = 0
    
    def __post_init__(self):
        """计算内容哈希"""
        if not self.content_hash:
            self.content_hash = self._compute_hash()
    
    def _compute_hash(self) -> str:
        """计算消息内容哈希"""
        # 只用最后一条消息的文本做哈希（快速检测变化）
        if not self.messages:
            return ""
        last_msg = self.messages[-1]
        content = f"{last_msg.get('sender')}:{last_msg.get('text', '')}"
        return hashlib.md5(content.encode()).hexdigest()[:16]
    
    def is_expired(self, ttl_seconds: int) -> bool:
        """检查是否过期"""
        return (time.time() - self.cached_at) > ttl_seconds
    
    def age_seconds(self) -> float:
        """缓存年龄（秒）"""
        return time.time() - self.cached_at
    
    def mark_hit(self):
        """标记缓存命中"""
        self.hit_count += 1


class ConversationCache:
    """
    对话历史缓存管理器
    
    功能：
    - TTL 过期（默认 5 分钟）
    - 内容哈希检测变化
    - LRU 淘汰（最大 100 条）
    - 持久化到磁盘
    """
    
    def __init__(
        self,
        cache_dir: Path,
        ttl_seconds: int = 300,  # 5 分钟
        max_entries: int = 100,
    ):
        """
        初始化缓存管理器
        
        Args:
            cache_dir: 缓存目录
            ttl_seconds: 缓存有效期（秒）
            max_entries: 最大缓存条目数
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        
        self.cache: Dict[str, CachedConversation] = {}
        self.access_order: List[str] = []  # LRU 顺序
        
        # 统计
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "expirations": 0,
        }
        
        # 加载持久化缓存
        self._load_from_disk()
        
        log.info(
            f"ConversationCache 初始化 "
            f"(ttl={ttl_seconds}s, max={max_entries}, "
            f"loaded={len(self.cache)})"
        )
    
    def get(
        self,
        contact_id: str,
        content_hash: Optional[str] = None,
    ) -> Optional[CachedConversation]:
        """
        获取缓存的对话
        
        Args:
            contact_id: 联系人 ID
            content_hash: 内容哈希（用于验证是否有新消息）
        
        Returns:
            缓存的对话，如果未命中或过期则返回 None
        """
        if contact_id not in self.cache:
            self.stats["misses"] += 1
            log.debug(f"缓存未命中: {contact_id}")
            return None
        
        cached = self.cache[contact_id]
        
        # 检查过期
        if cached.is_expired(self.ttl_seconds):
            log.debug(
                f"缓存过期: {contact_id} "
                f"(age={cached.age_seconds():.0f}s > {self.ttl_seconds}s)"
            )
            self.stats["expirations"] += 1
            self.invalidate(contact_id)
            return None
        
        # 检查内容变化
        if content_hash and content_hash != cached.content_hash:
            log.debug(
                f"内容已变化: {contact_id} "
                f"(hash {cached.content_hash} → {content_hash})"
            )
            self.stats["misses"] += 1
            self.invalidate(contact_id)
            return None
        
        # 缓存命中
        self.stats["hits"] += 1
        cached.mark_hit()
        self._update_access_order(contact_id)
        
        log.debug(
            f"缓存命中: {contact_id} "
            f"(age={cached.age_seconds():.0f}s, hits={cached.hit_count})"
        )
        
        return cached
    
    def put(
        self,
        contact_id: str,
        messages: List[Dict[str, Any]],
        bio: str = "",
    ):
        """
        缓存对话
        
        Args:
            contact_id: 联系人 ID
            messages: 消息列表
            bio: 对方资料
        """
        # LRU 淘汰
        if len(self.cache) >= self.max_entries and contact_id not in self.cache:
            self._evict_lru()
        
        cached = CachedConversation(
            contact_id=contact_id,
            messages=messages,
            bio=bio,
        )
        
        self.cache[contact_id] = cached
        self._update_access_order(contact_id)
        
        log.debug(
            f"缓存写入: {contact_id} "
            f"({len(messages)} 条消息, hash={cached.content_hash})"
        )
    
    def invalidate(self, contact_id: str):
        """使缓存失效"""
        if contact_id in self.cache:
            del self.cache[contact_id]
            if contact_id in self.access_order:
                self.access_order.remove(contact_id)
            log.debug(f"缓存失效: {contact_id}")
    
    def clear(self):
        """清空所有缓存"""
        count = len(self.cache)
        self.cache.clear()
        self.access_order.clear()
        log.info(f"缓存已清空 ({count} 条)")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total_requests = self.stats["hits"] + self.stats["misses"]
        hit_rate = (
            self.stats["hits"] / total_requests * 100
            if total_requests > 0
            else 0
        )
        
        return {
            **self.stats,
            "total_requests": total_requests,
            "hit_rate": f"{hit_rate:.1f}%",
            "cached_entries": len(self.cache),
            "max_entries": self.max_entries,
            "ttl_seconds": self.ttl_seconds,
        }
    
    def save_to_disk(self):
        """持久化缓存到磁盘"""
        cache_file = self.cache_dir / "conversation_cache.json"
        
        data = {
            "version": 1,
            "saved_at": time.time(),
            "stats": self.stats,
            "entries": {
                contact_id: {
                    **asdict(cached),
                    "messages": cached.messages[:10],  # 只保存最近 10 条
                }
                for contact_id, cached in self.cache.items()
            },
        }
        
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.debug(f"缓存已保存: {cache_file} ({len(self.cache)} 条)")
        except Exception as e:
            log.error(f"缓存保存失败: {e}")
    
    def _load_from_disk(self):
        """从磁盘加载缓存"""
        cache_file = self.cache_dir / "conversation_cache.json"
        
        if not cache_file.exists():
            return
        
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 加载统计
            if "stats" in data:
                self.stats.update(data["stats"])
            
            # 加载条目（跳过过期的）
            now = time.time()
            for contact_id, entry in data.get("entries", {}).items():
                cached_at = entry.get("cached_at", 0)
                if (now - cached_at) < self.ttl_seconds:
                    self.cache[contact_id] = CachedConversation(**entry)
                    self.access_order.append(contact_id)
            
            log.info(
                f"缓存已加载: {len(self.cache)} 条 "
                f"(跳过 {len(data.get('entries', {})) - len(self.cache)} 条过期)"
            )
        except Exception as e:
            log.error(f"缓存加载失败: {e}")
    
    def _update_access_order(self, contact_id: str):
        """更新 LRU 访问顺序"""
        if contact_id in self.access_order:
            self.access_order.remove(contact_id)
        self.access_order.append(contact_id)
    
    def _evict_lru(self):
        """淘汰最久未使用的条目"""
        if not self.access_order:
            return
        
        victim = self.access_order[0]
        self.invalidate(victim)
        self.stats["evictions"] += 1
        
        log.debug(f"LRU 淘汰: {victim}")


# 全局缓存实例（单例）
_cache: Optional[ConversationCache] = None


def get_conversation_cache(config: Any) -> ConversationCache:
    """
    获取全局对话缓存实例（单例）
    
    Args:
        config: 统一配置对象
    
    Returns:
        ConversationCache 实例
    """
    global _cache
    if _cache is None:
        cache_dir = config.workspace_dir / "cache" / "conversations"
        _cache = ConversationCache(
            cache_dir=cache_dir,
            ttl_seconds=300,  # 5 分钟
            max_entries=100,
        )
    return _cache


def cleanup_cache():
    """清理全局缓存"""
    global _cache
    if _cache:
        _cache.save_to_disk()
        _cache = None
