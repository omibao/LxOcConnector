# 蓝信 ↔ opencode 桥接服务

把蓝信（Lansenger）个人机器人接入本机的 [opencode](https://opencode.ai)，让你在蓝信里和 opencode AI 助手对话。

```
蓝信云  ←(WebSocket长连)←  本桥接进程  ←(HTTP)→  opencode serve(本机)
        本机主动连出，无需公网IP/域名/回调
```

## 原理

- **蓝信侧（收消息）**：个人机器人支持 WebSocket 长连接入站。本机主动连出去，**不需要公网 IP、不需要备案域名、不用内网穿透**。
- **opencode 侧（AI 后端）**：调用本机 `opencode serve` 的 HTTP API（创建会话、发 prompt、拿回复）。
- **出站（发回复）**：用 [`lansenger-sdk`](https://pypi.org/project/lansenger-sdk/) 把 AI 回复发回蓝信。

## 前置条件

1. **Python 3.10+**（已在 Python 3.14 验证）
2. **本机已安装并运行 opencode**
   - 确认 `opencode` CLI 可用：`opencode --version`
   - 启动服务端：`opencode serve`（默认监听 `http://localhost:4096`）
   - 如需密码保护：`OPENCODE_SERVER_PASSWORD=xxx opencode serve`
3. **蓝信个人机器人凭证**
   - 蓝信桌面端 → 通讯录 → 智能机器人 → 个人机器人 → 点击右侧 ℹ️ 图标
   - （移动端不支持查看凭证）
   - 记下 **App ID**、**App Secret**、**API 网关地址**

## 安装

```powershell
git clone https://github.com/omibao/LxOcConnector.git
cd LxOcConnector
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env`，填写真实值：

```powershell
Copy-Item .env.example .env
notepad .env
```

关键字段：

| 变量 | 必填 | 说明 |
|------|------|------|
| `LANSENGER_APP_ID` | ✅ | 蓝信个人机器人 App ID |
| `LANSENGER_APP_SECRET` | ✅ | 蓝信个人机器人 App Secret |
| `LANSENGER_API_GATEWAY_URL` | ✅ | API 网关。公有云 `https://open.e.lanxin.cn/open/apigw`；私有部署填贵司网关 |
| `OPENCODE_BASE_URL` | ✅ | opencode 服务地址，默认 `http://localhost:4096` |
| `OPENCODE_SERVER_PASSWORD` |   | 若 opencode 设了密码则填 |
| `OPENCODE_MODEL_PROVIDER` / `OPENCODE_MODEL_ID` |   | 指定模型，留空用默认 |
| `REQUIRE_MENTION` |   | 群聊是否必须 @机器人才响应，默认 `true` |
| `ALLOWED_USERS` |   | 允许的用户 openId 列表（逗号分隔），留空不限制 |
| `SESSION_PERSISTENCE` |   | 每个 chat 保留 opencode 会话上下文，默认 `true` |

> **如何确认公有云/私有部署**：看蓝信桌面端凭证弹窗里的「API 网关地址」。
> 如果是 `open.e.lanxin.cn` 就是公有云；如果是贵司域名就是私有部署。

## 运行

```powershell
python main.py
```

正常启动会看到：
```
[INFO] lanxin-opencode: ✅ opencode 服务可达：version=...
[INFO] 蓝信: 获取 WS 票据成功：expiresIn=7200s pingInterval=50s
[INFO] 蓝信: WebSocket 已连接
[INFO] 蓝信: 入站监听已启动
```

然后在蓝信里给机器人发消息（私聊或群里 @它），即可收到 opencode 的回复。按 `Ctrl+C` 退出。

## 使用方式

- **私聊**：个人机器人只能与**创建者本人**私聊。直接给机器人发消息即可。
- **群聊**：把机器人拉进群，**@机器人 + 你的问题**触发回复（`REQUIRE_MENTION=true` 时）。
- **多轮对话**：默认每个聊天会保留 opencode 会话上下文（`SESSION_PERSISTENCE=true`），可连续追问。

## 文件结构

```
LxOcConnector/
├── main.py              # 入口：启动检查 + 组装各组件
├── config.py            # .env 配置加载与校验
├── lansenger_inbound.py # 蓝信 WebSocket 长连接入站监听（移植自 hermes-lansenger-adapter）
├── opencode_client.py   # opencode server HTTP 客户端
├── bridge.py            # 桥接逻辑：消息路由 + 会话管理 + 分段发送
├── requirements.txt     # 依赖
├── .env.example         # 配置模板
└── README.md
```

## 协议细节（参考实现）

蓝信 WebSocket 协议来自开源的 [`hermes-lansenger-adapter`](https://github.com/lansenger-pm/hermes-lansenger-adapter)（MIT）：

1. **获取票据**：`POST {gateway}/v1/ws/endpoint/create`，body `{appId, secret}` → 返回 `wsEndpoint`、`expiresIn`(7200s)、`pingInterval`(50s)
2. **连接** `wsEndpoint`，websockets 库自带 RFC6455 ping/pong
3. **入站消息**：JSON `{events:[{type, data}]}`，关注 `bot_private_message` / `bot_group_message`
4. **重连退避**：`[2, 5, 10, 30, 60]` 秒；票据过期前长时间无入站消息会主动重连

出站用 `lansenger-sdk` 的 `LansengerClient.send_text(chat_id, content, is_group=...)`，自动处理 appToken 刷新（7200s）。

## 故障排查

| 现象 | 排查 |
|------|------|
| 启动报"无法连接 opencode 服务" | 确认 `opencode serve` 在跑，端口对得上 |
| "获取 WS 票据失败" | 检查 App ID/Secret、网关地址是否正确；私有部署确认能访问到网关 |
| 连上但收不到消息 | 私聊：确认你是机器人创建者；群聊：确认 `REQUIRE_MENTION=true` 时有 @机器人 |
| 回复发不出 | 看日志的蓝信 errCode/errMsg；个人机器人只能发私聊(创建者)和群聊 |
| opencode 回复慢 | `opencode_client.py` 默认超时 600s；复杂任务工具调用较慢属正常 |

## 已知限制

- 个人机器人**只能与创建者私聊**；给同事用主战场是**群聊 @提及**
- 出站只支持文本（`msgType=text`）；如需 Markdown/卡片/文件，可改用 `ls.send_markdown` / `send_file` / `send_app_card`
- 入站只解析 text / formatText；图片/文件/语音消息会被跳过（如需支持可扩展 `lansenger_inbound._extract_text`）
- 每个蓝信 chat 同时只处理一条消息（opencode session 非并发安全），并发消息会排队
