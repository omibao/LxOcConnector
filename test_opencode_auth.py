"""opencode_client.py 纯逻辑单元测试。

覆盖：
  - OpencodeClient._auth: HTTP Basic 鉴权元组构造
"""

from __future__ import annotations

import unittest

from opencode_client import OpencodeClient


class TestOpencodeAuth(unittest.TestCase):
    def test_password_returns_basic_auth_tuple(self):
        auth = OpencodeClient._auth("s3cret")
        self.assertEqual(auth, ("opencode", "s3cret"))

    def test_empty_password_returns_none(self):
        self.assertIsNone(OpencodeClient._auth(""))

    def test_whitespace_password_preserved(self):
        """密码应原样保留，不做 strip（避免破坏含空格的密码）。"""
        auth = OpencodeClient._auth(" pass word ")
        self.assertEqual(auth, ("opencode", " pass word "))


class TestOpencodeClientInit(unittest.TestCase):
    def test_base_url_trailing_slash_stripped(self):
        c = OpencodeClient("http://localhost:4096///", password="x")
        self.assertEqual(c._base_url, "http://localhost:4096")

    def test_base_url_no_slash_unchanged(self):
        c = OpencodeClient("http://localhost:4096", password="x")
        self.assertEqual(c._base_url, "http://localhost:4096")


if __name__ == "__main__":
    unittest.main()
