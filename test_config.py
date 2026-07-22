"""config.py 的单元测试。

覆盖：
  - _load_env_file: .env 文件解析（BOM/注释/引号/已存在变量不覆盖）
  - Config.load: 字段映射与默认值
  - Config.validate: 必填校验
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from config import Config, _load_env_file


# 测试中可能被污染的环境变量键
_ENV_KEYS = [
    "LANSENGER_APP_ID", "LANSENGER_APP_SECRET", "LANSENGER_API_GATEWAY_URL",
    "OPENCODE_BASE_URL", "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_MODEL_PROVIDER", "OPENCODE_MODEL_ID", "OPENCODE_TIMEOUT",
    "SEND_THINKING", "THINKING_FLUSH_INTERVAL",
    "REQUIRE_MENTION", "ALLOW_ALL_USERS", "ALLOWED_USERS",
    "SESSION_PERSISTENCE", "MAX_MESSAGE_LENGTH", "LOG_LEVEL",
]


def _clear_env() -> None:
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


class TestLoadEnvFile(unittest.TestCase):
    def setUp(self):
        _clear_env()

    def tearDown(self):
        _clear_env()

    def test_nonexistent_file_is_silent(self):
        _load_env_file("/no/such/path/.env")
        # 不抛异常即可

    def test_basic_key_value(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("FOO_TEST_KEY=hello\n")
            path = f.name
        try:
            _load_env_file(path)
            self.assertEqual(os.environ.get("FOO_TEST_KEY"), "hello")
        finally:
            os.unlink(path)
            os.environ.pop("FOO_TEST_KEY", None)

    def test_skips_blank_and_comment_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("# 注释行\n\n   \nBAR_TEST_KEY=42\n")
            path = f.name
        try:
            _load_env_file(path)
            self.assertEqual(os.environ.get("BAR_TEST_KEY"), "42")
        finally:
            os.unlink(path)
            os.environ.pop("BAR_TEST_KEY", None)

    def test_strips_quotes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write('Q1="double"\nQ2=\'single\'\nQ3=plain\n')
            path = f.name
        try:
            _load_env_file(path)
            self.assertEqual(os.environ.get("Q1"), "double")
            self.assertEqual(os.environ.get("Q2"), "single")
            self.assertEqual(os.environ.get("Q3"), "plain")
        finally:
            os.unlink(path)
            for k in ("Q1", "Q2", "Q3"):
                os.environ.pop(k, None)

    def test_does_not_overwrite_existing_env(self):
        os.environ["EXISTING_TEST_KEY"] = "from-env"
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("EXISTING_TEST_KEY=from-file\n")
            path = f.name
        try:
            _load_env_file(path)
            self.assertEqual(os.environ.get("EXISTING_TEST_KEY"), "from-env")
        finally:
            os.unlink(path)
            os.environ.pop("EXISTING_TEST_KEY", None)

    def test_handles_utf8_bom(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".env", delete=False) as f:
            f.write("\ufeffBOM_TEST_KEY=value\n".encode("utf-8"))
            path = f.name
        try:
            _load_env_file(path)
            self.assertEqual(os.environ.get("BOM_TEST_KEY"), "value")
        finally:
            os.unlink(path)
            os.environ.pop("BOM_TEST_KEY", None)

    def test_line_without_equals_is_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("NO_EQUAL_SIGN\nKEY=val\n")
            path = f.name
        try:
            _load_env_file(path)
            self.assertNotIn("NO_EQUAL_SIGN", os.environ)
            self.assertEqual(os.environ.get("KEY"), "val")
        finally:
            os.unlink(path)
            os.environ.pop("KEY", None)


class TestConfigLoad(unittest.TestCase):
    def setUp(self):
        _clear_env()

    def tearDown(self):
        _clear_env()

    def test_defaults_when_empty(self):
        cfg = Config.load(env_path="/no/such/.env")
        self.assertEqual(cfg.lansenger_app_id, "")
        self.assertEqual(cfg.lansenger_api_gateway_url, "https://open.e.lanxin.cn/open/apigw")
        self.assertEqual(cfg.opencode_base_url, "http://localhost:4096")
        self.assertEqual(cfg.opencode_timeout, 300)
        self.assertTrue(cfg.send_thinking)
        self.assertTrue(cfg.require_mention)
        self.assertFalse(cfg.allow_all_users)
        self.assertTrue(cfg.session_persistence)
        self.assertEqual(cfg.max_message_length, 4000)
        self.assertEqual(cfg.log_level, "INFO")
        self.assertEqual(cfg.allowed_users, [])

    def test_reads_from_env_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write(
                "LANSENGER_APP_ID=app123\n"
                "LANSENGER_APP_SECRET=secret456\n"
                "OPENCODE_TIMEOUT=120\n"
                "ALLOW_ALL_USERS=true\n"
                "ALLOWED_USERS=u1,u2,u3\n"
                "LOG_LEVEL=debug\n"
            )
            path = f.name
        try:
            cfg = Config.load(env_path=path)
            self.assertEqual(cfg.lansenger_app_id, "app123")
            self.assertEqual(cfg.lansenger_app_secret, "secret456")
            self.assertEqual(cfg.opencode_timeout, 120)
            self.assertTrue(cfg.allow_all_users)
            self.assertEqual(cfg.allowed_users, ["u1", "u2", "u3"])
            self.assertEqual(cfg.log_level, "DEBUG")
        finally:
            os.unlink(path)

    def test_invalid_int_falls_back_to_default(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("OPENCODE_TIMEOUT=not-a-number\n")
            path = f.name
        try:
            cfg = Config.load(env_path=path)
            self.assertEqual(cfg.opencode_timeout, 300)
        finally:
            os.unlink(path)

    def test_bool_variants(self):
        for truthy in ("1", "true", "TRUE", "Yes", "on", "ON"):
            with self.subTest(truthy=truthy):
                _clear_env()
                with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
                    f.write(f"ALLOW_ALL_USERS={truthy}\n")
                    path = f.name
                try:
                    cfg = Config.load(env_path=path)
                    self.assertTrue(cfg.allow_all_users, f"{truthy} 应为 True")
                finally:
                    os.unlink(path)
        for falsy in ("0", "false", "no", "off", "anything"):
            with self.subTest(falsy=falsy):
                _clear_env()
                with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
                    f.write(f"ALLOW_ALL_USERS={falsy}\n")
                    path = f.name
                try:
                    cfg = Config.load(env_path=path)
                    self.assertFalse(cfg.allow_all_users, f"{falsy} 应为 False")
                finally:
                    os.unlink(path)

    def test_allowed_users_strips_whitespace(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("ALLOWED_USERS=  u1 , u2 ,  u3  \n")
            path = f.name
        try:
            cfg = Config.load(env_path=path)
            self.assertEqual(cfg.allowed_users, ["u1", "u2", "u3"])
        finally:
            os.unlink(path)

    def test_opencode_base_url_trailing_slash_stripped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("OPENCODE_BASE_URL=http://localhost:4096////\n")
            path = f.name
        try:
            cfg = Config.load(env_path=path)
            self.assertEqual(cfg.opencode_base_url, "http://localhost:4096")
        finally:
            os.unlink(path)


class TestConfigValidate(unittest.TestCase):
    def test_empty_config_has_all_required_errors(self):
        cfg = Config(
            lansenger_app_id="", lansenger_app_secret="", lansenger_api_gateway_url="",
            opencode_base_url="", opencode_server_password="",
            opencode_model_provider="", opencode_model_id="",
            opencode_timeout=0, send_thinking=False, thinking_flush_interval=0,
            require_mention=False, allow_all_users=False, allowed_users=[],
            session_persistence=False, max_message_length=0, log_level="INFO",
        )
        errors = cfg.validate()
        # 4 个必填字段 + 鉴权 fail-closed
        self.assertIn("LANSENGER_APP_ID 未配置", errors)
        self.assertIn("LANSENGER_APP_SECRET 未配置", errors)
        self.assertIn("LANSENGER_API_GATEWAY_URL 未配置", errors)
        self.assertIn("OPENCODE_BASE_URL 未配置", errors)
        self.assertTrue(any("ALLOW_ALL_USERS" in e for e in errors))

    def test_full_config_no_errors(self):
        cfg = Config(
            lansenger_app_id="id", lansenger_app_secret="secret",
            lansenger_api_gateway_url="https://gw", opencode_base_url="http://localhost:4096",
            opencode_server_password="", opencode_model_provider="", opencode_model_id="",
            opencode_timeout=300, send_thinking=True, thinking_flush_interval=3,
            require_mention=True, allow_all_users=False, allowed_users=["u1"],
            session_persistence=True, max_message_length=4000, log_level="INFO",
        )
        self.assertEqual(cfg.validate(), [])

    def test_allow_all_users_skips_allowlist_check(self):
        cfg = Config(
            lansenger_app_id="id", lansenger_app_secret="secret",
            lansenger_api_gateway_url="https://gw", opencode_base_url="http://localhost:4096",
            opencode_server_password="", opencode_model_provider="", opencode_model_id="",
            opencode_timeout=300, send_thinking=True, thinking_flush_interval=3,
            require_mention=True, allow_all_users=True, allowed_users=[],
            session_persistence=True, max_message_length=4000, log_level="INFO",
        )
        self.assertEqual(cfg.validate(), [])


if __name__ == "__main__":
    unittest.main()
