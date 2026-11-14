from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析，避免额外依赖。已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


@dataclass
class Config:
    app_id: str
    app_secret: str
    allow_open_ids: list[str] = field(default_factory=list)
    allow_chat_ids: list[str] = field(default_factory=list)
    transport: str = "http"
    agent_url: str = ""
    agent_token: str = ""
    ssh_target: str = ""
    ssh_command: str = "claude -p --output-format text"
    local_command: str = "claude -p --output-format text"
    agent_timeout: int = 900
    workers: int = 4
    max_chunk_chars: int = 3500
    max_chunks: int = 12
    state_dir: str = "./state"
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Config":
        _load_dotenv(Path(env_file))
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise SystemExit(
                "缺少 FEISHU_APP_ID / FEISHU_APP_SECRET。请复制 .env.example 为 .env 并填写。"
            )
        transport = os.environ.get("AGENT_TRANSPORT", "http").strip().lower()
        if transport not in ("http", "ssh", "local"):
            raise SystemExit(f"AGENT_TRANSPORT 只能是 http/ssh/local，当前是 {transport}")
        if transport == "http" and not os.environ.get("AGENT_URL", "").strip():
            raise SystemExit("AGENT_TRANSPORT=http 时必须填写 AGENT_URL")
        if transport == "ssh" and not os.environ.get("SSH_TARGET", "").strip():
            raise SystemExit("AGENT_TRANSPORT=ssh 时必须填写 SSH_TARGET")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            allow_open_ids=_csv("ALLOW_OPEN_IDS"),
            allow_chat_ids=_csv("ALLOW_CHAT_IDS"),
            transport=transport,
            agent_url=os.environ.get("AGENT_URL", "").strip(),
            agent_token=os.environ.get("AGENT_TOKEN", "").strip(),
            ssh_target=os.environ.get("SSH_TARGET", "").strip(),
            ssh_command=os.environ.get("SSH_COMMAND", cls.ssh_command).strip(),
            local_command=os.environ.get("LOCAL_COMMAND", cls.local_command).strip(),
            agent_timeout=int(os.environ.get("AGENT_TIMEOUT", "900")),
            workers=int(os.environ.get("WORKERS", "4")),
            max_chunk_chars=int(os.environ.get("MAX_CHUNK_CHARS", "3500")),
            max_chunks=int(os.environ.get("MAX_CHUNKS", "12")),
            state_dir=os.environ.get("STATE_DIR", "./state"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir)
