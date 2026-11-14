"""飞书开放平台 REST 封装：租户 token 缓存、回复消息、分片发送。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

import requests

from .text import split_text

log = logging.getLogger("bridge.feishu")

TOKEN_INVALID_CODES = {99991661, 99991663, 99991664, 99991668, 99991677}


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, detail: str = ""):
        self.code = code
        self.msg = msg
        self.detail = detail
        super().__init__(f"code={code} msg={msg} {detail}".strip())

    @property
    def hint(self) -> str:
        if self.code in (99991672, 99991679, 230027):
            return "应用缺少权限或被管理员限制，去开放平台补 im:message 相关权限并重新发版。"
        if self.code == 230002:
            return "机器人不在这个会话里，先把应用机器人拉进群。"
        if self.code in (99991400, 99991401):
            return "请求过于频繁或超出频率限制，放慢发送节奏。"
        return ""


class FeishuClient:
    BASE = "https://open.feishu.cn"

    def __init__(self, app_id: str, app_secret: str, timeout: int = 15):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout = timeout
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expire_at = 0.0

    # ---------- 底层 ----------
    def tenant_token(self, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expire_at - 60:
                return self._token
            resp = requests.post(
                f"{self.BASE}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=self.timeout,
            )
            data = resp.json()
            if data.get("code") != 0:
                raise FeishuError(data.get("code", -1), data.get("msg", "token fail"))
            self._token = data["tenant_access_token"]
            self._expire_at = time.time() + int(data.get("expire", 7200))
            return self._token

    def _post(self, path: str, payload: dict, params: dict | None = None) -> dict:
        last: FeishuError | None = None
        for attempt in (1, 2):
            token = self.tenant_token(force=attempt > 1)
            resp = requests.post(
                f"{self.BASE}{path}",
                json=payload,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
            try:
                data = resp.json()
            except ValueError:
                raise FeishuError(-1, f"HTTP {resp.status_code}", resp.text[:300])
            if data.get("code") == 0:
                return data.get("data") or {}
            err = FeishuError(data.get("code", -1), data.get("msg", ""), json.dumps(data, ensure_ascii=False)[:300])
            if err.code in TOKEN_INVALID_CODES and attempt == 1:
                last = err
                continue
            raise err
        raise last or FeishuError(-1, "unknown")

    # ---------- 业务 ----------
    def reply_text(self, message_id: str, text: str) -> dict:
        return self._post(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            {
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "reply_in_thread": False,
            },
        )

    def send_text(self, chat_id: str, text: str, uuid: str | None = None) -> dict:
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        if uuid:
            payload["uuid"] = uuid[:50]
        return self._post("/open-apis/im/v1/messages", payload, params={"receive_id_type": "chat_id"})

    def send_chunks(
        self,
        chat_id: str,
        message_id: str,
        chunks: list[str],
        use_reply: bool = True,
    ) -> None:
        """第一条用 reply 挂在线程上，其余用 send。失败自动降级为 send。"""
        for idx, chunk in enumerate(chunks):
            uid = hashlib.sha1(f"{message_id}:{idx}:{chunk[:32]}".encode()).hexdigest()
            if idx == 0 and use_reply:
                try:
                    self.reply_text(message_id, chunk)
                    continue
                except FeishuError as exc:
                    log.warning("reply 失败，降级为 send：%s %s", exc, exc.hint)
            self.send_text(chat_id, chunk, uuid=uid)

    def bot_info(self) -> dict:
        """取机器人自身信息，用来拿到 bot 的 open_id（群聊里判断是否 @ 了本机器人）。"""
        token = self.tenant_token()
        resp = requests.get(
            f"{self.BASE}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(data.get("code", -1), data.get("msg", "bot info fail"))
        return data.get("bot") or {}


class DryRunFeishu:
    """联调用：不真的发飞书，把内容打到 stdout。"""

    def reply_text(self, message_id: str, text: str) -> dict:
        print(f"[SEND reply={message_id}] {text}", flush=True)
        return {}

    def send_text(self, chat_id: str, text: str, uuid: str | None = None) -> dict:
        print(f"[SEND chat={chat_id}] {text}", flush=True)
        return {}

    def send_chunks(self, chat_id: str, message_id: str, chunks: list[str], use_reply: bool = True) -> None:
        for idx, chunk in enumerate(chunks):
            if idx == 0 and use_reply:
                self.reply_text(message_id, chunk)
            else:
                self.send_text(chat_id, chunk)


def chunk(text: str, limit: int, max_chunks: int) -> list[str]:
    return split_text(text, limit=limit, max_chunks=max_chunks)
