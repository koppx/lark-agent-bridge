import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.agent import AgentResult  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.dispatch import Dispatcher  # noqa: E402
from bridge.text import extract_text, is_at_me, parse_command, split_text  # noqa: E402


class FakeFeishu:
    def __init__(self):
        self.replies = []
        self.sent = []

    def reply_text(self, message_id, text):
        self.replies.append((message_id, text))
        return {}

    def send_text(self, chat_id, text, uuid=None):
        self.sent.append((chat_id, text))
        return {}

    def send_chunks(self, chat_id, message_id, chunks, use_reply=True):
        for idx, c in enumerate(chunks):
            if idx == 0 and use_reply:
                self.reply_text(message_id, c)
            else:
                self.send_text(chat_id, c)


class FakeAgent:
    def __init__(self, reply="搞定了", sid="sess-1"):
        self.reply = reply
        self.sid = sid
        self.calls = []

    def run(self, prompt, session_id=None, meta=None):
        self.calls.append((prompt, session_id, meta))
        return AgentResult(text=self.reply, session_id=self.sid, elapsed=1.5)


def make_cfg(**kw):
    base = dict(
        app_id="a",
        app_secret="b",
        allow_open_ids=["ou_ok"],
        allow_chat_ids=[],
        transport="http",
        agent_url="http://x/task",
        max_chunk_chars=3500,
        max_chunks=12,
        workers=2,
        state_dir=tempfile.mkdtemp(),
        agent_timeout=30,
    )
    base.update(kw)
    return Config(**base)


def group_msg(text, open_id="ou_ok", mention=True, message_id=None):
    return {
        "message_id": message_id or f"om_{time.time_ns()}",
        "chat_id": "oc_demo",
        "chat_type": "group",
        "message_type": "text",
        "content": json.dumps({"text": ("@_user_1 " + text) if mention else text}, ensure_ascii=False),
        "mentions": [{"key": "@_user_1", "name": "bot", "open_id": "ou_bot"}] if mention else [],
        "open_id": open_id,
        "create_time": str(time.time_ns()),
    }


class TextTests(unittest.TestCase):
    def test_extract_text_removes_mention(self):
        content = json.dumps({"text": "@_user_1  跑一下  单测"}, ensure_ascii=False)
        self.assertEqual(extract_text("text", content, [{"key": "@_user_1"}]), "跑一下 单测")

    def test_extract_text_post(self):
        content = json.dumps(
            {
                "title": "标题",
                "content": [
                    [{"tag": "text", "text": "第一行 "}, {"tag": "at", "user_id": "ou_bot"}],
                    [{"tag": "text", "text": " 第二行"}],
                ],
            },
            ensure_ascii=False,
        )
        self.assertIn("第一行", extract_text("post", content))
        self.assertIn("第二行", extract_text("post", content))

    def test_extract_text_unknown_type(self):
        self.assertEqual(extract_text("image", "{}"), "")

    def test_is_at_me(self):
        self.assertTrue(is_at_me("p2p", []))
        self.assertFalse(is_at_me("group", []))
        self.assertTrue(is_at_me("group", [{"open_id": "ou_bot"}], "ou_bot"))
        self.assertFalse(is_at_me("group", [{"open_id": "ou_other"}], "ou_bot"))

    def test_split_short(self):
        self.assertEqual(split_text("短消息", 3500, 12), ["短消息"])

    def test_split_long_paragraph(self):
        chunks = split_text("a" * 5000, 1000, 12)
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))

    def test_split_keeps_paragraphs(self):
        text = "\n".join([f"第{i}行" + "字" * 200 for i in range(20)])
        chunks = split_text(text, 500, 12)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual(len("".join(chunks).replace("\n", "")), len(text.replace("\n", "")))

    def test_split_truncates_beyond_max_chunks(self):
        chunks = split_text("\n".join(["x" * 400] * 30), 400, 3)
        self.assertEqual(len(chunks), 3)
        self.assertIn("已截断", chunks[-1])

    def test_parse_command(self):
        self.assertEqual(parse_command("/new"), ("new", ""))
        self.assertEqual(parse_command("/Help"), ("help", ""))
        self.assertEqual(parse_command("帮我看看这个"), ("", "帮我看看这个"))
        self.assertEqual(parse_command("/unknown cmd"), ("", "/unknown cmd"))


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.feishu = FakeFeishu()
        self.agent = FakeAgent()
        from bridge.agent import SessionStore

        self.dispatcher = Dispatcher(self.cfg, self.feishu, self.agent, SessionStore(self.cfg.state_path), "ou_bot")

    def tearDown(self):
        self.dispatcher.executor.shutdown(wait=True)

    def _wait(self, timeout=3.0):
        from bridge.agent import SessionStore  # noqa: F401

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.dispatcher._running:
                return
            time.sleep(0.05)

    def test_unauthorized_denied_and_logged(self):
        status = self.dispatcher.handle(group_msg("跑测试", open_id="ou_stranger"))
        self.assertEqual(status, "denied")
        self.assertTrue(any("ou_stranger" in t for _, t in self.feishu.replies))
        self.assertEqual(self.agent.calls, [])

    def test_no_allowlist_denies_everyone(self):
        cfg = make_cfg(allow_open_ids=[])
        d = Dispatcher(cfg, FakeFeishu(), FakeAgent(), __import__("bridge.agent", fromlist=["SessionStore"]).SessionStore(cfg.state_path))
        self.assertEqual(d.handle(group_msg("hi")), "denied")
        d.executor.shutdown(wait=True)

    def test_chat_whitelist(self):
        cfg = make_cfg(allow_chat_ids=["oc_other"])
        from bridge.agent import SessionStore

        d = Dispatcher(cfg, FakeFeishu(), FakeAgent(), SessionStore(cfg.state_path))
        self.assertEqual(d.handle(group_msg("hi")), "denied")
        d.executor.shutdown(wait=True)

    def test_group_without_mention_ignored(self):
        self.assertEqual(self.dispatcher.handle(group_msg("hi", mention=False)), "ignored")
        self.assertEqual(self.agent.calls, [])

    def test_duplicate_ignored(self):
        msg = group_msg("跑测试", message_id="om_dup")
        self.assertEqual(self.dispatcher.handle(msg), "queued")
        self._wait()
        self.assertEqual(self.dispatcher.handle(msg), "dup")
        self.assertEqual(len(self.agent.calls), 1)

    def test_help_command(self):
        self.assertEqual(self.dispatcher.handle(group_msg("/help")), "command")
        self.assertIn("用法", self.feishu.replies[-1][1])

    def test_new_command_clears_session(self):
        msg = group_msg("干点活")
        self.dispatcher.handle(msg)
        self._wait()
        self.assertEqual(self.dispatcher.sessions.get("oc_demo"), "sess-1")
        self.assertEqual(self.dispatcher.handle(group_msg("/new", message_id="om_new")), "command")
        self.assertIsNone(self.dispatcher.sessions.get("oc_demo"))

    def test_queued_runs_agent_and_sends_result(self):
        self.dispatcher.handle(group_msg("帮我跑一下回归"))
        self._wait()
        self.assertEqual(self.agent.calls[0][0], "帮我跑一下回归")
        self.assertIsNone(self.agent.calls[0][1])  # 首次没有 session
        self.assertTrue(any("任务完成" in t for _, t in self.feishu.replies))
        self.assertEqual(self.dispatcher.sessions.get("oc_demo"), "sess-1")

    def test_second_task_carries_session(self):
        self.dispatcher.handle(group_msg("第一件事", message_id="om_1"))
        self._wait()
        self.dispatcher.handle(group_msg("第二件事", message_id="om_2"))
        self._wait()
        self.assertEqual(self.agent.calls[1][1], "sess-1")

    def test_busy_returns_without_dispatch(self):
        slow_agent = FakeAgent()
        d = Dispatcher(self.cfg, self.feishu, slow_agent, self.dispatcher.sessions, "ou_bot")
        # 手动占住会话
        d._mark_running("oc_demo")
        self.assertEqual(d.handle(group_msg("插队", message_id="om_q")), "busy")
        d.executor.shutdown(wait=True)

    def test_new_during_run_does_not_restore_session(self):
        """任务跑到一半发 /new，旧任务的 session 不能写回来。"""

        class Slow(FakeAgent):
            def run(self, *args, **kwargs):
                time.sleep(0.3)
                return AgentResult("done", "old-session", 0.3)

        from bridge.agent import SessionStore

        d = Dispatcher(self.cfg, self.feishu, Slow(), SessionStore(self.cfg.state_path), "ou_bot")
        self.assertEqual(d.handle(group_msg("干点活", message_id="om_slow")), "queued")
        time.sleep(0.05)
        self.assertEqual(d.handle(group_msg("/new", message_id="om_reset")), "command")
        d.executor.shutdown(wait=True)
        self.assertIsNone(d.sessions.get("oc_demo"))

    def test_agent_failure_reported(self):
        class Boom(FakeAgent):
            def run(self, *a, **kw):
                raise RuntimeError("远端挂了")

        d = Dispatcher(self.cfg, self.feishu, Boom(), self.dispatcher.sessions, "ou_bot")
        d.handle(group_msg("干点活", message_id="om_boom"))
        deadline = time.time() + 3
        while time.time() < deadline and d._running:
            time.sleep(0.05)
        self.assertTrue(any("任务失败" in t and "远端挂了" in t for _, t in self.feishu.replies))
        d.executor.shutdown(wait=True)


class SplitEdgeTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(split_text("", 100, 5), [])

    def test_limit_floor(self):
        self.assertEqual(len(split_text("abc", 0, 5)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
