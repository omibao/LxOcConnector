"""桥接逻辑：蓝信消息 → opencode 会话 → AI 回复（含 thinking）→ 蓝信回复。

每个蓝信 chat_id 对应一个 opencode session（默认开启，保留多轮上下文）。
收到消息后：
  1. 找到/创建该 chat 的 opencode session
  2. 流式调用 opencode（prompt_async + SSE），实时把 thinking 转发到蓝信
  3. 最终回复按 max_message_length 分段发回蓝信
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
from opencode_client import OpencodeClient, OpencodeError, OpencodeTimeout

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
    """按最大长度分段，尽量在换行处断开。

    不变量："".join(parts) == text（不丢失任何字符，包括换行符）。
    """
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
            parts.append(remaining[:cut])
            remaining = remaining[cut:]
        else:
            parts.append(remaining[:cut + 1])
            remaining = remaining[cut + 1:]
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
        # 临时会话列表缓存（/sessions + /more 用序号选择）
        self._session_list: list[dict] = []
        self._session_offset: int = 0
        self._page_size: int = 10

    async def handle(self, msg: InboundMessage) -> None:
        if not self.cfg.is_user_allowed(msg.sender_id):
            logger.info("[桥接] 用户 %s 被拒绝访问", msg.sender_id[:24])
            return
        # 群聊里 @机器人 时蓝信会追加 "@机器人名"，先去掉
        text = msg.text.strip()
        # 斜杠命令拦截
        if text.startswith("/"):
            lock = _get_lock(msg.chat_id)
            async with lock:
                await self._handle_command(msg, text)
            return
        lock = _get_lock(msg.chat_id)
        async with lock:
            await self._process(msg)

    # ---- 斜杠命令 ----
    async def _handle_command(self, msg: InboundMessage, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/sessions", "/ls", "/list"):
            await self._cmd_list_sessions(msg, reset=True)
        elif cmd in ("/more", "/next"):
            await self._cmd_list_sessions(msg, reset=False)
        elif cmd in ("/switch", "/use"):
            await self._cmd_switch(msg, arg)
        elif cmd in ("/new", "/start"):
            await self._cmd_new(msg)
        elif cmd in ("/fork", "/branch"):
            await self._cmd_fork(msg)
        elif cmd in ("/current", "/info"):
            await self._cmd_current(msg)
        elif cmd in ("/exit", "/leave", "/detach"):
            await self._cmd_exit(msg)
        elif cmd in ("/history", "/hist"):
            await self._cmd_history(msg, arg)
        elif cmd in ("/help", "/h", "/?"):
            await self._cmd_help(msg)
        else:
            await self._send(msg, f"未知命令 {cmd}。发送 /help 查看可用命令。")

    async def _cmd_help(self, msg: InboundMessage) -> None:
        help_text = (
            "📋 可用命令：\n"
            "/sessions — 列出 opencode 会话（每页10条）\n"
            "/more — 显示下一页会话\n"
            "/switch <序号> — 接管对应会话，继续对话\n"
            "/fork — 从当前会话分叉新会话（带最近上下文，避免大会话超时）\n"
            "/history [N] — 查看当前会话最近N轮对话（默认5）\n"
            "/new — 开始全新会话\n"
            "/current — 查看当前绑定的会话\n"
            "/exit — 取消接管，下次发消息自动新建会话\n"
            "/help — 显示本帮助\n"
            "\n直接发消息（不带 /）即与当前会话对话。"
        )
        await self._send(msg, help_text)

    async def _cmd_list_sessions(self, msg: InboundMessage, reset: bool = True) -> None:
        if reset or not self._session_list:
            try:
                sessions = await self.oc.list_all_sessions()
            except Exception as e:
                await self._send(msg, f"❌ 获取会话列表失败：{e}")
                return
            def _time(s):
                t = s.get("time", 0)
                if isinstance(t, dict):
                    return t.get("updated", 0) or t.get("created", 0)
                return t
            sessions.sort(key=_time, reverse=True)
            self._session_list = sessions
            self._session_offset = 0

        total = len(self._session_list)
        if total == 0:
            await self._send(msg, "暂无 opencode 会话。")
            return

        offset = self._session_offset
        page = self._session_list[offset:offset + self._page_size]
        if not page:
            await self._send(msg, "没有更多会话了。发 /sessions 重新从头查看。")
            return

        current_sid = self._sessions.get(msg.chat_id)

        has_more = offset + self._page_size < total
        lines = [f"📋 会话列表（{offset+1}-{offset+len(page)}/{total}，发 /switch 序号 接管）："]
        for i, s in enumerate(page):
            global_idx = offset + i
            sid = s.get("id", "")
            directory = s.get("directory", "")
            marker = " ← 当前" if sid == current_sid else ""
            project_tag = ""
            if directory:
                import os
                dirname = os.path.basename(directory.replace("\\", "/").rstrip("/"))
                project_tag = f"[{dirname}] " if dirname else ""
            last_text = s.get("last_user_text", "")
            summary = last_text[:50] if last_text else "(无对话)"
            lines.append(f"{global_idx}. {project_tag}{summary}{marker}")
        if has_more:
            lines.append("发 /more 查看更多")
        await self._send(msg, "\n".join(lines))
        # 推进偏移量供下次 /more
        self._session_offset = offset + self._page_size

    async def _cmd_switch(self, msg: InboundMessage, arg: str) -> None:
        if not arg:
            await self._send(msg, "用法：/switch <序号>（先用 /sessions 查看列表）")
            return
        try:
            idx = int(arg)
        except ValueError:
            await self._send(msg, "序号必须是数字，先用 /sessions 查看列表。")
            return
        # 重新查询列表，确保序号与最新排序一致
        # （/sessions 之后如果有新消息，time_updated 会变，排序会偏移）
        try:
            sessions = await self.oc.list_all_sessions()
            def _time(s):
                t = s.get("time", 0)
                if isinstance(t, dict):
                    return t.get("updated", 0) or t.get("created", 0)
                return t
            sessions.sort(key=_time, reverse=True)
            self._session_list = sessions
        except Exception as e:
            await self._send(msg, f"❌ 刷新会话列表失败：{e}")
            return
        if idx < 0 or idx >= len(self._session_list):
            await self._send(msg, "序号无效，先用 /sessions 刷新列表。")
            return
        s = self._session_list[idx]
        sid = s.get("id", "")
        title = s.get("title", "(无标题)")
        directory = s.get("directory", "")
        pid = s.get("projectID") or s.get("project_id", "")
        is_cross = pid != "global" and bool(pid)
        self._sessions[msg.chat_id] = sid
        cross_hint = "\n⚠️ 跨项目会话，不支持实时思考过程，但能正常收发消息。" if is_cross else ""
        await self._send(msg, f"✅ 已接管会话：{title}\n目录：{directory}{cross_hint}\n现在直接发消息即可继续对话。")

    async def _cmd_new(self, msg: InboundMessage) -> None:
        title = f"蓝信-{msg.sender_name}-{'群' if msg.is_group else '私聊'}"
        try:
            sid = await self.oc.create_session(title=title)
        except Exception as e:
            await self._send(msg, f"❌ 创建会话失败：{e}")
            return
        self._sessions[msg.chat_id] = sid
        await self._send(msg, f"✅ 已创建新会话，直接发消息开始对话。")

    async def _cmd_fork(self, msg: InboundMessage) -> None:
        old_sid = self._sessions.get(msg.chat_id)
        if not old_sid:
            await self._send(msg, "当前未绑定会话。先用 /switch 接管一个会话再 fork。")
            return
        try:
            new_sid = await self.oc.fork_session(old_sid)
        except Exception as e:
            await self._send(msg, f"❌ fork 会话失败：{e}")
            return
        self._sessions[msg.chat_id] = new_sid
        await self._send(msg, f"✅ 已从当前会话分叉出新会话，带有最近上下文。\n现在直接发消息即可继续对话。")

    async def _cmd_current(self, msg: InboundMessage) -> None:
        sid = self._sessions.get(msg.chat_id)
        if not sid:
            await self._send(msg, "当前未绑定会话。发消息会自动创建，或用 /sessions 接管已有会话。")
            return
        # 尝试获取标题
        title = "(未知)"
        for s in self._session_list:
            if s.get("id") == sid:
                title = s.get("title", title)
                break
        await self._send(msg, f"当前会话：{title}\nID: {sid}")

    async def _cmd_exit(self, msg: InboundMessage) -> None:
        sid = self._sessions.pop(msg.chat_id, None)
        if not sid:
            await self._send(msg, "当前未绑定会话，无需退出。")
            return
        await self._send(msg, "✅ 已取消接管当前会话。下次发消息将自动新建会话。")

    async def _cmd_history(self, msg: InboundMessage, arg: str) -> None:
        sid = self._sessions.get(msg.chat_id)
        if not sid:
            await self._send(msg, "当前未绑定会话。先用 /switch 接管或 /new 新建。")
            return
        limit = 5
        if arg:
            try:
                limit = max(1, min(int(arg), 50))
            except ValueError:
                await self._send(msg, "用法：/history [N]，N 为轮数（1-50）。")
                return
        try:
            history = await self.oc.fetch_history(sid, limit=limit)
        except Exception as e:
            await self._send(msg, f"❌ 获取历史失败：{e}")
            return
        if not history:
            await self._send(msg, "该会话暂无对话记录。")
            return
        # 统计轮数（user 消息数）
        rounds = sum(1 for h in history if h["role"] == "user")
        lines = [f"📜 最近 {rounds} 轮对话："]
        for h in history:
            role = h["role"]
            label = "👤" if role == "user" else "🤖"
            text = h["text"]
            if len(text) > 300:
                text = text[:300] + "…"
            lines.append(f"{label} {text}")
        await self._send(msg, "\n".join(lines))

    async def _process(self, msg: InboundMessage) -> None:
        try:
            # 1. 获取/创建 opencode session
            session_id = await self._get_or_create_session(msg)
            if not session_id:
                await self._send(msg, "❌ 无法创建 opencode 会话，请检查 opencode 服务是否运行。")
                return

            # 2. 检查会话状态——如果 busy（上次请求卡住），先 abort
            status = await self.oc.get_session_status(session_id)
            if status == "busy":
                logger.warning("[桥接] 会话 %s 处于 busy 状态，先 abort", session_id)
                await self.oc.abort_session(session_id)
                await asyncio.sleep(1)

            # 3. 发送"正在思考"状态提示
            if self.cfg.send_thinking:
                await self._send(msg, "🤔 正在思考...")

            # 4. 同步调用 opencode（稳定可靠，支持所有项目会话）
            t0 = time.time()
            reply = await self.oc.send_prompt(
                session_id=session_id,
                text=msg.text,
                provider_id=self.cfg.opencode_model_provider,
                model_id=self.cfg.opencode_model_id,
                timeout=self.cfg.opencode_timeout,
            )

            elapsed = time.time() - t0
            logger.info("[桥接] opencode 回复耗时 %.1fs 长度 %d", elapsed, len(reply))

            # 5. 发送最终回复
            if not reply.strip():
                await self._send(msg, "（AI 未返回文本内容）")
                return
            for part in _split_text(reply, self.cfg.max_message_length):
                await self._send(msg, part)
        except OpencodeTimeout as e:
            logger.error("[桥接] opencode 超时：%s", e)
            await self._send(msg, f"⏰ {e}\n\n会话上下文较大，opencode 处理超时。请稍后重试，或用 /fork 从最近的消息分叉新会话。")
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
        """发回蓝信。私聊/群聊由 is_group 决定。"""
        try:
            if msg.is_group:
                await self.ls.send_text(
                    chat_id=msg.chat_id,
                    content=content,
                    is_group=True,
                )
            else:
                await self.ls.send_text(
                    chat_id=msg.chat_id,
                    content=content,
                )
        except LansengerError as e:
            logger.error("[桥接] 蓝信发送失败：%s", e)
        except Exception as e:
            logger.exception("[桥接] 发送异常：%s", e)
