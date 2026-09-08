import os
import yaml
from pathlib import Path
from typing import List, Optional

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))

class Config:
    def __init__(self, data: dict):
        self.bot_token: str = data.get("bot_token", "")
        self.admin_chat_id: Optional[int] = data.get("admin_chat_id")
        self.listen_host: str = data.get("listen_host", "127.0.0.1")
        self.listen_port: int = int(data.get("listen_port", 8765))
        self.protected_proxies: List[str] = data.get("protected_proxies", ["phone-ssh", "phone"])
        self.approval_timeout: int = int(data.get("approval_timeout", 30))
        self.whitelist_duration: int = int(data.get("whitelist_duration", 1800)) # 30分钟

        # 代理配置与智能嗅探：显式配置 > 环境变量 > 本地代理端口 (7890, 40000)
        proxy_candidate = (
            data.get("telegram_proxy")
            or os.environ.get("TELEGRAM_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
        )
        if not proxy_candidate:
            import socket
            for port, proto in ((7890, "http"), (40000, "socks5")):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                        proxy_candidate = f"{proto}://127.0.0.1:{port}"
                        break
                except (OSError, socket.timeout):
                    pass
        self.telegram_proxy: Optional[str] = proxy_candidate

        # 自动更新配置
        update_data = data.get("auto_update", {})
        self.auto_update_enabled: bool = update_data.get("enabled", False)
        self.auto_update_interval: int = int(update_data.get("interval_seconds", 300))
        self.git_branch: str = update_data.get("branch", "main")

def load_config() -> Config:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    # 支持环境变量覆盖
    if "BOT_TOKEN" in os.environ:
        data["bot_token"] = os.environ["BOT_TOKEN"]
    if "ADMIN_CHAT_ID" in os.environ:
        data["admin_chat_id"] = int(os.environ["ADMIN_CHAT_ID"])
    if "LISTEN_PORT" in os.environ:
        data["listen_port"] = int(os.environ["LISTEN_PORT"])

    return Config(data)

config = load_config()
