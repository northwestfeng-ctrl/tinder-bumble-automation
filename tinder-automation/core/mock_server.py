#!/usr/bin/env python3
"""
Mock Tinder 服务器 - 用于测试自动化流程
模拟 Tinder 的 DOM 结构和交互
"""
import http.server
import socketserver
import json
import random
from pathlib import Path
from urllib.parse import parse_qs

PORT = 8888

# Mock 对话数据
MOCK_MESSAGES = [
    {"sender": "them", "text": "你好呀"},
    {"sender": "them", "text": "最近在忙什么"},
    {"sender": "me", "text": "在上班"},
    {"sender": "them", "text": "哈哈，我也是"},
]

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Mock Tinder</title>
    <style>
        body { font-family: Arial; max-width: 400px; margin: 50px auto; padding: 20px; }
        .match-card { 
            border: 1px solid #ddd; 
            padding: 15px; 
            margin: 10px 0; 
            cursor: pointer;
            display: flex;
            align-items: center;
        }
        .match-card:hover { background: #f5f5f5; }
        .unread-dot {
            width: 10px; height: 10px;
            background: red; border-radius: 50%;
            margin-right: 10px;
        }
        .chat-bubble {
            padding: 10px 15px;
            margin: 5px 0;
            border-radius: 15px;
            max-width: 70%;
        }
        .bubble-sent {
            background: #e一来:00e;
            color: white;
            margin-left: auto;
        }
        .bubble-received {
            background: #f1f0f0;
        }
        #input-box {
            width: 100%;
            padding: 15px;
            border: 1px solid #ddd;
            border-radius: 25px;
            margin-top: 20px;
        }
        #send-btn {
            background: #fe3c44;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 20px;
            cursor: pointer;
            margin-top: 10px;
        }
        #send-btn:hover { background: #e02535; }
    </style>
</head>
<body>
    <h1>Mock Tinder</h1>
    <div id="matches">
        <div class="match-card" onclick="openChat('match1')">
            <div class="unread-dot"></div>
            <div>
                <strong>用户A</strong><br>
                <small>最后消息...</small>
            </div>
        </div>
        <div class="match-card" onclick="openChat('match2')">
            <div class="unread-dot"></div>
            <div>
                <strong>用户B</strong><br>
                <small>最后消息...</small>
            </div>
        </div>
    </div>

    <div id="chat-view" style="display:none;">
        <button onclick="back()">← 返回</button>
        <h2 id="chat-name">用户A</h2>
        <div id="messages"></div>
        <textarea id="input-box" placeholder="Type a message..."></textarea>
        <button id="send-btn" onclick="sendMessage()">Send</button>
    </div>

    <script>
        let currentMessages = [];
        
        function openChat(matchId) {
            document.getElementById('matches').style.display = 'none';
            document.getElementById('chat-view').style.display = 'block';
            document.getElementById('chat-name').textContent = matchId === 'match1' ? '用户A' : '用户B';
            
            // 加载模拟消息
            currentMessages = """ + json.dumps(MOCK_MESSAGES) + """;
            renderMessages();
        }
        
        function renderMessages() {
            const container = document.getElementById('messages');
            container.innerHTML = '';
            currentMessages.forEach(msg => {
                const div = document.createElement('div');
                div.className = 'chat-bubble ' + (msg.sender === 'me' ? 'bubble-sent' : 'bubble-received');
                div.textContent = msg.text;
                container.appendChild(div);
            });
            container.scrollTop = container.scrollHeight;
        }
        
        function sendMessage() {
            const input = document.getElementById('input-box');
            const text = input.value.trim();
            if (!text) return;
            
            // 添加到本地
            currentMessages.push({sender: 'me', text: text});
            renderMessages();
            
            // 模拟对方回复
            setTimeout(() => {
                const replies = ['好的', '哈哈', '真的吗', '嗯嗯', '有意思'];
                currentMessages.push({
                    sender: 'them', 
                    text: replies[Math.floor(Math.random() * replies.length)]
                });
                renderMessages();
            }, 1000);
            
            input.value = '';
        }
        
        function back() {
            document.getElementById('matches').style.display = 'block';
            document.getElementById('chat-view').style.display = 'none';
        }
    </script>
</body>
</html>
"""

class MockTinderHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        else:
            super().do_GET()
    
    def log_message(self, format, *args):
        print(f"[Mock] {format % args}")

def run_server(port=PORT):
    with socketserver.TCPServer(("", port), MockTinderHandler) as httpd:
        print(f"✅ Mock Tinder 服务器运行中: http://localhost:{port}")
        print("按 Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止")

if __name__ == "__main__":
    run_server()
