"""事件分发：鉴权、去重、指令、异步执行、结果回发。"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .feishu import FeishuError
from .text import extract_text, is_at_me, parse_command, split_text

log = logging.getLogger("bridge.dispatch")

HELP_TEXT = "\n".join(
    [
        "用法：@我 直接说你要干什么，我会把任务转给远端 agent，跑完把结果发回来。",
        "",
        "/new   开一个新会话（丢掉上下文，适合换话题）",
        "/help  看这条说明",
        "/ping  测一下机器人是否在线",
    ]
)

ACK_TEXT = "收到，已转给远端 agent，跑完我发结果。"


class Dispatcher:
    def __init__(self, cfg, feishu, agent, sessions, bot_open_id: str = ""):
        self.cfg = cfg
        self.feishu = feishu
        self.agent = agent
        self.sessions = sessions
        self.bot_open_id = bot_open_id
        self._dedupe: dict[str, float] = {}
        self._dedupe_lock = threading.Lock()
        self._running: dict[str, float] = {}
        self._running_lock = threading.Lock()
        self._gen: dict[str, int] = {}
        self.executor = ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="task")

    # ---------- 入口 ----------
    def handle(self, msg: dict) -> str:
        """返回处理结果标记，便于测试：dup / denied / ignored / command / busy / queued"""
        key = msg.get("message_id") or f"{msg.get('chat_id')}:{msg.get('create_time')}"
        if not self._first_time(key):
            log.info("重复事件，忽略：%s", key)
            return "dup"

        chat_id = msg.get("chat_id", "")
        open_id = msg.get("open_id", "")
        chat_type = msg.get("chat_type", "p2p")

        if not self._auth_open_id(open_id):
            log.warning("未授权用户 open_id=%s chat=%s，把它加进 ALLOW_OPEN_IDS 才能用", open_id, chat_id)
            self._safe_reply(msg, f"你还没有权限（open_id: {open_id}）。让管理员把这个 open_id 加到 ALLOW_OPEN_IDS 后重启服务。")
            return "denied"
        if not self._auth_chat(chat_id):
            log.warning("会话不在白名单：%s", chat_id)
            self._safe_reply(msg, "这个会话不在白名单里。")
            return "denied"
        if not is_at_me(chat_type, msg.get("mentions"), self.bot_open_id):
            return "ignored"

        text = extract_text(msg.get("message_type", "text"), msg.get("content", ""), msg.get("mentions"))
        if not text:
            self._safe_reply(msg, "只支持文本指令。图片、文件先发文字说明要干什么。")
            return "ignored"

        cmd, rest = parse_command(text)
        if cmd == "ping":
            self._safe_reply(msg, f"在线。后端：{self.cfg.transport}，超时 {self.cfg.agent_timeout}s。")
            return "command"
        if cmd == "help":
            self._safe_reply(msg, HELP_TEXT)
            return "command"
        if cmd == "new":
            self._bump_gen(chat_id)
            self.sessions.clear(chat_id)
            self._safe_reply(msg, "已开新会话，之前的上下文不带了（正在跑的任务不会中断）。")
            return "command"
        if cmd == "status":
            sid = self.sessions.get(chat_id)
            self._safe_reply(msg, f"后端：{self.cfg.transport}\n会话：{sid or '（新会话）'}\n运行中：{'是' if self._is_running(chat_id) else '否'}")
            return "command"

        if cmd and cmd != "new" and rest:
            text = rest  # 允许 "/xxx 具体任务" 透传

        if self._is_running(chat_id):
            self._safe_reply(msg, "上一个任务还在跑，等它出结果再发新的。想彻底换话题就 /new。")
            return "busy"

        self._safe_reply(msg, ACK_TEXT)
        self._mark_running(chat_id)
        self.executor.submit(self._run, msg, text, self._gen.get(chat_id, 0))
        return "queued"

    # ---------- 执行 ----------
    def _run(self, msg: dict, text: str, gen: int) -> None:
        chat_id = msg.get("chat_id", "")
        message_id = msg.get("message_id", "")
        sid = self.sessions.get(chat_id)
        meta = {
            "source": "feishu",
            "chat_id": chat_id,
            "chat_type": msg.get("chat_type"),
            "open_id": msg.get("open_id"),
            "message_id": message_id,
        }
        log.info("开始执行 chat=%s len=%d session=%s", chat_id, len(text), sid)
        try:
            result = self.agent.run(text, sid, meta)
            # 期间用户发过 /new，就不要把旧会话写回去
            if result.session_id and self._gen.get(chat_id, 0) == gen:
                self.sessions.set(chat_id, result.session_id)
            body = f"任务完成（{result.elapsed:.1f}s）\n\n{result.text}"
            chunks = split_text(body, self.cfg.max_chunk_chars, self.cfg.max_chunks)
            self.feishu.send_chunks(chat_id, message_id, chunks)
            log.info("执行完成 chat=%s %.1fs %d 字", chat_id, result.elapsed, len(result.text))
        except Exception as exc:  # 任何异常都要让用户看到
            log.exception("执行失败 chat=%s", chat_id)
            hint = f"\n\n{exc.hint}" if isinstance(exc, FeishuError) else ""
            self._safe_reply(msg, f"任务失败：{exc}{hint}")
        finally:
            self._mark_idle(chat_id)

    # ---------- 辅助 ----------
    def _safe_reply(self, msg: dict, text: str) -> None:
        try:
            self.feishu.reply_text(msg.get("message_id", ""), text)
        except Exception as exc:
            log.warning("回复失败：%s", exc)

    def _first_time(self, key: str, ttl: float = 900) -> bool:
        now = time.time()
        with self._dedupe_lock:
            for k, ts in list(self._dedupe.items()):
                if now - ts > ttl:
                    self._dedupe.pop(k, None)
            if key in self._dedupe:
                return False
            self._dedupe[key] = now
            return True

    def _auth_open_id(self, open_id: str) -> bool:
        if not self.cfg.allow_open_ids:
            return False
        return open_id in self.cfg.allow_open_ids

    def _auth_chat(self, chat_id: str) -> bool:
        if not self.cfg.allow_chat_ids:
            return True
        return chat_id in self.cfg.allow_chat_ids

    def _mark_running(self, chat_id: str) -> None:
        with self._running_lock:
            self._running[chat_id] = time.time()

    def _mark_idle(self, chat_id: str) -> None:
        with self._running_lock:
            self._running.pop(chat_id, None)

    def _is_running(self, chat_id: str) -> bool:
        with self._running_lock:
            return chat_id in self._running

    def _bump_gen(self, chat_id: str) -> None:
        with self._running_lock:
            self._gen[chat_id] = self._gen.get(chat_id, 0) + 1

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)
