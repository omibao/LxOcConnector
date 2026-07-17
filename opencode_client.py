"""opencode 服务端 HTTP 客户端 — 封装 session/message 相关调用。

对应 opencode 的 Server API（OpenAPI 3.1）：
  - POST /session                创建会话
  - POST /session/:id/message    发送 prompt 并等待 AI 回复（同步）
  - GET  /session                列出会话
  - GET  /session/:id/message    列出会话消息
  - GET  /global/health          健康检查
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OpencodeError(Exception):
    pass


class OpencodeClient:
    """opencode server 的最小 HTTP 客户端。"""

    def __init__(self, base_url: str, password: str = "", timeout: float = 300.0):
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
        await self._http().aclose()

    def _http(self) -> httpx.AsyncClient:
        # 如果上层在断连后重建过客户端，这里支持替换
        return getattr(self, "_client", None) or httpx.AsyncClient()

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
        # opencode 返回 { id, ... }
        sid = data.get("id") if isinstance(data, dict) else None
        if not sid:
            raise OpencodeError(f"创建会话失败：响应无 id 字段 — {r.text[:300]}")
        logger.info("[opencode] 创建会话 %s (title=%s)", sid, title)
        return sid

    async def list_sessions(self) -> list[dict[str, Any]]:
        r = await self._client.get("/session")
        r.raise_for_status()
        return r.json()

    # ---- 消息 ----
    async def send_prompt(
        self,
        session_id: str,
        text: str,
        provider_id: str = "",
        model_id: str = "",
        agent: str = "",
    ) -> str:
        """向会话发送 prompt 并等待 AI 回复，返回回复的文本内容。

        opencode 的 POST /session/:id/message 是同步接口——会等到 AI 完成
        才返回 { info, parts }。我们在 parts 里提取所有 type==text 的文本拼接。
        """
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

        # data 结构：{ info: {...}, parts: [ {type, text}, {type, ...}, ... ] }
        parts = data.get("parts") or []
        texts: list[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                texts.append(p["text"])
        reply = "\n".join(texts).strip()
        if not reply:
            # 可能是工具调用结束但无文本输出；尝试读取消息列表兜底
            logger.warning("[opencode] 会话 %s 回复无文本 part，尝试拉取消息列表", session_id)
            reply = await self._fetch_last_assistant_text(session_id)
        return reply

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

    async def abort_session(self, session_id: str) -> None:
        """中止正在运行的会话（用于超时/取消）。"""
        try:
            await self._client.post(f"/session/{session_id}/abort")
        except Exception as e:
            logger.warning("[opencode] 中止会话 %s 失败: %s", session_id, e)
