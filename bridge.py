"""桥接逻辑：蓝信消息 → opencode 会话 → AI 回复 → 蓝信回复。

每个蓝信 chat_id 对应一个 opencode session（默认开启，保留多轮上下文）。
收到消息后：
  1. 找到/创建该 chat 的 opencode session
  2. 调用 send_prompt 拿到 AI 文本回复
  3. 按 max_message_length 分段，用 lansenger-sdk 发回蓝信
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from lansenger_sdk import LansengerClient
from lansenger_sdk import LansengerError

from config import Config
from lansenger_inbound import InboundMessage
from opencode_client import OpencodeClient, OpencodeError

logger = logging.getLogger(__name__)

# 单个 chat 同时只允许一个 prompt 在跑（opencode session 非并发安全）
_chat_locks: dict[str, asyncio.Lock] = {}


def _get_lock(chat_id: str) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


def _split_text(text: str, max_len: int) -> list[str]:
    """按最大长度分段，尽量在换行处断开。"""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        # 优先在最近的换行处切
        cut = remaining.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


class Bridge:
    """蓝信 ↔ opencode 桥接器。"""

    def __init__(self, cfg: Config, oc: OpencodeClient, ls: LansengerClient):
        self.cfg = cfg
        self.oc = oc
        self.ls = ls
        # chat_id → opencode session_id 映射（会话持久化）
        self._sessions: dict[str, str] = {}

    async def handle(self, msg: InboundMessage) -> None:
        # 权限检查
        if not self.cfg.allow_all_users:
            if self.cfg.allowed_users and msg.sender_id not in self.cfg.allowed_users:
                logger.info("[桥接] 用户 %s 不在允许列表，忽略", msg.sender_id[:24])
                return
        lock = _get_lock(msg.chat_id)
        async with lock:
            await self._process(msg)

    async def _process(self, msg: InboundMessage) -> None:
        # 先发"正在思考"提示（群聊尤其需要，避免用户以为没反应）
        ack_sent = False
        try:
            # 1. 获取/创建 opencode session
            session_id = await self._get_or_create_session(msg)
            if not session_id:
                await self._send(msg, "❌ 无法创建 opencode 会话，请检查 opencode 服务是否运行。")
                return

            # 2. 群聊发送 ack
            if msg.is_group:
                await self._send(msg, "⏳ 正在处理...")
                ack_sent = True

            # 3. 调用 opencode
            t0 = time.time()
            reply = await self.oc.send_prompt(
                session_id=session_id,
                text=msg.text,
                provider_id=self.cfg.opencode_model_provider,
                model_id=self.cfg.opencode_model_id,
            )
            elapsed = time.time() - t0
            logger.info("[桥接] opencode 回复耗时 %.1fs 长度 %d", elapsed, len(reply))

            # 4. 如果之前发了 ack 且回复很快，撤回 ack（可选；这里简单覆盖）
            # 5. 发送回复
            if not reply.strip():
                await self._send(msg, "（AI 未返回文本内容）")
                return
            for part in _split_text(reply, self.cfg.max_message_length):
                await self._send(msg, part)
        except OpencodeError as e:
            logger.error("[桥接] opencode 错误：%s", e)
            await self._send(msg, f"❌ opencode 调用失败：{e}")
        except LansengerError as e:
            logger.error("[桥接] 蓝信发送错误：%s", e)
        except Exception as e:
            logger.exception("[桥接] 处理消息异常：%s", e)
            try:
                await self._send(msg, f"❌ 内部错误：{e}")
            except Exception:
                pass

    async def _get_or_create_session(self, msg: InboundMessage) -> str | None:
        if self.cfg.session_persistence:
            sid = self._sessions.get(msg.chat_id)
            if sid:
                return sid
        title = f"蓝信-{msg.sender_name}-{'群' if msg.is_group else '私聊'}"
        try:
            sid = await self.oc.create_session(title=title)
        except Exception as e:
            logger.error("[桥接] 创建会话失败：%s", e)
            return None
        self._sessions[msg.chat_id] = sid
        return sid

    async def _send(self, msg: InboundMessage, content: str) -> None:
        """发回蓝信。私聊/群聊由 is_group 决定，群聊带上 @发送者。"""
        try:
            if msg.is_group:
                # 群聊：send_text(chat_id, content, is_group=True)
                # 机器人身份发送，无需 userToken
                await self.ls.send_text(
                    chat_id=msg.chat_id,
                    content=content,
                    is_group=True,
                )
            else:
                # 私聊：个人机器人只能与创建者私聊
                await self.ls.send_text(
                    chat_id=msg.chat_id,
                    content=content,
                )
        except LansengerError as e:
            logger.error("[桥接] 蓝信发送失败：%s", e)
        except Exception as e:
            logger.exception("[桥接] 发送异常：%s", e)
