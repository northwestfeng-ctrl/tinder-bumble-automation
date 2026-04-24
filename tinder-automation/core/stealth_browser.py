#!/usr/bin/env python3
"""
Tinder 自动化 - 模块1: 浏览器指纹混淆
功能：
- 隐藏无头浏览器特征
- WebGL/Canvas 指纹随机化
- 注入 Stealth 插件
"""
import asyncio
import random
from playwright.sync_api import sync_playwright, Browser, BrowserContext

# ============ 指纹混淆配置 ============

STEALTH_ARGS = [
    # 核心反检测
    "--disable-blink-features=AutomationControlled",
    "--disable-automation-detection",
    "--no-first-run",
    "--disable-crash-reporter",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    
    # 屏幕分辨率
    "--window-size=1280,800",
    
    # 禁用提示栏
    "--disable-infobars",
    "--disable-extensions",
    
    # WebGL 混淆
    "--enable-webgl",
    "--use-gl=swiftshader",
    
    # 语言和时区
    "--lang=ja-JP",
    "--timezone=Asia/Tokyo",
]

# WebGL 随机化厂商/渲染器
FAKE_WEBGL_VENDORS = [
    "Intel Inc.",
    "NVIDIA Corporation",
    "AMD",
    "Apple M1",
]

FAKE_WEBGL_RENDERERS = [
    "Intel Iris OpenGL Engine",
    "NVIDIA GeForce GTX 1060",
    "AMD Radeon Pro 5500M",
    "Apple M1 GPU",
]

# Canvas 噪声种子
CANVAS_NOISE_SEED = random.randint(1000000, 9999999)


def get_stealth_context_args():
    """生成随机化上下文参数"""
    return {
        "viewport": {
            "width": random.randint(1200, 1400),
            "height": random.randint(800, 900),
            "device_scale_factor": random.choice([1, 1.25, 1.5, 2]),
        },
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
        "geolocation": {"longitude": 139.6917, "latitude": 35.6895},  # 东京
        "permissions": ["geolocation"],
        "user_agent": get_random_user_agent(),
        "extra_http_headers": {
            "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        },
    }


def get_random_user_agent():
    """随机生成 User-Agent"""
    chrome_versions = [
        "124.0.6367.78",
        "123.0.6312.86", 
        "122.0.6261.124",
        "121.0.6163.85",
    ]
    return (
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{random.choice(chrome_versions)} "
        f"Safari/537.36"
    )


async def inject_stealth_script(page):
    """向页面注入反检测脚本"""
    stealth_script = f"""
    // 1. 抹除 navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {{
        get: () => undefined,
        configurable: true
    }});
    
    // 2. 伪造 WebGL 厂商
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {{
        if (parameter === 37445) {{
            return "{random.choice(FAKE_WEBGL_VENDORS)}";
        }}
        if (parameter === 37446) {{
            return "{random.choice(FAKE_WEBGL_RENDERERS)}";
        }}
        return getParameter.apply(this, arguments);
    }};
    
    // 3. 伪造 Canvas 指纹
    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    
    // 4. 抹除 Chrome runtime
    window.chrome = {{ runtime: {{}}, app: {{}} }};
    
    // 5. 伪造 permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({{ state: Notification.permission }}) :
            originalQuery(parameters)
    );
    
    // 6. 伪造 plugins
    Object.defineProperty(navigator, 'plugins', {{
        get: () => [
            {{
                name: 'Chrome PDF Plugin',
                description: 'Portable Document Format',
                filename: 'internal-pdf-viewer'
            }},
            {{
                name: 'Chrome PDF Viewer',
                description: '',
                filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'
            }},
            {{
                name: 'Native Client',
                description: '',
                filename: 'internal-nacl-plugin'
            }}
        ],
        configurable: true
    }});
    
    // 7. 伪造 languages
    Object.defineProperty(navigator, 'languages', {{
        get: () => ['ja-JP', 'ja', 'en-US', 'en'],
        configurable: true
    }});
    
    console.log('[Stealth] 已注入反检测脚本');
    """
    await page.evaluate(stealth_script)


def create_stealth_browser() -> tuple:
    """
    创建反检测浏览器
    返回: (browser, context, page)
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=STEALTH_ARGS,
        ignore_default_args=["--enable-automation"],
    )
    
    context_args = get_stealth_context_args()
    context = browser.new_context(**context_args)
    
    # 注入 Stealth 脚本
    page = context.new_page()
    asyncio.get_event_loop().run_until_complete(inject_stealth_script(page))
    
    return playwright, browser, context, page


def close_stealth_browser(playwright, browser):
    """安全关闭浏览器"""
    if browser:
        browser.close()
    if playwright:
        playwright.stop()


class StealthBrowser:
    """反检测浏览器封装类"""

    def __init__(self, proxy: str = None):
        self.proxy = proxy
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def setup(self):
        """初始化浏览器，返回 (playwright, context, page)"""
        playwright = sync_playwright().start()
        self.playwright = playwright

        launch_args = STEALTH_ARGS.copy()

        browser = playwright.chromium.launch(
            headless=True,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
        )
        self.browser = browser

        context_args = get_stealth_context_args()
        if self.proxy:
            context_args["proxy"] = {"server": self.proxy}

        self.context = browser.new_context(**context_args)
        self.page = self.context.new_page()

        asyncio.get_event_loop().run_until_complete(inject_stealth_script(self.page))

        return self.playwright, self.context, self.page

    def cleanup(self):
        """关闭浏览器"""
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def __enter__(self):
        self.setup()
        return self.page

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


if __name__ == "__main__":
    print("=== Stealth Browser 测试 ===")
    with StealthBrowser() as page:
        page.goto("https://www.tinder.com")
        print(f"页面标题: {page.title()}")
        print("Stealth 浏览器测试通过")
