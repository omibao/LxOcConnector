"""鉴权逻辑单元测试。

覆盖场景：
  1. 默认安全：allow_all_users=False 且 allowed_users 为空 → 拒绝所有人（修复鉴权绕过）
  2. allow_all_users=True → 允许任意用户
  3. allowed_users 包含目标用户 → 允许
  4. allowed_users 不包含目标用户 → 拒绝
"""

from __future__ import annotations

import unittest

from config import Config


def _make_cfg(allow_all_users: bool, allowed_users: list[str]) -> Config:
    """构造最小可用的 Config 实例（仅鉴权相关字段有意义）。"""
    return Config(
        lansenger_app_id="",
        lansenger_app_secret="",
        lansenger_api_gateway_url="",
        opencode_base_url="",
        opencode_server_password="",
        opencode_model_provider="",
        opencode_model_id="",
        opencode_timeout=0,
        send_thinking=False,
        thinking_flush_interval=0,
        require_mention=False,
        allow_all_users=allow_all_users,
        allowed_users=allowed_users,
        session_persistence=False,
        max_message_length=0,
        log_level="INFO",
    )


class TestIsUserAllowed(unittest.TestCase):
    def test_default_secure_rejects_all_when_no_allowlist(self):
        """修复点：allow_all_users=False 且 allowed_users 为空时，必须拒绝所有人。"""
        cfg = _make_cfg(allow_all_users=False, allowed_users=[])
        self.assertFalse(_is_user_allowed(cfg, "user1"))
        self.assertFalse(_is_user_allowed(cfg, ""))
        self.assertFalse(_is_user_allowed(cfg, "any-random-id"))

    def test_allow_all_users_true_lets_everyone_in(self):
        cfg = _make_cfg(allow_all_users=True, allowed_users=[])
        self.assertTrue(_is_user_allowed(cfg, "user1"))
        self.assertTrue(_is_user_allowed(cfg, "anyone"))

    def test_allowlist_grants_listed_user(self):
        cfg = _make_cfg(allow_all_users=False, allowed_users=["user1", "user2"])
        self.assertTrue(_is_user_allowed(cfg, "user1"))
        self.assertTrue(_is_user_allowed(cfg, "user2"))

    def test_allowlist_denies_unlisted_user(self):
        cfg = _make_cfg(allow_all_users=False, allowed_users=["user1"])
        self.assertFalse(_is_user_allowed(cfg, "user2"))
        self.assertFalse(_is_user_allowed(cfg, "intruder"))


def _is_user_allowed(cfg: Config, sender_id: str) -> bool:
    """测试入口：委托给 Config 的实现。"""
    return cfg.is_user_allowed(sender_id)


if __name__ == "__main__":
    unittest.main()
