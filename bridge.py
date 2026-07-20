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
    """按最大长度分段，尽量在换行处断开。"""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
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
        # 临时会话列表缓存（/sessions + /more 用序号选择）
        self._session_list: list[dict] = []
        self._session_offset: int = 0
        self._page_size: int = 10

    async def handle(self, msg: InboundMessage) -> None:
        if not self.cfg.allow_all_users:
            if self.cfg.allowed_users and msg.sender_id not in self.cfg.allowed_users:
                logger.info("[桥接] 用户 %s 不在允许列表，忽略", msg.sender_id[:24])
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
        elif cmd in ("/current", "/info"):
            await self._cmd_current(msg)
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
            "/new — 开始全新会话\n"
            "/current — 查看当前绑定的会话\n"
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

        # 并行获取本页每个会话的最后一条 user 消息作为摘要
        async def _summary(s):
            sid = s.get("id", "")
            try:
                last = await self.oc.fetch_last_user_message(sid)
            except Exception:
                last = ""
            return last[:50] if last else "(无对话)"
        summaries = await asyncio.gather(*[_summary(s) for s in page])
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
            lines.append(f"{global_idx}. {project_tag}{summaries[i]}{marker}")
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
        if not self._session_list or idx < 0 or idx >= len(self._session_list):
            await self._send(msg, "序号无效，先用 /sessions 刷新列表。")
            return
        s = self._session_list[idx]
        sid = s.get("id", "")
        title = s.get("title", "(无标题)")
        self._sessions[msg.chat_id] = sid
        await self._send(msg, f"✅ 已接管会话：{title}\n现在直接发消息即可继续对话。")

    async def _cmd_new(self, msg: InboundMessage) -> None:
        title = f"蓝信-{msg.sender_name}-{'群' if msg.is_group else '私聊'}"
        try:
            sid = await self.oc.create_session(title=title)
        except Exception as e:
            await self._send(msg, f"❌ 创建会话失败：{e}")
            return
        self._sessions[msg.chat_id] = sid
        await self._send(msg, f"✅ 已创建新会话，直接发消息开始对话。")

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

    def _is_cross_project(self, session_id: str) -> bool:
        """判断会话是否属于其他项目（当前 serve 实例看不到 SSE 事件）。

        当前 serve 实例的 projectID 是 "global"（在 / 启动）。
        通过 /switch 接管的其他项目会话，projectID 不同，SSE 收不到事件。
        """
        # 从缓存的会话列表里找该会话的 project_id
        for s in self._session_list:
            if s.get("id") == session_id:
                pid = s.get("projectID") or s.get("project_id", "")
                return pid != "global" and bool(pid)
        # 缓存里没有（新建的会话）→ 当前项目
        return False

    async def _process(self, msg: InboundMessage) -> None:
        try:
            # 1. 获取/创建 opencode session
            session_id = await self._get_or_create_session(msg)
            if not session_id:
                await self._send(msg, "❌ 无法创建 opencode 会话，请检查 opencode 服务是否运行。")
                return

            # 2. 发送"正在思考"状态提示
            if self.cfg.send_thinking:
                await self._send(msg, "🤔 正在思考...")

            # 3. 判断是否当前项目会话
            # 当前 serve 实例的 projectID 是 "global"，工作目录是 /
            # 其他项目会话的 SSE 事件不会推送到当前 serve，需用同步接口
            is_cross_project = self._is_cross_project(session_id)

            t0 = time.time()
            if is_cross_project:
                # 跨项目：SSE 收不到事件，用同步接口
                logger.info("[桥接] 跨项目会话 %s，使用同步接口", session_id)
                reply = await self.oc.send_prompt(
                    session_id=session_id,
                    text=msg.text,
                    provider_id=self.cfg.opencode_model_provider,
                    model_id=self.cfg.opencode_model_id,
                )
            else:
                # 当前项目：SSE 流式，thinking 实时转发
                thinking_buffer = ThinkingBuffer(
                    send_fn=lambda text: self._send(msg, text),
                    flush_interval=self.cfg.thinking_flush_interval,
                    enabled=self.cfg.send_thinking,
                )
                await thinking_buffer.start()
                reply = await self.oc.send_prompt_streaming(
                    session_id=session_id,
                    text=msg.text,
                    provider_id=self.cfg.opencode_model_provider,
                    model_id=self.cfg.opencode_model_id,
                    on_reasoning=thinking_buffer.add,
                    on_text=None,
                    timeout=self.cfg.opencode_timeout,
                )
                await thinking_buffer.flush_remaining()

            elapsed = time.time() - t0
            logger.info("[桥接] opencode 回复耗时 %.1fs 长度 %d", elapsed, len(reply))

            # 4. 发送最终回复
            if not reply.strip():
                await self._send(msg, "（AI 未返回文本内容）")
                return
            for part in _split_text(reply, self.cfg.max_message_length):
                await self._send(msg, part)
        except OpencodeTimeout as e:
            logger.error("[桥接] opencode 超时：%s", e)
            await self._send(msg, f"⏰ {e}")
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


class ThinkingBuffer:
    """累积 reasoning 增量，定时分批发到蓝信。

    蓝信不支持"编辑消息"，所以 thinking 只能分批发新消息。
    每 flush_interval 秒检查一次缓冲区，有内容就发一条 "💭 ..." 消息。
    """

    def __init__(self, send_fn, flush_interval: float = 3.0, enabled: bool = True):
        self._send_fn = send_fn
        self._flush_interval = flush_interval
        self._enabled = enabled
        self._buffer: list[str] = []
        self._flushed_any = False
        self._task: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        if not self._enabled:
            return
        self._task = asyncio.create_task(self._flush_loop())

    async def add(self, delta: str) -> None:
        if not self._enabled:
            return
        self._buffer.append(delta)

    async def _flush_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self._flush_interval)
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        prefix = "💭 " if not self._flushed_any else "💭 … "
        self._flushed_any = True
        # 截断过长的 thinking 片段
        if len(text) > 1500:
            text = text[:1500] + "…"
        try:
            await self._send_fn(f"{prefix}{text}")
        except Exception as e:
            logger.warning("[桥接] thinking 发送失败: %s", e)

    async def flush_remaining(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()
