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

    async def get_session_status(self, session_id: str) -> str:
        """获取会话状态：'idle' / 'busy' / 'unknown'。"""
        try:
            r = await self._client.get("/session/status")
            r.raise_for_status()
            statuses = r.json()
            status = statuses.get(session_id, {})
            return status.get("type", "idle")
        except Exception:
            return "unknown"

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
        """列出所有项目的会话（跨项目），含最后一条用户消息摘要。

        opencode 的 GET /session 只返回当前项目的会话。
        本方法直接查 SQLite 数据库，一条 SQL 拿到所有会话 + 最后一条 user 消息文本，
        不再逐个调 API（避免 limit 不够导致摘要为空）。
        """
        import sqlite3
        import json
        from pathlib import Path
        # opencode 数据库路径查找
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

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT s.id, s.title, s.directory, s.project_id,
                       s.time_created, s.time_updated,
                       (
                           SELECT p.data FROM part p
                           JOIN message m2 ON p.message_id = m2.id
                           WHERE m2.session_id = s.id
                             AND json_extract(m2.data, '$.role') = 'user'
                             AND json_extract(p.data, '$.type') = 'text'
                           ORDER BY m2.time_created DESC
                           LIMIT 1
                       ) as last_user_part
                FROM session s
                WHERE s.time_archived IS NULL
                ORDER BY s.time_updated DESC
                LIMIT 50
            """).fetchall()
        finally:
            conn.close()

        results: list[dict[str, Any]] = []
        for r in rows:
            last_text = ""
            if r["last_user_part"]:
                try:
                    last_text = json.loads(r["last_user_part"]).get("text", "")
                except Exception:
                    pass
            results.append({
                "id": r["id"],
                "title": r["title"],
                "directory": r["directory"],
                "projectID": r["project_id"],
                "time": {"created": r["time_created"], "updated": r["time_updated"]},
                "last_user_text": last_text,
            })
        return results

    # ---- 同步消息 ----
    async def send_prompt(
        self,
        session_id: str,
        text: str,
        provider_id: str = "",
        model_id: str = "",
        agent: str = "",
        timeout: float | None = None,
    ) -> str:
        """向会话发送 prompt 并等待 AI 回复，返回回复的文本内容（同步接口）。

        如果会话 busy，opencode 会阻塞等待——本方法用 timeout 防止永久卡住。
        """
        to = timeout or self._timeout
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        elif provider_id:
            body["model"] = {"providerID": provider_id}
        if agent:
            body["agent"] = agent

        try:
            r = await asyncio.wait_for(
                self._client.post(f"/session/{session_id}/message", json=body),
                timeout=to,
            )
        except asyncio.TimeoutError:
            await self.abort_session(session_id)
            raise OpencodeTimeout(f"opencode 处理超时（{to}s），已中止会话")
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
            try:
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
            except Exception as e:
                logger.warning("[opencode] SSE 连接异常: %s", e)
            finally:
                # SSE 断开时（网络抖动、服务重启等）通知主流程
                # 主流程会回退到同步 API 取结果
                sse_ready.set()
                if not done.is_set():
                    done.set()
                    self._sse_disconnected = True

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

        # 3. 等待 session.idle（或 SSE 断开 / 超时）
        self._sse_disconnected = False
        try:
            await asyncio.wait_for(done.wait(), timeout=to)
        except asyncio.TimeoutError:
            await self.abort_session(session_id)
            raise OpencodeTimeout(f"opencode 处理超时（{to}s），已中止会话")
        finally:
            if not listen_task.done():
                listen_task.cancel()
                try:
                    await listen_task
                except asyncio.CancelledError:
                    pass

        # SSE 断开但可能 opencode 已完成——回退到同步 API 取结果
        if self._sse_disconnected:
            logger.warning("[opencode] SSE 断开，回退同步 API 取结果")
            # 轮询 session 状态，等 opencode 处理完
            for _ in range(int(to)):
                try:
                    r = await self._client.get("/session/status")
                    statuses = r.json()
                    status = statuses.get(session_id, {})
                    if status.get("type") == "idle":
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
            # 取最后一条 assistant 消息
            reply = await self._fetch_last_assistant_text(session_id)
            return reply

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
            r = await self._client.get(f"/session/{session_id}/message", params={"limit": 50})
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

    async def fetch_history(self, session_id: str, limit: int = 5) -> list[dict[str, str]]:
        """获取会话最近的对话历史，按"轮"分组返回正序列表。

        一轮 = 一条 user 消息 + 后续所有 assistant 消息（直到下一条 user）。
        assistant 的多段文本合并为一条。
        limit 是轮数（不是 message 条数）。

        返回: [{role: "user", text: "..."}, {role: "assistant", text: "..."}, ...]
        """
        # 取足够多的 message 来凑出 limit 轮对话
        # 一轮可能含 10+ 条 assistant message（tool call 中间步骤），多取些
        r = await self._client.get(f"/session/{session_id}/message", params={"limit": limit * 20})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []

        # 先提取有文本的 user/assistant message，保持正序（最旧在前）
        msgs: list[dict[str, str]] = []
        for entry in reversed(data):
            if not isinstance(entry, dict):
                continue
            info = entry.get("info") or {}
            role = info.get("role", "")
            if role not in ("user", "assistant"):
                continue
            texts = [
                p["text"]
                for p in (entry.get("parts") or [])
                if isinstance(p, dict) and p.get("type") == "text" and p.get("text", "").strip()
            ]
            if not texts:
                continue
            msgs.append({"role": role, "text": "\n".join(texts)})

        # 按 user 消息分组：每个 user 开启一轮，后续 assistant 归入该轮
        rounds: list[list[dict[str, str]]] = []
        current: list[dict[str, str]] = []
        for m in msgs:
            if m["role"] == "user":
                if current:
                    rounds.append(current)
                current = [m]
            else:  # assistant
                if not current:
                    # 没有前置 user 的 assistant（如系统消息），单独成轮
                    current = [m]
                else:
                    current.append(m)
        if current:
            rounds.append(current)

        # 取最近 limit 轮，展平为 user/assistant 交替列表
        recent = rounds[-limit:]
        result: list[dict[str, str]] = []
        for rnd in recent:
            # user 提问
            user_texts = [m["text"] for m in rnd if m["role"] == "user"]
            if user_texts:
                result.append({"role": "user", "text": "\n".join(user_texts)})
            # assistant 回答（合并所有 assistant 文本）
            asst_texts = [m["text"] for m in rnd if m["role"] == "assistant"]
            if asst_texts:
                result.append({"role": "assistant", "text": "\n".join(asst_texts)})
        return result

    async def abort_session(self, session_id: str) -> None:
        """中止正在运行的会话（用于超时/取消）。"""
        try:
            await self._client.post(f"/session/{session_id}/abort")
        except Exception as e:
            logger.warning("[opencode] 中止会话 %s 失败: %s", session_id, e)
