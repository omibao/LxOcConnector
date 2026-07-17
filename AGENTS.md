# AGENTS.md

## 项目概述
蓝信 ↔ opencode 桥接服务。Python 实现，将蓝信个人机器人通过 WebSocket 长连接接入本机 opencode serve。

## 代码结构
- `main.py` — 入口
- `config.py` — .env 配置加载
- `lansenger_inbound.py` — 蓝信 WebSocket 入站监听
- `opencode_client.py` — opencode server HTTP 客户端
- `bridge.py` — 桥接逻辑（消息路由 + 会话管理）
- `requirements.txt` — 依赖（websockets, httpx, lansenger-sdk）

## 运行命令
```powershell
pip install -r requirements.txt
python main.py
```

## 提交与推送（重要）
代码仓库：https://github.com/omibao/LxOcConnector.git

由于本机网络环境 SSL 证书拦截，推送时必须禁用 SSL 验证：
```powershell
$env:GIT_SSL_NO_VERIFY="1"
git -c http.sslVerify=false push origin main
```

## 安全规则
- `.env` 含真实凭证，已被 `.gitignore` 排除，**永不提交**
- 提交前务必确认 `git ls-files --cached` 中不含 `.env` 或 `*.log`
- 不要在代码中硬编码任何 App ID / Secret / 密码 / API Key

## 环境
- Python 3.14.2
- opencode CLI 1.18.3（通过 `npm install -g opencode-ai` 安装）
- opencode serve 运行在 `http://localhost:4096`，密码见 `.env`
- 蓝信私有部署网关：`https://apigw.lx.qianxin.com`
