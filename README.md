# LxOcConnector

> 蓝信（Lansenger）↔ [opencode](https://opencode.ai) 桥接服务

在蓝信里和 opencode AI 助手对话——收发消息、实时查看思考过程、随时接管电脑上正在进行的会话。本机主动连出蓝信 WebSocket，**无需公网 IP、域名或内网穿透**。

```
蓝信云  ←─ WebSocket 长连 ──←  LxOcConnector  ←─ HTTP ──→  opencode serve (本机)
                               ↑ thinking 实时转发 ↑        ↑ 会话管理 ↑
```

---

## 核心功能

| 功能 | 说明 |
|------|------|
| 💬 **收发消息** | 私聊直接对话；群聊 @机器人 触发；多轮上下文自动保留 |
| 🧠 **思考过程实时转发** | AI 的 reasoning 增量每 3 秒推送到蓝信，边想边看 |
| 🔄 **会话接管** | 下班后用蓝信列出 / 切换电脑上所有 opencode 会话（跨项目） |
| 📋 **斜杠命令** | `/sessions` `/more` `/switch` `/new` `/current` `/help` |
| 🔒 **纯本机** | 主动连出，不开放端口，凭证只存 `.env` |
| 🔄 **自动重连** | WS 断线退避 [2,5,10,30,60]s；票据过期自动刷新 |

---

## 快速开始

### 一键启动（推荐）

```powershell
git clone https://github.com/omibao/LxOcConnector.git
cd LxOcConnector
pip install -r requirements.txt
```

配置凭证（首次运行会自动创建 `.env`）：

```powershell
.\start.bat
```

脚本会自动：1）从 `.env.example` 创建 `.env`（首次）→ 2）启动 `opencode serve`（端口 4096）→ 3）启动蓝信桥接。

> **Linux / macOS**：`chmod +x start.sh && ./start.sh`

### 手动启动

1. 复制并编辑配置：
   ```powershell
   Copy-Item .env.example .env
   # 填写 LANSENGER_APP_ID / LANSENGER_APP_SECRET / LANSENGER_API_GATEWAY_URL
   ```

2. 启动 opencode 后端（另开一个终端）：
   ```powershell
   opencode serve --port 4096
   ```

3. 启动桥接：
   ```powershell
   python main.py
   ```

正常启动后：
```
[INFO] ✅ opencode 服务可达：version=1.18.3
[INFO] 蓝信: 获取 WS 票据成功：expiresIn=7200s pingInterval=20s
[INFO] 蓝信: WebSocket 已连接
[INFO] 蓝信: 入站监听已启动
```

在蓝信里给机器人发消息即可。`Ctrl+C` 退出。

---

## 配置项

编辑 `.env`（从 `.env.example` 复制）：

| 变量 | 必填 | 默认 | 说明 |
|------|:----:|------|------|
| `LANSENGER_APP_ID` | ✅ | — | 蓝信个人机器人 App ID |
| `LANSENGER_APP_SECRET` | ✅ | — | 蓝信个人机器人 App Secret |
| `LANSENGER_API_GATEWAY_URL` | ✅ | `https://open.e.lanxin.cn/open/apigw` | 公有云或私有部署网关 |
| `OPENCODE_BASE_URL` | ✅ | `http://localhost:4096` | opencode serve 地址 |
| `OPENCODE_SERVER_PASSWORD` | | — | opencode serve 密码 |
| `OPENCODE_MODEL_PROVIDER` / `_ID` | | — | 指定模型，留空用默认 |
| `OPENCODE_TIMEOUT` | | `600` | 单次请求超时（秒） |
| `SEND_THINKING` | | `true` | 是否实时转发 AI 思考过程 |
| `THINKING_FLUSH_INTERVAL` | | `3` | thinking 推送间隔（秒） |
| `REQUIRE_MENTION` | | `true` | 群聊是否必须 @机器人 |
| `ALLOW_ALL_USERS` | | `false` | 允许所有用户（调试用） |
| `ALLOWED_USERS` | | — | 允许的用户 openId（逗号分隔） |
| `SESSION_PERSISTENCE` | | `true` | 保留多轮上下文 |

> **获取蓝信凭证**：蓝信桌面端 → 通讯录 → 智能机器人 → 个人机器人 → 点击右侧 ℹ️ 图标（移动端不支持）
>
> **公有云 vs 私有部署**：凭证弹窗里的网关地址是 `open.e.lanxin.cn` 即公有云；是贵司域名即私有部署。

---

## 蓝信命令

直接发消息即与当前会话对话。以 `/` 开头则执行命令：

| 命令 | 说明 |
|------|------|
| `/sessions` | 列出所有 opencode 会话（跨项目，按最新对话时间排序，每页 10 条） |
| `/more` | 显示下一页会话 |
| `/switch <序号>` | 接管对应会话，继续对话 |
| `/new` | 开始全新会话 |
| `/current` | 查看当前绑定的会话 |
| `/help` | 显示命令帮助 |

会话列表格式示例：
```
📋 会话列表（1-10/16，发 /switch 序号 接管）：
0. [my-web-app] 修复登录页 CSS 样式错乱的问题
1. [data-pipeline] 优化每日数据同步的并发性能
2. [api-gateway] 给 /users 接口加分页和模糊搜索
3. [mobile-app] App 首页白屏，可能是路由配置问题
4. [infra-tools] 写一个自动清理过期日志的脚本
5. [my-web-app] 帮我写单元测试覆盖 auth 模块
6. [data-pipeline] 数据库连接池偶尔耗尽，帮忙排查
...
发 /more 查看更多
```

### 远程接管场景

> 下班后电脑留在公司，用蓝信继续控制 opencode 正在进行的会话

1. 蓝信发 `/sessions` → 看到所有项目的会话列表
2. 找到要接管的，发 `/switch <序号>`
3. 之后直接发消息，就在那个会话里继续对话

跨项目会话自动回退同步接口（看不到 thinking，但能正常收发）。

---

## 项目结构

```
LxOcConnector/
├── main.py               # 入口
├── config.py             # .env 配置加载
├── lansenger_inbound.py  # 蓝信 WebSocket 入站监听
├── opencode_client.py    # opencode server HTTP + SSE 流式客户端
├── bridge.py             # 桥接逻辑 + 斜杠命令 + thinking 转发
├── start.bat             # Windows 一键启动
├── start.sh              # Linux/macOS 一键启动
├── requirements.txt
├── .env.example          # 配置模板
└── README.md
```

---

## 前置条件

| 依赖 | 版本 | 安装 |
|------|------|------|
| Python | ≥ 3.10 | [python.org](https://python.org) |
| opencode CLI | ≥ 1.18 | `npm install -g opencode-ai` |
| 蓝信个人机器人 | — | 蓝信桌面端创建 |

opencode 的模型/Provider 在 `~/.config/opencode.json` 中配置，桥接通过 `OPENCODE_MODEL_PROVIDER` / `OPENCODE_MODEL_ID` 覆盖。

> **CLI 版 vs 桌面版**：桥接需要固定端口 + 可控密码的 serve 实例。opencode 桌面版（OpenCode.exe）内置的 serve 端口和密码每次启动随机生成且不持久化，**无法接入**。请用 CLI 版运行 `opencode serve`。桌面版可与 CLI serve 并存——两者共享同一个 `~/.config/opencode` 配置和会话数据库，`/sessions` 能列出桌面版创建的会话并接管。

---

## 故障排查

| 现象 | 解决 |
|------|------|
| "无法连接 opencode 服务" | 确认 `opencode serve` 在跑，端口与 `OPENCODE_BASE_URL` 一致 |
| "获取 WS 票据失败" | 检查 App ID/Secret、网关地址；私有部署确认能访问网关 |
| 连上但收不到消息 | 私聊：确认你是机器人创建者；群聊：确认有 @机器人 |
| 回复发不出 | 个人机器人只能发私聊（创建者）和群聊；看日志 errCode |
| opencode 回复慢 | 复杂任务工具调用较慢，默认超时 600s |
| 接管会话后无回复 | 跨项目会话需用同步接口，确认 opencode serve 在运行 |

---

## 已知限制

- 个人机器人**只能与创建者私聊**；给同事用主战场是群聊 @提及
- 出站仅支持纯文本（个人机器人无 Markdown/卡片权限）
- 入站仅解析 text/formatText；图片/文件/语音暂跳过
- 每个蓝信 chat 同时只处理一条消息（opencode session 非并发安全）

---

## 技术背景

蓝信 WebSocket 协议移植自开源 [`hermes-lansenger-adapter`](https://github.com/lansenger-pm/hermes-lansenger-adapter)（MIT）。出站使用 [`lansenger-sdk`](https://pypi.org/project/lansenger-sdk/)（自动 appToken 刷新）。opencode 后端通过 [Server API](https://opencode.ai/docs/server) 交互，支持同步（`POST /session/:id/message`）和流式（`prompt_async` + SSE `/event`）两种模式。

---

## 更新

```powershell
git pull origin main
pip install -r requirements.txt
```

本项目持续迭代，关注 [Releases](https://github.com/omibao/LxOcConnector/releases) 获取更新通知。

---

## 更新日志

### v0.5.0

- **[安全]** 鉴权改为 fail-closed：`ALLOW_ALL_USERS=false` 且 `ALLOWED_USERS` 为空时拒绝所有人，并在启动时报错提示（避免未配置时静默放行）
- **[修复]** `_split_text` 分段不再丢失换行符：原版 `lstrip("\n")` 会吞掉切点处的换行，导致拼接后内容与原文不一致
- **[测试]** 新增 63 项单元测试，覆盖文本分段、thinking 缓冲、配置加载、鉴权逻辑、入站消息解析、opencode 认证

### v0.4.0

- **[功能]** `/sessions` 按最新对话时间排序（`time_updated`），最近活跃的会话排最前
- **[功能]** 蓝信斜杠命令：`/sessions` `/more` `/switch` `/new` `/current` `/help`
- **[功能]** 跨项目列出所有 opencode 会话（查 SQLite 数据库，不限于当前 serve 项目）
- **[功能]** 会话列表显示最后一条对话内容 + `[项目目录名]` 标签
- **[修复]** 跨项目会话自动回退同步接口（SSE 事件不跨项目，原流式模式会永久等待）
- **[功能]** 一键启动脚本 `start.bat` / `start.sh`

### v0.3.0

- **[功能]** 流式 thinking 转发：AI 思考过程每 3 秒推送到蓝信
- **[功能]** opencode `prompt_async` + SSE 事件流接收
- **[改进]** 超时从 300s 提到 600s，超时返回友好提示
- **[修复]** 补上 `inbound.start()` 调用（原版漏调导致 WS 入站监听未启动）

### v0.2.0

- **[功能]** opencode serve HTTP 客户端（创建会话、发送 prompt、获取回复）
- **[功能]** 蓝信 WebSocket 长连接入站监听（移植自 hermes-lansenger-adapter）
- **[功能]** 桥接逻辑：消息路由 + 会话持久化 + 分段发送

### v0.1.0

- 初始版本：蓝信 ↔ opencode 桥接服务骨架

---

## License

MIT
