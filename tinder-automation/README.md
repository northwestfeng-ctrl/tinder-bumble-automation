# Tinder 自动化项目

## 项目结构

```text
tinder-automation/
├── core/
│   ├── tinder_bot.py          # 主 Bot：浏览器、巡检、读对话、发消息
│   ├── lifecycle_guard.py     # 频控、熔断、会话状态
│   ├── human_behavior.py      # 人类化节奏与轨迹
│   ├── cdp_events.py          # 点击/输入/滚动模拟
│   ├── network_isolation.py   # 代理与网络环境
│   └── strategy_loader.py     # 加载 strategy_config.json
├── run_watcher.py             # 常驻巡检主入口
├── run_auto.py                # 单次真实环境测试
├── run_tinder_check.py        # 独立巡检入口
├── manual_login.py            # 登录态固化工具
├── project_config.py          # 本地配置与浏览器参数
├── strategy_config.json       # 回复策略与 few-shot 示例
└── logs/                      # 巡检日志
```

## 主流程

1. `manual_login.py` 打开持久化浏览器，手动完成登录。
2. 登录态保存到 `~/.tinder-automation/browser-profile`。
3. `run_watcher.py` 启动后循环执行：
   - 软刷新 Tinder SPA
   - 进入消息列表
   - 扫描对话并判断是否存在新消息
   - 生成回复并发送
   - 写入日志与语料记录
   - 动态退避休眠

## 运行方式

首次登录：

```bash
cd /Users/chengang/.openclaw/workspace/projects/tinder-automation
python3 manual_login.py
```

启动常驻巡检：

```bash
cd /Users/chengang/.openclaw/workspace/projects/tinder-automation
./start.sh
```

单次测试：

```bash
cd /Users/chengang/.openclaw/workspace/projects/tinder-automation
python3 run_auto.py
```

## 配置

项目不再依赖源码中的硬编码密钥。运行前可在本目录创建 `.env`，或使用环境变量注入：

```bash
cp .env.example .env
```

关键变量：

- `UNIFIED_LLM_API_KEY` 或 `TINDER_LLM_API_KEY`
- `TINDER_TELEGRAM_BOT_TOKEN`
- `TINDER_TELEGRAM_CHAT_ID`
- `TINDER_BROWSER_HEADLESS`
- `TINDER_PROFILE_DIR`

`project_config.py` 会优先读取 `shared_assets/.env`，其次读取本项目 `.env`。

## 当前优化点

- 统一了浏览器启动参数，减少 `manual_login.py` 与 `core/tinder_bot.py` 漂移。
- 主 watcher 改为调用统一巡检链路，避免旧接口返回值不匹配。
- 配置改为环境变量优先，源码里不再内置敏感凭据。

## 风险提示

⚠️ Tinder 禁止自动化账户  
⚠️ 使用风险自负，建议仅在测试账号上验证
