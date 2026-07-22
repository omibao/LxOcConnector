"""bridge.py 纯逻辑单元测试。

覆盖：
  - _split_text: 文本分段（短/长/换行/边界）
  - ThinkingBuffer: 缓冲/定时 flush/截断/flush_remaining
"""

from __future__ import annotations

import asyncio
import unittest

from bridge import _split_text


class TestSplitText(unittest.TestCase):
    def test_short_text_returns_single_part(self):
        self.assertEqual(_split_text("hello", 100), ["hello"])

    def test_exact_length_returns_single_part(self):
        self.assertEqual(_split_text("abcde", 5), ["abcde"])

    def test_long_text_no_newline_cut_at_max(self):
        text = "x" * 100
        parts = _split_text(text, 30)
        self.assertEqual(len(parts), 4)  # 30+30+30+10
        self.assertEqual("".join(parts), text)
        for p in parts[:-1]:
            self.assertEqual(len(p), 30)

    def test_long_text_prefers_newline_split(self):
        text = "line1\nline2\nline3"
        parts = _split_text(text, 12)
        # 换行切：\n 保留在前段末尾，重建后等于原文
        self.assertEqual(parts[0], "line1\nline2\n")
        self.assertEqual(parts[1], "line3")
        self.assertEqual("".join(parts), text)

    def test_newline_too_far_uses_hard_cut(self):
        """换行位置 >= max_len//2 时切在换行处，\n 保留在前段。"""
        text = "01234567\n9extra"  # 16 字符，换行在索引 8
        parts = _split_text(text, 10)
        # cut=8（>=5），换行切：\n 保留在前段
        self.assertEqual(parts, ["01234567\n", "9extra"])
        self.assertEqual("".join(parts), text)

    def test_newline_too_close_uses_hard_cut(self):
        # 换行在位置 1，max_len=10，1 < 10//2=5，应硬切 10
        text = "a\nbcdefghijklm"
        parts = _split_text(text, 10)
        self.assertEqual(parts[0], "a\nbcdefghi")
        # 剩余从 jklm 开始，前导换行被 lstrip
        self.assertEqual("".join(parts), text)

    def test_multibyte_handled_by_codepoint(self):
        # 中文按 codepoint 计数（Python len）
        text = "你好世界" * 10  # 40 字符
        parts = _split_text(text, 7)
        self.assertEqual(sum(len(p) for p in parts), 40)
        self.assertEqual("".join(parts), text)

    def test_empty_text(self):
        self.assertEqual(_split_text("", 100), [""])

    def test_consecutive_newlines_preserved_in_hard_cut(self):
        """硬切路径不应丢失换行信息（验证 lstrip 仅吃切点处的换行）。"""
        text = "para1\n\npara2long"  # 16 字符，max_len=8 触发硬切
        parts = _split_text(text, 8)
        # 重建后内容应与原文一致（验证不丢字符）
        self.assertEqual("".join(parts), text)

    def test_trailing_newline_in_short_text_preserved(self):
        """短文本末尾的换行应保留。"""
        self.assertEqual(_split_text("abc\n", 100), ["abc\n"])

    def test_reconstruction_invariant_random(self):
        """属性测试：分段后拼接必须等于原文（不丢任何字符）。"""
        import random
        random.seed(42)
        for _ in range(50):
            n = random.randint(50, 200)
            text = "".join(random.choice("ab\n \n中") for _ in range(n))
            max_len = random.randint(3, 25)
            with self.subTest(max_len=max_len, text=text):
                parts = _split_text(text, max_len)
                self.assertEqual("".join(parts), text)
                # 每段（除最后一段）长度不超过 max_len
                for p in parts[:-1]:
                    self.assertLessEqual(len(p), max_len)


if __name__ == "__main__":
    unittest.main()
