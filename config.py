"""配置加载 — 从环境变量 / .env 文件读取所有参数。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file(env_path: str | Path) -> None:
    """简易 .env 加载器（无需 python-dotenv 依赖）。"""
    p = Path(env_path)
    if not p.exists():
        return
    # utf-8-sig 自动去掉可能的 BOM（PowerShell Set-Content -Encoding utf8 会加 BOM）
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # 去掉可选的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    # 蓝信
    lansenger_app_id: str
    lansenger_app_secret: str
    lansenger_api_gateway_url: str
    # opencode
    opencode_base_url: str
    opencode_server_password: str
    opencode_model_provider: str
    opencode_model_id: str
    opencode_timeout: int
    # 流式 thinking 转发
    send_thinking: bool
    thinking_flush_interval: int
    # 行为
    require_mention: bool
    allow_all_users: bool
    allowed_users: list[str]
    session_persistence: bool
    max_message_length: int
    log_level: str

    @classmethod
    def load(cls, env_path: str | Path | None = None) -> "Config":
        if env_path is None:
            env_path = Path(__file__).resolve().parent / ".env"
        _load_env_file(env_path)

        def _bool(key: str, default: bool) -> bool:
            return os.environ.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

        def _int(key: str, default: int) -> int:
            try:
                return int(os.environ.get(key, str(default)))
            except ValueError:
                return default

        allowed_raw = os.environ.get("ALLOWED_USERS", "").strip()
        allowed = [u.strip() for u in allowed_raw.split(",") if u.strip()]

        return cls(
            lansenger_app_id=os.environ.get("LANSENGER_APP_ID", "").strip(),
            lansenger_app_secret=os.environ.get("LANSENGER_APP_SECRET", "").strip(),
            lansenger_api_gateway_url=os.environ.get(
                "LANSENGER_API_GATEWAY_URL", "https://open.e.lanxin.cn/open/apigw"
            ).strip(),
            opencode_base_url=os.environ.get("OPENCODE_BASE_URL", "http://localhost:4096").strip().rstrip("/"),
            opencode_server_password=os.environ.get("OPENCODE_SERVER_PASSWORD", "").strip(),
            opencode_model_provider=os.environ.get("OPENCODE_MODEL_PROVIDER", "").strip(),
            opencode_model_id=os.environ.get("OPENCODE_MODEL_ID", "").strip(),
            opencode_timeout=_int("OPENCODE_TIMEOUT", 600),
            send_thinking=_bool("SEND_THINKING", True),
            thinking_flush_interval=_int("THINKING_FLUSH_INTERVAL", 3),
            require_mention=_bool("REQUIRE_MENTION", True),
            allow_all_users=_bool("ALLOW_ALL_USERS", False),
            allowed_users=allowed,
            session_persistence=_bool("SESSION_PERSISTENCE", True),
            max_message_length=_int("MAX_MESSAGE_LENGTH", 4000),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )

    def validate(self) -> list[str]:
        """返回错误信息列表；空列表表示配置完整。"""
        errors: list[str] = []
        if not self.lansenger_app_id:
            errors.append("LANSENGER_APP_ID 未配置")
        if not self.lansenger_app_secret:
            errors.append("LANSENGER_APP_SECRET 未配置")
        if not self.lansenger_api_gateway_url:
            errors.append("LANSENGER_API_GATEWAY_URL 未配置")
        if not self.opencode_base_url:
            errors.append("OPENCODE_BASE_URL 未配置")
        if not self.allow_all_users and not self.allowed_users:
            errors.append(
                "ALLOW_ALL_USERS=false 且 ALLOWED_USERS 为空 —— 默认拒绝所有人。"
                "如需开放请设置 ALLOW_ALL_USERS=true 或在 ALLOWED_USERS 中填写 openId。"
            )
        return errors

    def is_user_allowed(self, sender_id: str) -> bool:
        """判断某发送者是否被允许使用本机器人。

        安全默认（fail-closed）：当 allow_all_users=False 且 allowed_users
        为空时拒绝所有人，避免「未配置允许列表」静默退化为「允许所有人」。
        """
        if self.allow_all_users:
            return True
        if not self.allowed_users:
            return False
        return sender_id in self.allowed_users
