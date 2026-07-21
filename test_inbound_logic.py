"""lansenger_inbound.py 纯逻辑单元测试。

覆盖：
  - LansengerInbound._extract_text: 各 msgType 解析
  - LansengerInbound 去重逻辑（msg_id 重复时跳过）
  - 群聊回声防护（sender == botId 时跳过）
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from lansenger_inbound import InboundMessage, LansengerInbound


def _make_inbound(on_message=None) -> LansengerInbound:
    return LansengerInbound(
        app_id="x", app_secret="y", api_gateway_url="http://gw",
        on_message=on_message or (lambda m: asyncio.sleep(0)),
        require_mention=True,
    )


def _msg_data(msg_id: str, msg_type: str, payload: dict, from_: str = "user-open-id", **extra) -> dict:
    d = {
        "msgId": msg_id,
        "from": from_,
        "senderName": "张三",
        "msgType": msg_type,
        "msgData": payload,
    }
    d.update(extra)
    return d


class TestExtractText(unittest.TestCase):
    def test_text_type_returns_content(self):
        d = _msg_data("m1", "text", {"text": {"content": "你好"}})
        self.assertEqual(LansengerInbound._extract_text(d), "你好")

    def test_text_type_strips_whitespace(self):
        d = _msg_data("m1", "text", {"text": {"content": "  hi  "}})
        self.assertEqual(LansengerInbound._extract_text(d), "hi")

    def test_text_type_missing_content(self):
        d = _msg_data("m1", "text", {})
        self.assertEqual(LansengerInbound._extract_text(d), "")

    def test_format_type_with_format_key(self):
        d = _msg_data("m1", "format", {"format": {"text": "格式消息"}})
        self.assertEqual(LansengerInbound._extract_text(d), "格式消息")

    def test_formatText_type_with_formatText_key(self):
        d = _msg_data("m1", "formatText", {"formatText": {"text": "格式文本"}})
        self.assertEqual(LansengerInbound._extract_text(d), "格式文本")

    def test_unsupported_type_returns_empty(self):
        for t in ("image", "file", "voice", "card", "video"):
            with self.subTest(t=t):
                d = _msg_data("m1", t, {"something": "x"})
                self.assertEqual(LansengerInbound._extract_text(d), "")

    def test_msg_data_none_returns_empty(self):
        d = {"msgType": "text", "msgData": None}
        self.assertEqual(LansengerInbound._extract_text(d), "")

    def test_payload_text_none_returns_empty(self):
        d = {"msgType": "text", "msgData": {"text": None}}
        self.assertEqual(LansengerInbound._extract_text(d), "")

    def test_format_non_dict_returns_empty(self):
        d = {"msgType": "format", "msgData": {"format": "not a dict"}}
        self.assertEqual(LansengerInbound._extract_text(d), "")

    def test_missing_msgType_defaults_to_text(self):
        d = {"msgData": {"text": {"content": "默认"}}}
        self.assertEqual(LansengerInbound._extract_text(d), "默认")


class TestDedupAndEcho(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_msg_id_skipped(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        event = {
            "type": "bot_private_message",
            "data": _msg_data("dup-1", "text", {"text": {"content": "hi"}}),
        }
        await ib._process_event(event)
        await ib._process_event(event)
        self.assertEqual(len(received), 1)

    async def test_different_msg_ids_both_processed(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "bot_private_message",
            "data": _msg_data("id-a", "text", {"text": {"content": "a"}}),
        })
        await ib._process_event({
            "type": "bot_private_message",
            "data": _msg_data("id-b", "text", {"text": {"content": "b"}}),
        })
        self.assertEqual(len(received), 2)

    async def test_group_echo_from_self_skipped(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "bot_group_message",
            "data": _msg_data(
                "echo-1", "text", {"text": {"content": "self"}},
                from_="self-bot-id",
                botId="self-bot-id",
                groupId="g1",
                reminder={"isAtMe": True, "isAtAll": False},
            ),
        })
        self.assertEqual(received, [])

    async def test_group_message_from_other_user_processed(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "bot_group_message",
            "data": _msg_data(
                "g-1", "text", {"text": {"content": "hi"}},
                from_="other-user",
                botId="self-bot-id",
                groupId="g1",
                reminder={"isAtMe": True, "isAtAll": False},
            ),
        })
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].is_group)
        self.assertEqual(received[0].chat_id, "g1")

    async def test_group_message_without_mention_skipped_when_required(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)  # require_mention=True
        await ib._process_event({
            "type": "bot_group_message",
            "data": _msg_data(
                "g-2", "text", {"text": {"content": "hi"}},
                from_="other-user",
                botId="self-bot-id",
                groupId="g2",
                reminder={"isAtMe": False, "isAtAll": False},
            ),
        })
        self.assertEqual(received, [])

    async def test_private_message_no_mention_check(self):
        """私聊不要求 @，应正常处理。"""
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "bot_private_message",
            "data": _msg_data(
                "p-1", "text", {"text": {"content": "hi"}},
            ),
        })
        self.assertEqual(len(received), 1)
        self.assertFalse(received[0].is_group)

    async def test_message_without_text_skipped(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "bot_private_message",
            "data": _msg_data("p-2", "image", {"url": "http://x/y.png"}),
        })
        self.assertEqual(received, [])

    async def test_message_without_msg_id_skipped(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "bot_private_message",
            "data": {"from": "u", "msgType": "text", "msgData": {"text": {"content": "hi"}}},
        })
        self.assertEqual(received, [])

    async def test_unknown_event_type_ignored(self):
        received: list[InboundMessage] = []

        async def handler(m):
            received.append(m)

        ib = _make_inbound(handler)
        await ib._process_event({
            "type": "some_other_event",
            "data": _msg_data("x", "text", {"text": {"content": "hi"}}),
        })
        self.assertEqual(received, [])

    async def test_seen_cache_capped(self):
        """超过 _seen_max 时应清理过期项，不无限增长。"""
        ib = _make_inbound()
        ib._seen_max = 3  # 小阈值便于测试
        ib._seen = {"a": 0, "b": 0, "c": 0, "d": 0}  # 4 > 3
        # 触发清理逻辑：再处理一条新消息
        await ib._process_event({
            "type": "bot_private_message",
            "data": _msg_data("new", "text", {"text": {"content": "hi"}}),
        })
        # 清理后 _seen 应已剔除过期项（cutoff = now - 3600，0 是过期）
        self.assertLessEqual(len(ib._seen), ib._seen_max + 1)


if __name__ == "__main__":
    unittest.main()
