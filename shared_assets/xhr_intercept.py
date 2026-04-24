# xhr_intercept.py — Playwright 网络层数据拦截
# 步骤2核心: 替代 DOM 黑名单过滤，直接从 API JSON 提取真实 age/bio/distance
# 适用: Tinder /v2/matches, Bumble /api/v1/user/matches

from playwright.sync_api import sync_playwright, Page, Route
import re
import json
import time
import threading
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
import os

log = logging.getLogger("XHR")


def _privacy_mask_words() -> set[str]:
    raw = os.getenv("APP_PRIVACY_MASK_WORDS", "")
    if not raw:
        return set()
    return {
        item.strip()
        for item in re.split(r"[,，|;；\n]+", raw)
        if item.strip()
    }


# ── 数据结构 ─────────────────────────────────────────────────────────
@dataclass
class MatchProfile:
    """
    从 API 响应解析的完整资料（置信度最高）
    age/distance/bio 全部来自后端 JSON，非 DOM 推测
    """
    match_id: str
    name: str = "?"
    age: int = 0
    bio: str = ""
    gender: str = ""
    city: str = ""
    distance_km: float = 0.0
    is_verified: bool = False
    last_active: str = ""          # ISO timestamp
    photos: list = field(default_factory=list)
    interests: list = field(default_factory=list)

    def __post_init__(self):
        """清洗: 移除己方资料泄露"""
        for frag in _privacy_mask_words():
            self.bio = self.bio.replace(frag, "")
        self.bio = " | ".join(p.strip() for p in self.bio.split("|") if p.strip())
        if self.bio == " | ":
            self.bio = ""


@dataclass
class ChatMessage:
    match_id: str
    sender: str       # "them" | "me"
    text: str
    timestamp: str = ""


# ── Tinder API 拦截器 ─────────────────────────────────────────────────
TINDER_MATCH_RE = re.compile(r"/v2/matches(?:\/[^?\/]+)?/?\?")
TINDER_PROFILE_RE = re.compile(r"/v2/profile(?!\/messages)(?:\/[^?\/]+)?/?\?")
TINDER_MSG_RE = re.compile(r"/v2/messages\/[^\?]+\?")
TINDER_CHAT_RE = re.compile(r"/v2/fast_match_prefs\?")

BUMBLE_MATCH_RE = re.compile(r"/api/v1/user/matches(?:\?|$)")
BUMBLE_PROFILE_RE = re.compile(r"/api/v1/profile/[^\?]+\?")
BUMBLE_CHAT_RE = re.compile(r"/api/v1/conversations/[^\?]+/messages\?")


class XHRInterceptor:
    """
    Playwright page.on("response") 网络层拦截器。

    用法:
        interceptor = XHRInterceptor(page)
        interceptor.start()
        page.goto("https://tinder.com/app/matches")
        page.wait_for_timeout(5000)
        profile = interceptor.get_profile(match_id)
        messages = interceptor.get_messages(match_id)
        interceptor.stop()
    """

    def __init__(self, page: Page):
        self.page = page
        self._profiles: dict[str, MatchProfile] = {}   # match_id → MatchProfile
        self._messages: dict[str, list[ChatMessage]] = {}  # match_id → 消息列表
        self._handlers: list = []
        self._lock = threading.Lock()
        self._active = False

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self):
        if self._active:
            return
        self._active = True
        h1 = self.page.on("response", self._on_response)
        h2 = self.page.on("requestfailed", self._on_fail)
        self._handlers = [h1, h2]
        log.info("[XHR] 拦截器已注入")

    def stop(self):
        if not self._active:
            return
        self._active = False
        for h in self._handlers:
            self.page.off("response", h)
        self._handlers.clear()
        log.info(f"[XHR] 拦截器已移除，已拦截 {len(self._profiles)} 个profile")

    # ── 事件处理 ──────────────────────────────────────────────────
    def _on_response(self, response):
        if not self._active:
            return
        url = response.url
        if response.status != 200:
            return
        try:
            # Tinder
            if TINDER_MATCH_RE.search(url):
                self._parse_tinder_matches(response.url, response)
            elif TINDER_PROFILE_RE.search(url):
                self._parse_tinder_profile(response.url, response)
            elif TINDER_MSG_RE.search(url):
                self._parse_tinder_messages(response.url, response)
            # Bumble
            elif BUMBLE_MATCH_RE.search(url):
                self._parse_bumble_matches(response.url, response)
            elif BUMBLE_PROFILE_RE.search(url):
                self._parse_bumble_profile(response.url, response)
            elif BUMBLE_CHAT_RE.search(url):
                self._parse_bumble_messages(response.url, response)
        except Exception as e:
            log.debug(f"[XHR] 解析异常 {url[:60]}: {e}")

    def _on_fail(self, request):
        log.debug(f"[XHR] 请求失败: {request.url[:80]}")

    # ── Tinder 数据解析 ───────────────────────────────────────────
    def _parse_tinder_matches(self, url: str, response):
        """解析 /v2/matches — 批量获取 match 列表 + 基本资料"""
        try:
            data = response.json()
        except Exception:
            return

        results = data.get("data", {}).get("results", [])
        if not results:
            results = data.get("results", [])

        for m in results:
            match_id = m.get("id") or m.get("match_id", "")
            if not match_id:
                continue

            person = m.get("person") or m.get("user", {}) or {}
            if isinstance(person, str):
                continue

            distance_mi = m.get("distance_miles", 0) or 0
            city_data = person.get("city", {}) or {}
            city_name = ""
            if isinstance(city_data, dict):
                city_name = city_data.get("name", "")
            elif isinstance(city_data, str):
                city_name = city_data

            photos = []
            for ph in (person.get("photos") or []):
                if isinstance(ph, dict):
                    photo_url = ph.get("url") or ph.get("processedFiles", [{}])[0].get("url", "") if isinstance(ph.get("processedFiles"), list) else ""
                    if photo_url:
                        photos.append(photo_url)

            with self._lock:
                self._profiles[match_id] = MatchProfile(
                    match_id=match_id,
                    name=person.get("name", "?"),
                    age=int(person.get("age", 0) or 0),
                    bio=person.get("bio", ""),
                    gender=person.get("gender", ""),
                    city=city_name,
                    distance_km=distance_mi * 1.60934 if distance_mi else 0,
                    is_verified=bool(person.get("is_verified", False)),
                    last_active=m.get("last_activity_time", ""),
                    photos=photos,
                )

    def _parse_tinder_profile(self, url: str, response):
        """解析 /v2/profile/{id} — 单用户完整资料"""
        try:
            data = response.json()
        except Exception:
            return

        user = data.get("data", {}) or data
        match_id = user.get("id", "")
        if not match_id:
            return

        interests = []
        for tag in (user.get("interests", []) or []):
            if isinstance(tag, dict):
                interests.append(tag.get("name", ""))
            elif isinstance(tag, str):
                interests.append(tag)

        with self._lock:
            if match_id in self._profiles:
                p = self._profiles[match_id]
                p.bio = user.get("bio", "") or p.bio
                p.gender = user.get("gender", "") or p.gender
                p.interests = interests or p.interests
                p.latitude = float(user.get("latitude", 0) or 0)
                p.longitude = float(user.get("longitude", 0) or 0)
            else:
                self._profiles[match_id] = MatchProfile(
                    match_id=match_id,
                    name=user.get("name", "?"),
                    age=int(user.get("age", 0) or 0),
                    bio=user.get("bio", ""),
                    gender=user.get("gender", ""),
                    interests=interests,
                )

    def _parse_tinder_messages(self, url: str, response):
        """解析 /v2/messages/{match_id} — 聊天记录"""
        try:
            data = response.json()
        except Exception:
            return

        messages = data.get("data", {}).get("messages", []) or data.get("messages", [])
        match_id = data.get("data", {}).get("match_id", "")
        if not match_id:
            m_id = re.search(r"/v2/messages\/([^?]+)", url)
            if m_id:
                match_id = m_id.group(1)

        if not match_id or not messages:
            return

        parsed = []
        for m in messages:
            sender_id = m.get("from", "")
            is_mine = "_sent" in sender_id or sender_id.endswith("_me")
            parsed.append(ChatMessage(
                match_id=match_id,
                sender="me" if is_mine else "them",
                text=m.get("body", "") or m.get("text", ""),
                timestamp=m.get("sent_date", ""),
            ))

        with self._lock:
            self._messages[match_id] = parsed

    # ── Bumble 数据解析 ───────────────────────────────────────────
    def _parse_bumble_matches(self, url: str, response):
        """解析 Bumble /api/v1/user/matches"""
        try:
            data = response.json()
        except Exception:
            return

        matches = data.get("data", {}).get("matches", []) or data.get("matches", [])
        for m in matches:
            match_id = m.get("id", "")
            if not match_id:
                continue
            user = m.get("user", {}) or m.get("profile", {}) or {}
            if isinstance(user, str):
                continue

            loc = user.get("location", {}) or {}
            if isinstance(loc, dict):
                city = loc.get("city", {})
                if isinstance(city, dict):
                    city = city.get("name", "")

            with self._lock:
                self._profiles[match_id] = MatchProfile(
                    match_id=match_id,
                    name=user.get("name", "?"),
                    age=int(user.get("age", 0) or 0),
                    bio=user.get("about", "") or user.get("bio", ""),
                    city=str(city) if city else "",
                    distance_km=float(m.get("distance", 0) or 0),
                    is_verified=bool(user.get("is_verified", False)),
                    last_active=m.get("last_activity", ""),
                )

    def _parse_bumble_profile(self, url: str, response):
        """解析 Bumble /api/v1/profile/{id}"""
        try:
            data = response.json()
        except Exception:
            return

        user = data.get("data", {}) or data
        match_id = user.get("id", "")
        if not match_id:
            return

        with self._lock:
            if match_id in self._profiles:
                self._profiles[match_id].bio = user.get("about", "") or user.get("bio", "")
            else:
                self._profiles[match_id] = MatchProfile(
                    match_id=match_id,
                    name=user.get("name", "?"),
                    age=int(user.get("age", 0) or 0),
                    bio=user.get("about", "") or user.get("bio", ""),
                )

    def _parse_bumble_messages(self, url: str, response):
        """解析 Bumble 消息"""
        try:
            data = response.json()
        except Exception:
            return

        messages = data.get("data", {}).get("messages", []) or data.get("messages", [])
        m_id = re.search(r"/conversations\/([^/]+)/messages", url)
        match_id = m_id.group(1) if m_id else ""

        if not match_id or not messages:
            return

        parsed = []
        for m in messages:
            parsed.append(ChatMessage(
                match_id=match_id,
                sender="them" if m.get("is_received", False) else "me",
                text=m.get("text", "") or m.get("body", ""),
                timestamp=m.get("timestamp", ""),
            ))

        with self._lock:
            self._messages[match_id] = parsed

    # ── 查询接口 ──────────────────────────────────────────────────
    def get_profile(self, match_id: str) -> Optional[MatchProfile]:
        with self._lock:
            return self._profiles.get(match_id)

    def get_messages(self, match_id: str) -> list[ChatMessage]:
        with self._lock:
            return self._messages.get(match_id, [])

    def get_all_profiles(self) -> list[MatchProfile]:
        with self._lock:
            return list(self._profiles.values())

    def get_all_match_ids(self) -> list[str]:
        with self._lock:
            return list(self._profiles.keys())

    @property
    def profile_count(self) -> int:
        with self._lock:
            return len(self._profiles)

    @property
    def message_count(self) -> int:
        with self._lock:
            return len(self._messages)


# ── 辅助: 静默打印已拦截的 API ───────────────────────────────────
def intercept_and_print(page: Page, platform: str = "tinder", wait_ms: int = 5000):
    """
    诊断用: 拦截页面所有含 'api' 的响应，打印 URL 和响应体摘要。
    用于 F12 验证阶段。
    """
    intercepted = []

    def handler(response):
        url = response.url
        if "api" not in url.lower() and "match" not in url.lower():
            return
        if response.status != 200:
            return
        try:
            d = response.json()
            size = len(json.dumps(d))
        except Exception:
            size = -1
        intercepted.append({
            "url": url[:100],
            "status": response.status,
            "size": size,
        })

    page.on("response", handler)
    page.wait_for_timeout(wait_ms)
    page.off("response", handler)

    print(f"\n[intercept_and_print] 平台={platform}, 耗时={wait_ms}ms, 捕获API数={len(intercepted)}")
    for item in intercepted[:20]:
        print(f"  [{item['status']}] {item['size']}B | {item['url']}")
    return intercepted
