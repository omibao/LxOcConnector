"""蓝信 ↔ opencode 桥接服务 — 主入口。

用法：
    1. 复制 .env.example 为 .env，填写蓝信凭证和 opencode 地址
    2. 确保本机已运行 `opencode serve`（默认 http://localhost:4096）
    3. pip install -r requirements.txt
    4. python main.py

架构：
    蓝信云 ←(WebSocket长连)← 本桥接进程 ←(HTTP)→ opencode serve(本机)
    本机主动连出蓝信，无需公网IP/域名/回调
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from lansenger_sdk import LansengerClient

from bridge import Bridge
from config import Config
from lansenger_inbound import LansengerInbound
from opencode_client import OpencodeClient

logger = logging.getLogger("lanxin-opencode")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # lansenger-sdk 和 websockets 噪音太大，降一级
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def check_opencode(oc: OpencodeClient) -> bool:
    """启动前检查 opencode 服务是否可达。"""
    try:
        h = await oc.health()
        logger.info("✅ opencode 服务可达：version=%s", h.get("version", "?"))
        return True
    except Exception as e:
        logger.error("❌ 无法连接 opencode 服务（%s）", e)
        logger.error("   请确认已运行 `opencode serve`（默认 http://localhost:4096）")
        return False


async def main_async() -> int:
    cfg = Config.load()
    errors = cfg.validate()
    if errors:
        for e in errors:
            logger.error("配置错误：%s", e)
        logger.error("请参考 .env.example 填写 .env 后重试")
        return 2

    # 初始化各组件
    oc = OpencodeClient(cfg.opencode_base_url, cfg.opencode_server_password)
    if not await check_opencode(oc):
        await oc.aclose()
        return 1

    # lansenger-sdk 的异步客户端（出站：发消息）
    ls = LansengerClient(
        app_id=cfg.lansenger_app_id,
        app_secret=cfg.lansenger_app_secret,
        api_gateway_url=cfg.lansenger_api_gateway_url,
    )

    bridge = Bridge(cfg, oc, ls)

    # 入站监听器（收消息 → bridge.handle）
    inbound = LansengerInbound(
        app_id=cfg.lansenger_app_id,
        app_secret=cfg.lansenger_app_secret,
        api_gateway_url=cfg.lansenger_api_gateway_url,
        on_message=bridge.handle,
        require_mention=cfg.require_mention,
    )

    # 启动蓝信入站监听（WebSocket 长连接）
    await inbound.start()

    # 持续运行，直到收到 Ctrl+C
    try:
        await asyncio.Event().wait()  # 永久阻塞，直到被取消
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("正在关闭...")
        await inbound.stop()
        await oc.aclose()
        logger.info("已退出")
    return 0


def main() -> None:
    # 先加载配置确定日志级别
    cfg = Config.load()
    setup_logging(cfg.log_level)
    try:
        rc = asyncio.run(main_async())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
