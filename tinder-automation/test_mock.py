#!/usr/bin/env python3
"""
Tinder Bot Mock 测试 - 简化版
直接使用 Playwright 无需 Stealth
"""
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.tinder_bot import TinderBot, CONFIG
from playwright.sync_api import sync_playwright

# Mock URL
MOCK_URL = "http://localhost:8888"

# 测试配置
TEST_CONFIG = {
    **CONFIG,
    "tinder_url": MOCK_URL,
    "account_id": "test_mock",
}

MOCK_HTML = """
<!DOCTYPE html>
<html><head><title>Mock Tinder</title></head>
<body>
    <h1>Mock Tinder - 测试环境</h1>
    <div id="matches">
        <div class="match-card" onclick="openChat('user1')">
            <div class="unread-dot"></div>
            <div><strong>用户A</strong></div>
        </div>
    </div>
    <div id="chat-view" style="display:none">
        <button onclick="back()">← 返回</button>
        <h2 id="chat-name">用户</h2>
        <div id="messages"></div>
        <textarea id="input-box" placeholder="输入消息..."></textarea>
        <button id="send-btn" onclick="sendMessage()">发送</button>
    </div>
    <script>
    const MESSAGES = [
        {sender: 'them', text: '你好呀'},
        {sender: 'them', text: '最近在忙什么'}
    ];
    let current = [];
    
    function openChat(id) {
        document.getElementById('matches').style.display = 'none';
        document.getElementById('chat-view').style.display = 'block';
        current = [...MESSAGES];
        renderMessages();
    }
    
    function renderMessages() {
        const container = document.getElementById('messages');
        container.innerHTML = '';
        current.forEach(msg => {
            const div = document.createElement('div');
            div.className = 'chat-bubble ' + (msg.sender === 'me' ? 'bubble-sent' : 'bubble-received');
            div.textContent = msg.text;
            container.appendChild(div);
        });
    }
    
    function sendMessage() {
        const input = document.getElementById('input-box');
        const text = input.value.trim();
        if (!text) return;
        current.push({sender: 'me', text});
        renderMessages();
        input.value = '';
        setTimeout(() => {
            current.push({sender: 'them', text: '好的'});
            renderMessages();
        }, 500);
    }
    
    function back() {
        document.getElementById('matches').style.display = 'block';
        document.getElementById('chat-view').style.display = 'none';
    }
    </script>
    <style>
    .match-card { border: 1px solid #ddd; padding: 15px; margin: 10px 0; cursor: pointer; }
    .match-card:hover { background: #f5f5f5; }
    .unread-dot { width: 10px; height: 10px; background: red; border-radius: 50%; display: inline-block; margin-right: 10px; }
    .bubble-sent { background: #0084ff; color: white; padding: 8px 12px; border-radius: 15px; margin: 5px; max-width: 70%; }
    .bubble-received { background: #f1f0f0; padding: 8px 12px; border-radius: 15px; margin: 5px; max-width: 70%; }
    #input-box { width: 90%; padding: 10px; margin-top: 10px; }
    #send-btn { background: #fe3c44; color: white; border: none; padding: 10px 20px; cursor: pointer; }
    </style>
</body></html>
"""

def start_mock_server(port=8888):
    """启动Mock服务器"""
    import http.server
    import socketserver
    
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(MOCK_HTML.encode())
            else:
                self.send_error(404)
        
        def log_message(self, fmt, *args):
            pass
    
    with socketserver.TCPServer(("", port), Handler) as httpd:
        print(f"🌐 Mock 服务器: http://localhost:{port}")
        httpd.handle_request()
        httpd.handle_request()

def test_llm():
    """测试 LLM 回复生成"""
    print("\n[3] 测试 LLM 回复生成...")
    try:
        bot = TinderBot(TEST_CONFIG)
        
        messages = [
            {"text": "你好呀", "is_mine": False, "sender": "them"},
            {"text": "最近在忙什么", "is_mine": False, "sender": "them"},
        ]
        
        reply = bot.generate_reply(messages)
        print(f"    ✅ LLM 生成: {reply}")
        
        if len(reply) > 30:
            print(f"    ⚠️ 回复超过30字")
        else:
            print(f"    ✅ 字数符合要求 ({len(reply)}/30)")
        
        return reply
        
    except Exception as e:
        print(f"    ❌ LLM 失败: {e}")
        return None

def test_browser_flow(reply):
    """测试浏览器流程"""
    print("\n[1] 测试浏览器启动...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(MOCK_URL, timeout=10000)
            print(f"    ✅ 浏览器启动成功: {page.title()}")
            
            print("\n[2] 测试 DOM 选择器...")
            page.wait_for_selector(".match-card", timeout=5000)
            print("    ✅ .match-card 选择器有效")
            
            page.click(".match-card")
            page.wait_for_selector("#chat-view", timeout=3000)
            print("    ✅ #chat-view 选择器有效")
            
            if reply:
                print(f"\n[4] 测试输入和发送 ({reply})...")
                page.fill("#input-box", reply)
                print(f"    ✅ 输入成功")
                
                page.click("#send-btn")
                time.sleep(1)
                print("    ✅ 发送成功")
                
                print("\n[5] 验证消息...")
                time.sleep(2)
                bubbles = page.query_selector_all(".bubble-sent")
                if bubbles:
                    last_text = bubbles[-1].inner_text()
                    print(f"    发送的消息: {last_text}")
                    if reply in last_text:
                        print("    ✅ 验证成功")
                    else:
                        print("    ⚠️ 消息内容不匹配")
            
            browser.close()
            return True
            
    except Exception as e:
        print(f"    ❌ 浏览器测试失败: {e}")
        return False

def main():
    print("=" * 50)
    print("Tinder Bot Mock 测试")
    print("=" * 50)
    
    # 启动Mock服务器
    server_thread = threading.Thread(target=start_mock_server, daemon=True)
    server_thread.start()
    time.sleep(0.5)
    
    # 测试 LLM
    reply = test_llm()
    
    # 测试浏览器
    test_browser_flow(reply)
    
    print("\n" + "=" * 50)
    print("测试完成")
    print("=" * 50)

if __name__ == "__main__":
    main()
