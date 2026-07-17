"""opencode 服务端 HTTP 客户端 — 封装 session/message 相关调用。

对应 opencode 的 Server API（OpenAPI 3.1）：
  - POST /session                  创建会话
  - POST /session/:id/message      发送 prompt 并等待 AI 回复（同步）
  - POST /session/:id/prompt_async  异步发送 prompt（不等待，204）
  - GET  /event                    SSE 事件流（思考/回复增量）
  - GET  /session/:id/message      列出会话消息
  - GET  /global/health            健康检查
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


class OpencodeError(Exception):
    pass


class OpencodeTimeout(OpencodeError):
    pass


# 流式回调类型
ThinkingCallback = Callable[[str], Awaitable[None]]
TextCallback = Callable[[str], Awaitable[None]]


class OpencodeClient:
    """opencode server 的 HTTP 客户端，支持同步与流式两种调用方式。"""

    def __init__(self, base_url: str, password: str = "", timeout: float = 600.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            auth=self._auth(password),
        )

    @staticmethod
    def _auth(password: str) -> tuple[str, str] | None:
        if password:
            return ("opencode", password)
        return None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- 健康检查 ----
    async def health(self) -> dict[str, Any]:
        r = await self._client.get("/global/health")
        r.raise_for_status()
        return r.json()

    # ---- 会话管理 ----
    async def create_session(self, title: str | None = None) -> str:
        """创建新会话，返回 sessionID。"""
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        r = await self._client.post("/session", json=body)
        r.raise_for_status()
        data = r.json()
        sid = data.get("id") if isinstance(data, dict) else None
        if not sid:
            raise OpencodeError(f"创建会话失败：响应无 id 字段 — {r.text[:300]}")
        logger.info("[opencode] 创建会话 %s (title=%s)", sid, title)
        return sid

    async def list_sessions(self) -> list[dict[str, Any]]:
        r = await self._client.get("/session")
        r.raise_for_status()
        return r.json()

    async def list_all_sessions(self) -> list[dict[str, Any]]:
        """列出所有项目的会话（跨项目），按时间倒序。

        opencode 的 GET /session 只返回当前项目的会话。
        本方法直接查 SQLite 数据库拿到所有项目的会话 ID，
        再通过 API 逐个获取详情（含标题、消息等）。
        """
        import sqlite3
        from pathlib import Path
        # opencode 数据库路径查找
        # 1. XDG_STATE_HOME 环境变量
        # 2. ~/.local/share/opencode/opencode.db (CLI serve 默认)
        # 3. ~/AppData/Roaming/ai.opencode.desktop/opencode/opencode.db (桌面 app)
        candidates = []
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        if xdg:
            candidates.append(Path(xdg) / "opencode.db")
        candidates.extend([
            Path.home() / ".local" / "share" / "opencode" / "opencode.db",
            Path.home() / "AppData" / "Roaming" / "ai.opencode.desktop" / "opencode" / "opencode.db",
        ])
        db_path = next((p for p in candidates if p.exists()), None)
        if db_path is None:
            logger.warning("[opencode] 找不到 opencode.db（尝试过 %s），回退到当前项目会话", candidates)
            return await self.list_sessions()

        # 查所有会话 ID（按时间倒序）
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, directory, project_id, time_created FROM session WHERE time_archived IS NULL ORDER BY time_created DESC LIMIT 30"
            ).fetchall()
        finally:
            conn.close()

        # 通过 API 批量获取详情（并行）
        session_ids = [r["id"] for r in rows]
        async def _get(sid):
            try:
                r = await self._client.get(f"/session/{sid}")
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            # API 失败时用 DB 数据兜底
            row = next(r for r in rows if r["id"] == sid)
            return {"id": sid, "title": row["title"], "directory": row["directory"], "project_id": row["project_id"]}
        results = await asyncio.gather(*[_get(sid) for sid in session_ids])
        return list(results)

    # ---- 同步消息 ----
    async def send_prompt(
        self,
        session_id: str,
        text: str,
        provider_id: str = "",
        model_id: str = "",
        agent: str = "",
    ) -> str:
        """向会话发送 prompt 并等待 AI 回复，返回回复的文本内容（同步接口）。"""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        elif provider_id:
            body["model"] = {"providerID": provider_id}
        if agent:
            body["agent"] = agent

        r = await self._client.post(f"/session/{session_id}/message", json=body)
        r.raise_for_status()
        data = r.json()

        parts = data.get("parts") or []
        texts: list[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                texts.append(p["text"])
        reply = "\n".join(texts).strip()
        if not reply:
            logger.warning("[opencode] 会话 %s 回复无文本 part，尝试拉取消息列表", session_id)
            reply = await self._fetch_last_assistant_text(session_id)
        return reply

    # ---- 流式消息（支持 thinking 转发）----
    async def send_prompt_streaming(
        self,
        session_id: str,
        text: str,
        provider_id: str = "",
        model_id: str = "",
        agent: str = "",
        on_reasoning: ThinkingCallback | None = None,
        on_text: TextCallback | None = None,
        timeout: float | None = None,
    ) -> str:
        """异步发送 prompt，通过 SSE 流式接收 reasoning 和 text 增量。

        - on_reasoning(delta): 每收到一段思考增量时回调
        - on_text(delta): 每收到一段回复增量时回调
        - 返回: 最终回复的完整文本

        实现方式：POST /session/:id/prompt_async（返回 204，不等结果），
        同时监听 GET /event SSE 流，过滤本 session 的事件，
        直到收到 session.idle 表示完成。
        """
        to = timeout or self._timeout

        # 1. 先启动 SSE 监听（确保不漏早期事件）
        sse_ready = asyncio.Event()
        done = asyncio.Event()
        full_text_parts: list[str] = []
        current_part_type: str | None = None  # "reasoning" | "text"

        async def listen_sse():
            async with self._client.stream("GET", "/event") as resp:
                sse_ready.set()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    etype = data.get("type", "")
                    props = data.get("properties", {})
                    ssid = props.get("sessionID") or props.get("sessionId") or ""
                    if ssid != session_id:
                        continue

                    if etype == "message.part.updated":
                        p = props.get("part", {}) or {}
                        current_part_type = p.get("type", "")

                    elif etype == "message.part.delta":
                        field = props.get("field", "")
                        delta = props.get("delta", "")
                        if not delta:
                            continue
                        # field="text" 的 delta 在 reasoning 阶段属于思考，
                        # 在 text 阶段属于最终回复
                        if field == "text":
                            if current_part_type == "reasoning":
                                if on_reasoning:
                                    try:
                                        await on_reasoning(delta)
                                    except Exception as e:
                                        logger.warning("[opencode] reasoning 回调异常: %s", e)
                            elif current_part_type == "text":
                                full_text_parts.append(delta)
                                if on_text:
                                    try:
                                        await on_text(delta)
                                    except Exception as e:
                                        logger.warning("[opencode] text 回调异常: %s", e)

                    elif etype == "session.idle":
                        done.set()
                        return

        listen_task = asyncio.create_task(listen_sse())
        try:
            await asyncio.wait_for(sse_ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            listen_task.cancel()
            raise OpencodeError("SSE 连接超时")

        # 2. 异步发送 prompt
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        elif provider_id:
            body["model"] = {"providerID": provider_id}
        if agent:
            body["agent"] = agent

        r = await self._client.post(f"/session/{session_id}/prompt_async", json=body)
        if r.status_code not in (200, 204):
            raise OpencodeError(f"prompt_async 失败：{r.status_code} {r.text[:300]}")

        # 3. 等待 session.idle
        try:
            await asyncio.wait_for(done.wait(), timeout=to)
        except asyncio.TimeoutError:
            await self.abort_session(session_id)
            raise OpencodeTimeout(f"opencode 处理超时（{to}s），已中止会话")

        return "".join(full_text_parts).strip()

    async def _fetch_last_assistant_text(self, session_id: str) -> str:
        """从消息列表里取最后一条 assistant 消息的文本。"""
        r = await self._client.get(f"/session/{session_id}/message", params={"limit": 5})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return ""
        for entry in reversed(data):
            if not isinstance(entry, dict):
                continue
            info = entry.get("info") or {}
            if info.get("role") != "assistant":
                continue
            texts = [p.get("text", "") for p in (entry.get("parts") or []) if isinstance(p, dict) and p.get("type") == "text"]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                return joined
        return ""

    async def fetch_last_user_message(self, session_id: str) -> str:
        """获取会话最后一条 user 消息的文本（用于会话列表摘要）。"""
        try:
            r = await self._client.get(f"/session/{session_id}/message", params={"limit": 6})
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return ""
            # 消息列表是倒序（最新在前），找第一条 user 文本
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                info = entry.get("info") or {}
                if info.get("role") != "user":
                    continue
                for p in (entry.get("parts") or []):
                    if isinstance(p, dict) and p.get("type") == "text" and p.get("text", "").strip():
                        return p["text"].strip()
            return ""
        except Exception:
            return ""

    async def abort_session(self, session_id: str) -> None:
        """中止正在运行的会话（用于超时/取消）。"""
        try:
            await self._client.post(f"/session/{session_id}/abort")
        except Exception as e:
            logger.warning("[opencode] 中止会话 %s 失败: %s", session_id, e)
