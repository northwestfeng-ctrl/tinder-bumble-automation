#!/usr/bin/env python3
"""
core/bumble_bot.py
Bumble Playwright 底层封装
- 独立 Stealth 配置（区别于 Tinder Profile）
- 反爬参数注入
- 登录态校验
"""
import sys
import os
import time
import random
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from playwright_stealth.stealth import Stealth  # 复用同一 stealth 引擎

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared_assets"))
from unified_send_message import send_message_unified
from unified_reply_engine import sanitize_reply_for_send


BUMBLE_STRATEGY_FILE = Path(
    os.getenv(
        "BUMBLE_STRATEGY_FILE",
        str(Path(__file__).parent.parent / "bumble_strategy.json"),
    )
)

DEFAULT_BUMBLE_STRATEGY = {
    "auto_like_keywords": [],
    "auto_like_probability": 0.3,
    "fingerprints": [
        {
            "languages": ["zh-CN", "zh", "en-US", "en"],
            "plugins": [
                "Chrome PDF Plugin",
                "Chrome PDF Viewer",
                "Native Client",
                "Chromium PDF Plugin",
            ],
            "webgl_vendor": "Intel Inc.",
            "webgl_renderer": "Intel Iris OpenGL Engine",
        }
    ],
}


class BumbleBot:
    def __init__(self, profile_path: str):
        self.profile_path = profile_path
        self.browser = None
        self.context = None
        self.page = None
        self.strategy = self._load_strategy()

    @staticmethod
    def _load_strategy() -> dict:
        strategy = dict(DEFAULT_BUMBLE_STRATEGY)
        try:
            if BUMBLE_STRATEGY_FILE.exists():
                data = json.loads(BUMBLE_STRATEGY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    strategy.update(data)
        except Exception as exc:
            print(f"  [BumbleBot] 策略文件读取失败，使用默认值: {exc}")
        return strategy

    # ── Stealth 参数 ──────────────────────────────

    STEALTH_ARGS = [
        "--headless=new",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-accelerated-2d-canvas",
        "--disable-gpu",
        "--window-size=1280,800",
        "--disable-web-security",
        "--disable-features=Translate",
    ]

    def _stealth_js(self) -> str:
        """注入 JS 指纹伪装，移除 automation 特征"""
        fingerprints = self.strategy.get("fingerprints") or DEFAULT_BUMBLE_STRATEGY["fingerprints"]
        fingerprint = random.choice(fingerprints) if isinstance(fingerprints, list) and fingerprints else DEFAULT_BUMBLE_STRATEGY["fingerprints"][0]
        fingerprint_json = json.dumps(fingerprint, ensure_ascii=False)
        script = """
        const __bumbleFingerprint = __FINGERPRINT__;
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => (__bumbleFingerprint.plugins || []).map((name, index) => ({
                name,
                description: name,
                filename: `plugin-${index}.plugin`
            }))
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => __bumbleFingerprint.languages || ['zh-CN', 'zh', 'en-US', 'en']
        });
        window.chrome = { runtime: {} };
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return __bumbleFingerprint.webgl_vendor || 'Intel Inc.';
            if (parameter === 37446) return __bumbleFingerprint.webgl_renderer || 'Intel Iris OpenGL Engine';
            return getParameter.call(this, parameter);
        };
        delete navigator.__proto__['webdriver'];
        """
        return script.replace("__FINGERPRINT__", fingerprint_json)

    # ── 生命周期 ──────────────────────────────────

    def launch(self):
        """启动浏览器（接入 BrowserManager 单例，不私有启停）"""
        from config import get_config
        from browser_manager import get_browser_manager

        manager = get_browser_manager("bumble", get_config())
        instance = manager.get_instance()
        self.playwright = instance.playwright
        self.context = instance.context
        self.page = instance.page
        self.context.set_default_timeout(20000)
        if not getattr(self.page, "_bumble_stealth_applied", False):
            Stealth().apply_stealth_sync(self.page)      # 复用 Tinder 侧同一 stealth 引擎
            self.page.add_init_script(self._stealth_js())
            setattr(self.page, "_bumble_stealth_applied", True)
        print(f"  浏览器已挂载，Profile: {self.profile_path}")


    def _check_backend_error(self) -> bool:
        """
        视觉熔断机制：检测 Bumble DOM 层异常。
        命中 "Something went wrong" 或重试按钮 → 抛出 BackendError，强制 30min cooldown。
        返回 True 表示检测到异常。
        """
        try:
            body_text = self.page.inner_text("body")
        except Exception:
            return False

        # 1. 模糊匹配错误文本
        error_patterns = [
            "something went wrong",
            "something went wrong.",
            "加载失败",
            "服务器错误",
            "try again",
            "重试",
        ]
        text_lower = body_text.lower()
        for pattern in error_patterns:
            if pattern in text_lower:
                print(f"  [BumbleBot] ⚠️ 后端异常检测: 命中「{pattern}」")
                return True

        # 2. 重试按钮 DOM 检测
        retry_selectors = [
            "[class*='retry']",
            "[class*='Retry']",
            "[class*='error'] button",
            "button:has-text('Try Again')",
            "button:has-text('重试')",
        ]
        for sel in retry_selectors:
            if self.page.locator(sel).count() > 0:
                print(f"  [BumbleBot] ⚠️ 后端异常检测: 重试按钮 present ({sel})")
                return True

        return False


    def close(self):
        """关闭浏览器（已上交 BrowserManager，单例不关闭）"""
        pass

    # ── 工具方法 ──────────────────────────────────

    def wait_for_url_contains(self, fragment: str, timeout: int = 20):
        self.page.wait_for_url(f"**{fragment}**", timeout=timeout * 1000)

    def is_logged_in(self) -> bool:
        """简单登录态判断：当前 URL 不在 auth 页即算已登录"""
        return "/authentication" not in self.page.url

    def scroll_to_bottom(self, selector: str = None, times: int = 5):
        """滚动页面或指定元素到底部"""
        for i in range(times):
            if selector:
                self.page.locator(selector).evaluate("el => el.scrollTop += 500")
            else:
                self.page.evaluate("window.scrollBy(0, 800)")
            time.sleep(1.5)

    # ── 消息提取（严格区域隔离） ───────────────────────

    def extract_messages(self) -> dict:
        """
        分离式提取 + 滚动增量收集（解决虚拟列表截断问题）
        返回格式: {"match_bio": "...", "messages": [{"sender": "me"/"them", "text": "..."}]}
        消息按时间正序（ oldest first）
        """
        import time as _time

        # ── 1. 提取 Bio（不变） ──
        bio_result = self.page.evaluate(r"""
            () => {
                const bioNodes = document.querySelectorAll(
                    'div[class*="profile__section"], div[class*="profile__about"], div[class*="profile__info"]'
                );
                const bioTexts = Array.from(bioNodes)
                    .map(el => el.innerText.trim())
                    .filter(text => text.length > 0);
                return [...new Set(bioTexts)].join(' | ').replace(/\n/g, ' ');
            }
        """)

        # ── 2. 滚动增量收集 ──
        seen_texts = set()
        all_messages = []          # 按收集顺序（倒序，最后会反转）
        no_new_count = 0           # 连续未发现新消息的次数
        last_count = 0

        def _extract_batch():
            """单次抓取当前屏幕可见气泡，返回 [{sender, text}] 列表"""
            return self.page.evaluate(r"""
                () => {
                    const bubbles = document.querySelectorAll('div[class*="message-bubble"]');
                    const results = [];
                    const inferSenderFromDom = (bubble) => {
                        const markerParts = [];
                        let current = bubble;
                        for (let i = 0; i < 6 && current; i++, current = current.parentElement) {
                            markerParts.push(String(current.className || ''));
                            markerParts.push(current.getAttribute('data-qa-role') || '');
                            markerParts.push(current.getAttribute('data-testid') || '');
                            markerParts.push(current.getAttribute('aria-label') || '');
                        }
                        const marker = markerParts.join(' ').toLowerCase();
                        if (/(outgoing|sent|from-me|own-message|is-own|message-bubble--out|--out)/.test(marker)) {
                            return 'me';
                        }
                        if (/(incoming|received|from-them|their-message|is-their|message-bubble--in|--in)/.test(marker)) {
                            return 'them';
                        }
                        return '';
                    };
                    const fallbackSenderFromGeometry = (bubble) => {
                        try {
                            const rect = bubble.getBoundingClientRect();
                            const container = bubble.parentElement?.parentElement || document.body;
                            const containerBox = container.getBoundingClientRect();
                            const outgoingSideStart = containerBox.left + (containerBox.width / 2);
                            return rect.left > outgoingSideStart ? 'me' : 'them';
                        } catch (e) {
                            return 'them';
                        }
                    };
                    const extractBubbleText = (bubble) => {
                        const textEl = bubble.querySelector('div[class*="message-bubble__text"]');
                        let text = ((textEl && textEl.innerText) || bubble.innerText || '').trim();
                        if (text) return text;

                        const attrs = [];
                        bubble.querySelectorAll('[aria-label], img[alt], svg[aria-label], [title]').forEach(el => {
                            ['aria-label', 'alt', 'title'].forEach(name => {
                                const value = (el.getAttribute(name) || '').trim();
                                if (value) attrs.push(value);
                            });
                        });
                        const attrText = attrs.join(' ').trim();
                        if (!attrText) return '';
                        if (/(like|liked|heart|react|reaction|赞|喜欢|红心|爱心)/i.test(attrText)) {
                            return '[liked your message]';
                        }
                        return attrText;
                    };
                    bubbles.forEach(bubble => {
                        const text = extractBubbleText(bubble);
                        if (!text || text.length > 500) return;

                        // 跳过时间戳
                        if (/^\d{1,2}:\d{2}$/.test(text)) return;
                        if (/^\d{4}年/.test(text)) return;

                        // 优先读 DOM 语义标记；坐标只作为最后兜底。
                        const sender = inferSenderFromDom(bubble) || fallbackSenderFromGeometry(bubble);
                        results.push({ sender, text });
                    });
                    return results;
                }
            """)

        for scroll_round in range(20):  # 最多20轮
            batch = _extract_batch()
            new_found = 0

            for item in batch:
                if item['text'] not in seen_texts:
                    seen_texts.add(item['text'])
                    all_messages.append(item)
                    new_found += 1

            print(f"    [滚动 {scroll_round+1}] 本轮 {len(batch)} 条，新增 {new_found} 条，累计 {len(all_messages)}")

            if new_found == 0:
                no_new_count += 1
                if no_new_count >= 3:
                    print(f"    连续3轮无新消息，触顶停止")
                    break
            else:
                no_new_count = 0

            # 向上滚动聊天区域
            self.page.mouse.wheel(0, -2000)
            _time.sleep(1.5)

        # 反转使 oldest first
        all_messages.reverse()

        # 相邻去重（按时间正序）
        deduped = []
        for m in all_messages:
            if not deduped or deduped[-1]['text'] != m['text']:
                deduped.append(m)

        return {
            'match_bio': bio_result,
            'messages': deduped
        }

    def extract_and_separate(self) -> tuple:
        """返回 (messages_list, profile_bio_str)"""
        result = self.extract_messages()
        return result.get('messages', []), result.get('match_bio', '')

    def extract_messages_from_chat_panel(self) -> dict:
        """
        只从 page--chat 主内容区的聊天面板提取消息，不含 sidebar 预览。
        DOM 结构:
          page--chat
            └─ page__layout (index 0)
                  ├─ sidebar / contact-tabs (左边栏，显示对话列表)
                  └─ conversation panel (右边栏，实际聊天内容)
        返回: {"match_bio": "...", "messages": [{"sender": "me"/"them", "text": "..."}]}
        """
        import time as _time
        _time.sleep(3)  # 等待通知面板关闭

        result = self.page.evaluate(r"""
        () => {
            // page--chat 是最顶层容器
            const pageChat = document.querySelector('.page--chat');
            if (!pageChat) return { match_bio: '', messages: [], error: 'no page--chat' };

            // page__layout 是第一个子元素
            const layout = pageChat.children[0];
            if (!layout) return { match_bio: '', messages: [], error: 'no layout' };

            // 在 layout 下找 conversation panel（排除 sidebar/contact-tabs）
            let chatPanel = null;
            for (const child of layout.children) {
                const cls = child.className || '';
                if (cls.includes('sidebar') || cls.includes('contact-tabs') || cls.includes('request-panel')) continue;
                if (child.querySelector('[class*="bubble"]')) {
                    chatPanel = child;
                    break;
                }
            }

            // 兜底：直接用 layout（如果它自己有气泡）
            if (!chatPanel && layout.querySelector('[class*="bubble"]')) {
                chatPanel = layout;
            }

            if (!chatPanel) {
                return { match_bio: '', messages: [], error: 'no chat panel' };
            }

            // 收集气泡
            const bubbles = Array.from(chatPanel.querySelectorAll('[class*="bubble"]'));
            const seen = new Set();
            const messages = [];
            const inferSenderFromDom = (bubble) => {
                const markerParts = [];
                let current = bubble;
                for (let i = 0; i < 6 && current; i++, current = current.parentElement) {
                    markerParts.push(String(current.className || ''));
                    markerParts.push(current.getAttribute('data-qa-role') || '');
                    markerParts.push(current.getAttribute('data-testid') || '');
                    markerParts.push(current.getAttribute('aria-label') || '');
                }
                const marker = markerParts.join(' ').toLowerCase();
                if (/(outgoing|sent|from-me|own-message|is-own|message-bubble--out|--out)/.test(marker)) {
                    return 'me';
                }
                if (/(incoming|received|from-them|their-message|is-their|message-bubble--in|--in)/.test(marker)) {
                    return 'them';
                }
                return '';
            };
            const fallbackSenderFromGeometry = (bubble) => {
                const rect = bubble.getBoundingClientRect();
                return rect.left > window.innerWidth / 2 ? 'me' : 'them';
            };
            const extractBubbleText = (bubble) => {
                let text = (bubble.innerText || '').trim();
                if (text) return text;

                const attrs = [];
                bubble.querySelectorAll('[aria-label], img[alt], svg[aria-label], [title]').forEach(el => {
                    ['aria-label', 'alt', 'title'].forEach(name => {
                        const value = (el.getAttribute(name) || '').trim();
                        if (value) attrs.push(value);
                    });
                });
                const attrText = attrs.join(' ').trim();
                if (!attrText) return '';
                if (/(like|liked|heart|react|reaction|赞|喜欢|红心|爱心)/i.test(attrText)) {
                    return '[liked your message]';
                }
                return attrText;
            };

            bubbles.forEach(b => {
                const text = extractBubbleText(b);
                if (!text || text.length > 500) return;
                if (/^\d{1,2}:\d{2}$/.test(text)) return;
                if (seen.has(text)) return;
                seen.add(text);

                const sender = inferSenderFromDom(b) || fallbackSenderFromGeometry(b);
                const is_mine = sender === 'me';

                messages.push({ sender: is_mine ? 'me' : 'them', text, is_mine });
            });

            // 读对方名字
            const nameEl = chatPanel.querySelector('[class*="profileName"]') ||
                           chatPanel.querySelector('.contact__name');
            const match_bio = nameEl ? nameEl.innerText.trim().replace(/\n/g, ' | ') : '';

            return { match_bio, messages, bubbleCount: bubbles.length };
        }
        """)

        if result.get('error'):
            print(f"    [提取警告] {result['error']}, bubbleCount={result.get('bubbleCount', 0)}")

        return result

    def send_message(self, text: str, messages: list[dict] | None = None) -> bool:
        """统一走 shared_assets.unified_send_message，避免平台实现漂移"""
        text = sanitize_reply_for_send(text, max_len=50, messages=messages)
        return send_message_unified(
            page=self.page,
            message=text,
            platform="bumble",
            message_context=messages,
        )

    def click_conversation(self, name: str = None, your_move_only: bool = False):
        """
        点击侧边栏中的对话条目
        name: 精确匹配对话名称
        your_move_only: True = 只点击"轮到您了"条目（物理坐标点击）
        """
        if your_move_only:
            box = self.page.evaluate(r"""
                () => {
                    const nodes = Array.from(document.querySelectorAll('*')).filter(el => {
                        const text = el.innerText || '';
                        return text.includes('轮到您了') || text.includes('Your Move');
                    });
                    if (nodes.length > 0) {
                        const rect = nodes[0].getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
                        }
                    }
                    return null;
                }
            """)
            if box:
                self.page.mouse.click(box['x'], box['y'])
                time.sleep(3)
                print(f"  ✅ 进入 Your Move 对话: {self.page.url}")
                return True
            return False

        if name:
            for sel in [
                f"[class*='sidebarItem']:has-text('{name}')",
                f"[class*='conversationItem']:has-text('{name}')",
            ]:
                if self.page.locator(sel).count():
                    self.page.locator(sel).first.click(force=True)
                    time.sleep(3)
                    return True

        # 兜底：点击第一个对话
        for sel in ["[class*='sidebarItem']", "[class*='conversationItem']"]:
            count = self.page.locator(sel).count()
            if count:
                self.page.locator(sel).first.click(force=True)
                time.sleep(3)
                print(f"  ✅ 进入对话: {self.page.url}")
                return True

        return False

    def click_new_match(self) -> bool:
        """
        物理坐标级点击：彻底绕过 Bumble 遮罩层与 SPA 路由拦截
        """
        print("  [BumbleBot] 探测顶部新配对名单...")
        try:
            box = self.page.evaluate(r"""
                () => {
                    const headers = Array.from(document.querySelectorAll('*')).filter(el =>
                        ['配对名单', 'Match Queue', '配对'].some(t => el.innerText && el.innerText.includes(t)) && el.innerText.length < 20
                    );
                    if (!headers.length) return null;

                    let current = headers[headers.length - 1];
                    let listContainer = null;

                    for(let i=0; i<5; i++) {
                        if (current.nextElementSibling) {
                            listContainer = current.nextElementSibling;
                            break;
                        }
                        current = current.parentElement;
                    }
                    if (!listContainer) return null;

                    const imgs = Array.from(listContainer.querySelectorAll('img'));
                    for (const img of imgs) {
                        const wrapper = img.closest('a') || img.closest('[role="button"]') || img;
                        const text = wrapper.innerText || '';

                        if (/^\d+$/.test(text.trim()) || text.includes('Beeline')) continue;

                        const rect = wrapper.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return {
                                x: rect.left + rect.width / 2,
                                y: rect.top + rect.height / 2
                            };
                        }
                    }
                    return null;
                }
            """)

            if box:
                self.page.mouse.click(box['x'], box['y'])
                time.sleep(3)
                return True

        except Exception as e:
            print(f"  [BumbleBot] 新配对物理点击异常: {e}")

        return False

    def auto_like(self, max_actions: int = 20, max_likes: int = 5) -> dict:
        """
        主页自动滑动（双轨：右滑喜欢 + 左滑稀释）
        返回状态字典，供守护进程做24小时退避决策
        """
        self.page.goto("https://bumble.com/app", timeout=20000)
        time.sleep(5)

        like_count = 0
        action_count = 0
        out_of_likes = False

        for i in range(max_actions):
            # 每日喜欢上限阻断
            if like_count >= max_likes:
                print(f"  喜欢次数已达上限 ({max_likes})，退出")
                break

            # 等待卡片加载
            try:
                self.page.wait_for_selector("figure img, [class*='card'] img", timeout=5000)
            except Exception:
                print(f"  [{i+1}] 卡片未加载，终止")
                break

            # 读取卡片地域信息
            try:
                card_text = self.page.locator("main").inner_text(timeout=2000)
            except Exception:
                card_text = ""

            auto_like_keywords = [
                str(keyword).strip()
                for keyword in self.strategy.get("auto_like_keywords", [])
                if str(keyword).strip()
            ]
            like_probability = self.strategy.get("auto_like_probability", 0.3)
            try:
                like_probability = float(like_probability)
            except Exception:
                like_probability = 0.3
            like_probability = max(0.0, min(1.0, like_probability))

            matched_keyword = next(
                (keyword for keyword in auto_like_keywords if keyword in card_text),
                "",
            )
            if matched_keyword:
                is_like = True
                print(f"  [{i+1}] 命中策略关键字「{matched_keyword}」，强制喜欢")
            else:
                is_like = random.random() < like_probability

            try:
                if is_like:
                    self.page.keyboard.press("ArrowRight")
                    like_count += 1
                    print(f"  [{i+1}/{max_actions}] ✅ 喜欢 (累计{like_count})")
                else:
                    self.page.keyboard.press("ArrowLeft")
                    print(f"  [{i+1}/{max_actions}] ⬅️ 左滑")
                action_count += 1
            except Exception as e:
                print(f"  [{i+1}] 按键失败: {e}")
                continue

            time.sleep(random.uniform(1.5, 3.5))

            # 弹窗处理
            popup_selectors = [
                "text=继续玩Bumble",
                "text=Keep swiping",
                "text=Continue playing",
                "text=继续聊天",
                "text=继续滑动",
            ]
            popup_closed = False
            for psel in popup_selectors:
                try:
                    btn = self.page.locator(psel).first
                    if btn.is_visible(timeout=2000):
                        btn.click(force=True)
                        print(f"  弹窗已关闭")
                        time.sleep(1.5)
                        popup_closed = True
                        break
                except Exception:
                    pass

            # 付费墙检测（仅在右滑后才检测，左滑不限次不会触发）
            if is_like:
                paywall_selectors = [
                    "text=喜欢次数已用完",
                    "text=You're out of likes",
                    "text=Out of votes",
                    "text=You're out of swipes",
                    "div[role='dialog']:has-text('Premium')",
                    "div[role='dialog']:has-text('Upgrade')",
                    "text=明日再来",
                ]
                for psel in paywall_selectors:
                    try:
                        el = self.page.locator(psel).first
                        if el.count() > 0 and el.is_visible(timeout=2000):
                            print(f"  付费墙检测，退出")
                            out_of_likes = True
                            return {
                                "actions": action_count,
                                "likes": like_count,
                                "out_of_likes": out_of_likes,
                            }
                    except Exception:
                        pass

        return {
            "actions": action_count,
            "likes": like_count,
            "out_of_likes": out_of_likes,
        }
