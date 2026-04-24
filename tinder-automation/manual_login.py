#!/usr/bin/env python3
"""
manual_login.py - Tinder 手动登录辅助脚本
浏览器配置与 tinder_bot.py 完全对齐，用于刷新持久化的 Session 凭证。
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from project_config import DEFAULT_PROFILE_DIR, build_browser_launch_options

from playwright.sync_api import sync_playwright
from playwright_stealth.stealth import Stealth

PROFILE_PATH = os.getenv("TINDER_PROFILE_DIR", str(DEFAULT_PROFILE_DIR))
PROFILE_DIR = Path(PROFILE_PATH)
STALE_LOCK_PATTERNS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def cleanup_stale_profile_locks(profile_dir: Path) -> list[Path]:
    removed: list[Path] = []
    profile_dir.mkdir(parents=True, exist_ok=True)
    for pattern in STALE_LOCK_PATTERNS:
        for path in profile_dir.glob(pattern):
            try:
                path.unlink(missing_ok=True)
                removed.append(path)
            except OSError as exc:
                print(f"[Warn] 无法删除锁文件 {path}: {exc}")
    return removed


def page_text(page) -> str:
    try:
        body = page.inner_text("body") or ""
    except Exception:
        return ""
    return re.sub(r"\s+", " ", body).strip()


def is_authenticated_page(page) -> bool:
    url = page.url or ""
    text = page_text(page)
    app_paths = ("/app/recs", "/app/matches", "/app/messages", "/app/connections")
    login_markers = ("登录", "登入", "继续使用手机号登录", "Log in", "Sign in")
    invalid_markers = (
        "Javascript is Disabled",
        "JavaScript is Disabled",
        "enable javascript",
        "please enable javascript",
        "Something went wrong",
        "Oops",
    )
    if "tinder.com" not in url:
        return False
    if any(marker in text for marker in login_markers):
        return False
    if any(marker.lower() in text.lower() for marker in invalid_markers):
        return False
    if not text:
        return False
    return any(path in url for path in app_paths)


def wait_for_manual_login(page, timeout_seconds: int = 180) -> bool:
    print("\n>>> 等待登录完成（扫码后请在手机上确认）...")
    for attempt in range(1, timeout_seconds + 1):
        if is_authenticated_page(page):
            print(f"\n[Detected] 已进入登录后页面: {page.url}")
            return True
        if attempt % 10 == 0:
            print(f"    等待中... ({attempt}s) 当前 URL: {page.url}")
        time.sleep(1)
    return False


def verify_session(page) -> bool:
    check_urls = [
        "https://tinder.com/app/connections",
        "https://tinder.com/app/matches",
        "https://tinder.com/app/recs",
    ]
    for url in check_urls:
        try:
            page.goto(url, timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            time.sleep(2)
            text = page_text(page)
            print(f"[Verify] {url} -> {page.url}")
            if is_authenticated_page(page):
                preview = text[:120] if text else "(空白)"
                print(f"[Verify] 页面摘要: {preview}")
                return True
        except Exception as exc:
            print(f"[Warn] 访问 {url} 失败: {exc}")
    return False


removed_locks = cleanup_stale_profile_locks(PROFILE_DIR)

print("=" * 60)
print("Tinder 手动登录 — 请在弹出的浏览器中完成操作")
print("=" * 60)
print(f"[Profile] {PROFILE_DIR}")
if removed_locks:
    print(f"[Clean] 已移除 {len(removed_locks)} 个残留锁文件")
else:
    print("[Clean] 未发现残留 Singleton 锁文件")

pw = sync_playwright().start()
context = None

try:
    launch_options = build_browser_launch_options(headless=False)
    context = pw.chromium.launch_persistent_context(
        PROFILE_PATH,
        **launch_options,
    )

    page = context.pages[0] if context.pages else context.new_page()

    # ── Stealth 注入（与 tinder_bot.py 完全一致）───────────────
    Stealth().apply_stealth_sync(page)
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});"
    )

    # ── 导航到 Tinder（进入配对/发现页，触发完整 SPA 加载）────
    page.goto("https://tinder.com/app/recs", timeout=30000)
    page.wait_for_load_state("domcontentloaded")
    time.sleep(3)

    print(f"\n[URL]  {page.url}")
    print(f"[UA]   {launch_options['user_agent']}")
    print()

    login_confirmed = wait_for_manual_login(page)
    if not login_confirmed:
        print("\n[Timeout] 未检测到登录完成，请手动按 Enter 继续...")
        input("    确认后按 Enter")

    # ── 验证登录状态 ────────────────────────────────────────────
    cookies = context.cookies()
    g = next((c for c in cookies if c["name"] == "g_state"), None)
    print(f"\n[Cookie] 共 {len(cookies)} 条")
    if g:
        import re
        m = re.search(r'"i_l"\s*:\s*(\d+)', g["value"])
        i_l = m.group(1) if m else "?"
        print(f"[g_state.i_l] = {i_l}  {'✅ 已登录' if i_l == '1' else '⚠️ 可能未登录'}")
    else:
        print("[g_state] ⚠️ 未找到 g_state Cookie，可能未登录")

    print(f"[URL] 最终页面: {page.url}")
    verified = verify_session(page)
    print(f"[Session] {'✅ 已验证可访问登录后页面' if verified else '⚠️ 仍未稳定验证登录态'}")

finally:
    # ── 安全固化 ────────────────────────────────────────────────
    if context is not None:
        context.close()
    pw.stop()
    print("\n[Done] 浏览器已关闭，Cookie 和 LocalStorage 已固化到 Profile 目录。")
