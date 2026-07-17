"""蓝信个人机器人 WebSocket 入站监听器。

协议说明（来自 hermes-lansenger-adapter 开源实现 + lansenger-sdk 常量）：

1. 获取 WS 票据：
     POST {api_gateway}/v1/ws/endpoint/create
     body: {"appId": app_id, "secret": app_secret}
     返回: { errCode:0, data:{ wsEndpoint, expiresIn(默认7200), pingInterval(默认50) } }

2. 连接 wsEndpoint（wss://...），websockets 库自带 RFC6455 ping/pong。

3. 收到的消息是 JSON：{ "events": [ {type, data}, ... ] }
   我们关心的事件类型：
     - bot_private_message  私聊（个人机器人只能与创建者私聊）
     - bot_group_message    群聊（@提及触发）

   data 字段关键内容：
     msgId, from(发送者openId), fromType(0=人 1=机器人), senderName,
     msgType(text/format/image/file/...), msgData{...},
     groupId(仅群聊), groupName(仅群聊), botId(仅群聊,自己),
     reminder{ isAtMe, isAtAll, staffs[], bots[] }(仅群聊)

4. 重连退避：[2,5,10,30,60] 秒，与官方实现一致。
   票据 7200s 过期前若长时间无入站消息，主动断开重连（静默死亡保护）。

出站（发回复）直接用已安装的 lansenger-sdk 的 LansengerClient（异步），
它处理 appToken 自动刷新、私聊/群聊路由、多种消息类型。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
import websockets

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
INBOUND_SILENCE_TIMEOUT = 7200  # 票据 TTL，超过这个时间无入站消息则重连
WS_ENDPOINT_PATH = "/v1/ws/endpoint/create"


@dataclass
class InboundMessage:
    """解析后的入站消息。"""
    chat_id: str          # 私聊=发送者openId；群聊=groupId
    sender_id: str        # 发送者 openId
    sender_name: str
    text: str             # 提取出的文本
    is_group: bool
    msg_id: str
    is_at_me: bool        # 群聊是否 @了本机器人
    is_at_all: bool
    raw: dict[str, Any]   # 原始 event data，供调试/扩展


MessageHandler = Callable[[InboundMessage], Awaitable[None]]


class LansengerInbound:
    """蓝信个人机器人 WebSocket 长连接监听器。

    只负责"收"——收到消息后回调 handler。发送由 lansenger-sdk 负责。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        api_gateway_url: str,
        on_message: MessageHandler,
        require_mention: bool = True,
    ):
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_gateway_url = api_gateway_url.rstrip("/")
        self._on_message = on_message
        self._require_mention = require_mention

        self._http: httpx.AsyncClient | None = None
        self._ws_url: str | None = None
        self._ping_interval: int = 50
        self._ws_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._running = False
        self._last_inbound_time: float = 0.0
        # 已见过的 msgId 去重（防止 WS 重连导致的重复投递）
        self._seen: dict[str, float] = {}
        self._seen_max = 2000

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)
        self._ws_url = await self._get_ws_url()
        if not self._ws_url:
            logger.error("[蓝信] 无法获取 WebSocket 票据，请检查 appId/secret 和网关地址")
            self._running = False
            return
        self._ws_task = asyncio.create_task(self._run_ws())
        self._watchdog_task = asyncio.create_task(self._watchdog())
        logger.info("[蓝信] 入站监听已启动")

    async def stop(self) -> None:
        self._running = False
        for t in (self._ws_task, self._watchdog_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._ws_task = None
        self._watchdog_task = None
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("[蓝信] 入站监听已停止")

    # ---- 票据获取 ----
    async def _get_ws_url(self) -> str | None:
        if not self._http:
            self._http = httpx.AsyncClient(timeout=30.0)
        url = f"{self._api_gateway_url}{WS_ENDPOINT_PATH}"
        try:
            r = await self._http.post(url, json={"appId": self._app_id, "secret": self._app_secret})
            r.raise_for_status()
            data = r.json()
            if data.get("errCode") == 0:
                d = data.get("data", {})
                ws_url = d.get("wsEndpoint")
                self._ping_interval = int(d.get("pingInterval", 50))
                expires_in = d.get("expiresIn", 7200)
                logger.info("[蓝信] 获取 WS 票据成功：expiresIn=%ss pingInterval=%ss", expires_in, self._ping_interval)
                return ws_url
            logger.error("[蓝信] 获取 WS 票据失败：errCode=%s errMsg=%s", data.get("errCode"), data.get("errMsg"))
            return None
        except Exception as e:
            logger.error("[蓝信] 获取 WS 票据异常：%s (type=%s)", e, type(e).__name__)
            return None

    # ---- WS 主循环 ----
    async def _run_ws(self) -> None:
        ws_url = self._ws_url
        backoff_idx = 0
        try:
            while self._running:
                try:
                    ping_interval = self._ping_interval
                    ping_timeout = max(15, ping_interval // 3)
                    logger.info("[蓝信] 连接 WebSocket（ping=%ss timeout=%ss）", ping_interval, ping_timeout)
                    ws = await asyncio.wait_for(
                        websockets.connect(
                            ws_url,
                            ping_interval=ping_interval,
                            ping_timeout=ping_timeout,
                            close_timeout=10,
                            open_timeout=10,
                        ),
                        timeout=15,
                    )
                    try:
                        async with ws:
                            backoff_idx = 0
                            self._last_inbound_time = time.time()
                            logger.info("[蓝信] WebSocket 已连接")
                            keepalive = asyncio.create_task(self._keepalive(ws))
                            try:
                                async for raw in ws:
                                    await self._on_raw(raw)
                            finally:
                                keepalive.cancel()
                                try:
                                    await keepalive
                                except asyncio.CancelledError:
                                    pass
                    except websockets.exceptions.ConnectionClosedOK as e:
                        logger.info("[蓝信] WebSocket 正常关闭 (code=%d)", e.code)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    if not self._running:
                        return
                    logger.warning("[蓝信] WebSocket 异常：%s (type=%s)", e, type(e).__name__)

                if not self._running:
                    return
                logger.warning("[蓝信] WebSocket 断开，准备重连")

                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                logger.info("[蓝信] %ds 后重连（第 %d 次）...", delay, backoff_idx + 1)
                try:
                    await asyncio.sleep(delay)
                    backoff_idx += 1
                    # 重建 http 客户端避免连接池僵尸
                    if self._http:
                        try:
                            await self._http.aclose()
                        except Exception:
                            pass
                    self._http = httpx.AsyncClient(timeout=30.0)
                    new_url = await self._get_ws_url()
                    if new_url:
                        ws_url = new_url
                        self._ws_url = new_url
                    else:
                        logger.error("[蓝信] 重连时获取票据失败，下个周期重试")
                except asyncio.CancelledError:
                    return
        except asyncio.CancelledError:
            logger.info("[蓝信] WS 主循环已取消")

    async def _keepalive(self, ws, interval: int = 120) -> None:
        """应用层心跳：协议层 ping/pong + 入站静默检测。

        websockets 库的 ping/pong 只能发现 TCP 层的死连接；若服务端关闭
        但 socket 处于 CLOSE_WAIT 状态，async for 会永远阻塞。
        所以额外用 ws.ping() 主动探测，并检查入站静默时长。
        """
        try:
            while self._running:
                await asyncio.sleep(interval)
                if not self._running or ws is None:
                    return
                # 机制1：协议层 ping/pong
                try:
                    await asyncio.wait_for(ws.ping(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning("[蓝信] keepalive ping 超时，断开重连")
                    try:
                        await asyncio.wait_for(ws.close(), timeout=10)
                    except Exception:
                        pass
                    return
                except Exception as e:
                    logger.warning("[蓝信] keepalive ping 失败：%s，断开重连", e)
                    try:
                        await asyncio.wait_for(ws.close(), timeout=10)
                    except Exception:
                        pass
                    return
                # 机制2：入站静默
                silence = time.time() - self._last_inbound_time
                if silence > INBOUND_SILENCE_TIMEOUT:
                    logger.warning("[蓝信] %ds 无入站消息，断开重连", int(silence))
                    try:
                        await asyncio.wait_for(ws.close(), timeout=10)
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    async def _watchdog(self, interval: int = 60) -> None:
        """看门狗：WS 任务意外死亡时拉起重连。"""
        try:
            while self._running:
                await asyncio.sleep(interval)
                if not self._running:
                    return
                if self._ws_task is None or self._ws_task.done():
                    logger.warning("[蓝信] 看门狗：WS 任务已死，重启")
                    await self._restart_ws()
        except asyncio.CancelledError:
            pass

    async def _restart_ws(self) -> None:
        if not self._running:
            return
        if self._ws_task and not self._ws_task.done():
            return
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
        self._http = httpx.AsyncClient(timeout=30.0)
        new_url = await self._get_ws_url()
        if new_url:
            self._ws_url = new_url
            self._ws_task = asyncio.create_task(self._run_ws())
        else:
            logger.error("[蓝信] 重启时获取票据失败")

    # ---- 消息解析 ----
    async def _on_raw(self, raw: str) -> None:
        self._last_inbound_time = time.time()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[蓝信] 收到非法 JSON 消息")
            return
        events = data.get("events") or []
        for ev in events:
            try:
                await self._process_event(ev)
            except Exception as e:
                logger.exception("[蓝信] 处理事件异常：%s", e)

    async def _process_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype not in ("bot_private_message", "bot_group_message"):
            logger.debug("[蓝信] 忽略事件类型 %s", etype)
            return

        is_group = etype == "bot_group_message"
        msg_data = event.get("data", {}) or {}
        msg_id = msg_data.get("msgId") or ""
        if not msg_id:
            return

        # 去重
        now = time.time()
        if msg_id in self._seen:
            logger.debug("[蓝信] 重复消息 %s，跳过", msg_id)
            return
        self._seen[msg_id] = now
        # 清理过期项
        if len(self._seen) > self._seen_max:
            cutoff = now - 3600
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

        sender_id = msg_data.get("from", "")
        # 群聊：跳过自己发的消息（回声防护）
        if is_group:
            self_bot_id = msg_data.get("botId")
            if self_bot_id and sender_id == self_bot_id:
                return

        text = self._extract_text(msg_data)
        if not text:
            logger.debug("[蓝信] 消息无文本内容（msgType=%s），跳过", msg_data.get("msgType"))
            return

        if is_group:
            chat_id = msg_data.get("groupId") or sender_id
            reminder = msg_data.get("reminder", {}) or {}
            is_at_me = bool(reminder.get("isAtMe", False))
            is_at_all = bool(reminder.get("isAtAll", False))
            # require_mention 过滤
            if self._require_mention and not is_at_me and not is_at_all:
                logger.debug("[蓝信] 群消息未 @本机器人，跳过（chat=%s）", chat_id)
                return
        else:
            chat_id = sender_id
            is_at_me = False
            is_at_all = False

        msg = InboundMessage(
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=msg_data.get("senderName", sender_id),
            text=text,
            is_group=is_group,
            msg_id=msg_id,
            is_at_me=is_at_me,
            is_at_all=is_at_all,
            raw=msg_data,
        )
        logger.info(
            "[蓝信] 收到消息 chat=%s from=%s(%s) group=%s text=%s",
            chat_id[:24], sender_id[:24], msg.sender_name, is_group, text[:80],
        )
        await self._on_message(msg)

    @staticmethod
    def _extract_text(msg_data: dict[str, Any]) -> str:
        """从 msgData 提取文本（仅 text / format / formatText）。"""
        msg_type = msg_data.get("msgType", "text")
        payload = msg_data.get("msgData", {}) or {}
        if msg_type == "text":
            return (payload.get("text", {}) or {}).get("content", "").strip()
        if msg_type in ("format", "formatText"):
            fmt = payload.get("format") or payload.get("formatText") or {}
            return fmt.get("text", "").strip() if isinstance(fmt, dict) else ""
        # 其他类型（图片/文件/语音/卡片...）暂不支持，返回空
        return ""
