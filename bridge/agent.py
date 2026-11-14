"""远端 agent 的三种连接方式：HTTP / SSH / 本地命令。"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("bridge.agent")


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    session_id: str | None = None
    elapsed: float = 0.0


def _parse_claude_json(stdout: str) -> tuple[str, str | None]:
    """兼容 `claude -p --output-format json`：取 result 和 session_id。"""
    obj = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            obj = candidate
            break
    if obj is None:
        return stdout.strip(), None
    text = obj.get("result") or obj.get("text") or obj.get("reply") or stdout.strip()
    return str(text), obj.get("session_id")


class SessionStore:
    """chat_id → agent 侧会话 id，落盘以便重启后还能续上下文。"""

    def __init__(self, path: Path):
        self.path = Path(path) / "sessions.json"
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, chat_id: str) -> str | None:
        with self._lock:
            return self._data.get(chat_id)

    def set(self, chat_id: str, session_id: str | None) -> None:
        with self._lock:
            if session_id:
                self._data[chat_id] = session_id
            else:
                self._data.pop(chat_id, None)
            self._flush()

    def clear(self, chat_id: str) -> None:
        self.set(chat_id, None)

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("会话映射落盘失败：%s", exc)


class HttpAgent:
    def __init__(self, url: str, token: str = "", timeout: int = 900):
        self.url = url
        self.token = token
        self.timeout = timeout

    def run(self, prompt: str, session_id: str | None, meta: dict | None = None) -> AgentResult:
        started = time.time()
        payload = {"text": prompt, "session_id": session_id, "meta": meta or {}}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except requests.Timeout:
            raise AgentError(f"远端 agent 超时（{self.timeout}s）")
        except requests.RequestException as exc:
            raise AgentError(f"连不上远端 agent：{exc}")
        if resp.status_code == 401:
            raise AgentError("远端 agent 鉴权失败，检查 AGENT_TOKEN")
        if resp.status_code >= 400:
            raise AgentError(f"远端 agent 返回 {resp.status_code}：{resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError:
            raise AgentError(f"远端 agent 返回的不是 JSON：{resp.text[:300]}")
        text = data.get("reply") or data.get("result") or data.get("text") or ""
        if not text:
            raise AgentError("远端 agent 返回内容为空")
        return AgentResult(text=str(text), session_id=data.get("session_id"), elapsed=time.time() - started)


class CommandAgent:
    """SSH 或本地命令通用实现：prompt 从 stdin 传入，避免超长参数和转义问题。"""

    def __init__(self, argv: list[str], timeout: int = 900, label: str = "command"):
        self.argv = argv
        self.timeout = timeout
        self.label = label

    def run(self, prompt: str, session_id: str | None = None, meta: dict | None = None) -> AgentResult:
        started = time.time()
        try:
            proc = subprocess.run(
                self.argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise AgentError(f"{self.label} 超时（{self.timeout}s）")
        except FileNotFoundError as exc:
            raise AgentError(f"{self.label} 命令不存在：{exc}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise AgentError(f"{self.label} 退出码 {proc.returncode}：{tail}")
        text, sid = _parse_claude_json(proc.stdout)
        if not text:
            raise AgentError(f"{self.label} 没有输出")
        return AgentResult(text=text, session_id=sid, elapsed=time.time() - started)


def build_agent(cfg) -> HttpAgent | CommandAgent:
    if cfg.transport == "http":
        return HttpAgent(cfg.agent_url, cfg.agent_token, cfg.agent_timeout)
    if cfg.transport == "ssh":
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", cfg.ssh_target, "--"] + shlex.split(
            cfg.ssh_command
        )
        return CommandAgent(argv, cfg.agent_timeout, label="ssh")
    argv = shlex.split(cfg.local_command)
    return CommandAgent(argv, cfg.agent_timeout, label="local")
