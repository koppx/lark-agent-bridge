"""入口：长连接接收 @ 消息，转给远端 agent。

真实运行：python -m bridge.app
本地联调：python -m bridge.app --mock   （读 stdin 的模拟事件，不碰飞书）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time

from .agent import build_agent, SessionStore
from .config import Config
from .dispatch import Dispatcher
from .feishu import DryRunFeishu, FeishuClient


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


def normalize(ev) -> dict:
    """把 lark-oapi 的事件对象拍平成朴素 dict（后面所有逻辑都不依赖 SDK）。"""
    data = getattr(ev, "event", ev)
    msg = getattr(data, "message", data)
    sender = getattr(data, "sender", None)

    open_id = ""
    if sender is not None:
        sid_obj = getattr(sender, "sender_id", None)
        open_id = getattr(sid_obj, "open_id", "") or "" if sid_obj is not None else ""

    mentions = []
    for m in getattr(msg, "mentions", None) or []:
        mid = getattr(m, "id", None)
        mentions.append(
            {
                "key": getattr(m, "key", ""),
                "name": getattr(m, "name", ""),
                "open_id": (getattr(mid, "open_id", "") if mid is not None else "") or "",
            }
        )

    return {
        "message_id": getattr(msg, "message_id", ""),
        "chat_id": getattr(msg, "chat_id", ""),
        "chat_type": getattr(msg, "chat_type", "p2p"),
        "message_type": getattr(msg, "message_type", "text"),
        "content": getattr(msg, "content", ""),
        "mentions": mentions,
        "open_id": open_id,
        "create_time": getattr(msg, "create_time", ""),
    }


def run_real(cfg: Config) -> None:
    feishu = FeishuClient(cfg.app_id, cfg.app_secret)
    bot_open_id = os.environ.get("BOT_OPEN_ID", "").strip()
    try:
        if not bot_open_id:
            info = feishu.bot_info()
            bot_open_id = info.get("open_id", "")
            logging.info("机器人：%s (%s)", info.get("app_name", "?"), bot_open_id or "open_id 未取到")
        else:
            feishu.tenant_token()
            logging.info("机器人 open_id 来自环境变量：%s", bot_open_id)
    except Exception as exc:
        logging.warning("启动自检未通过（不影响收消息，但可能影响 @ 判断）：%s", exc)

    if not cfg.allow_open_ids:
        logging.warning("ALLOW_OPEN_IDS 为空：所有消息都会被拒绝，日志里会打印对方 open_id，复制进 .env 再重启。")

    sessions = SessionStore(cfg.state_path)
    agent = build_agent(cfg)
    dispatcher = Dispatcher(cfg, feishu, agent, sessions, bot_open_id)

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    def on_event(ev: P2ImMessageReceiveV1) -> None:
        # 这里必须极快返回：飞书 3 秒内要拿到 ACK，重活已经丢到线程池
        try:
            dispatcher.handle(normalize(ev))
        except Exception:
            logging.exception("处理事件异常")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_event)
        .build()
    )
    ws = lark.ws.Client(cfg.app_id, cfg.app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    logging.info("长连接启动中（后端 %s）……", cfg.transport)
    try:
        ws.start()
    except KeyboardInterrupt:
        logging.info("收到中断，退出")
    finally:
        dispatcher.shutdown()


def run_mock(argv: list[str]) -> None:
    """离线联调：每行一个 JSON 事件，从 stdin 读，输出打到 stdout。"""
    env_path = argv[argv.index("--env") + 1] if "--env" in argv else ".env"
    if os.path.exists(env_path):
        from .config import _load_dotenv
        from pathlib import Path

        _load_dotenv(Path(env_path))

    cfg = Config(
        app_id="mock",
        app_secret="mock",
        allow_open_ids=[os.environ.get("MOCK_OPEN_ID", "ou_demo")],
        allow_chat_ids=[],
        transport=os.environ.get("MOCK_TRANSPORT", "http"),
        agent_url=os.environ.get("AGENT_URL", "http://127.0.0.1:8787/task"),
        agent_token=os.environ.get("AGENT_TOKEN", ""),
        ssh_target=os.environ.get("SSH_TARGET", ""),
        ssh_command=os.environ.get("SSH_COMMAND", "claude -p --output-format text"),
        local_command=os.environ.get("LOCAL_COMMAND", "claude -p --output-format text"),
        agent_timeout=int(os.environ.get("AGENT_TIMEOUT", "120")),
        workers=4,
        max_chunk_chars=int(os.environ.get("MAX_CHUNK_CHARS", "3500")),
        max_chunks=int(os.environ.get("MAX_CHUNKS", "12")),
        state_dir=tempfile.mkdtemp(prefix="bridge-mock-"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    setup_logging(cfg.log_level)

    sessions = SessionStore(cfg.state_path)
    dispatcher = Dispatcher(cfg, DryRunFeishu(), build_agent(cfg), sessions, bot_open_id="ou_bot")
    print("mock 模式：每行一个事件 JSON，Ctrl-D 结束", flush=True)
    seq = 0
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            seq += 1
            text = raw.get("text", "")
            msg = {
                "message_id": raw.get("message_id", f"om_mock_{seq}"),
                "chat_id": raw.get("chat_id", "oc_demo"),
                "chat_type": raw.get("chat_type", "group"),
                "message_type": "text",
                "content": json.dumps(
                    {"text": ("@_user_1 " + text) if raw.get("chat_type", "group") == "group" else text},
                    ensure_ascii=False,
                ),
                "mentions": (
                    [{"key": "@_user_1", "name": "bot", "open_id": "ou_bot"}]
                    if raw.get("chat_type", "group") == "group"
                    else []
                ),
                "open_id": raw.get("open_id", "ou_demo"),
                "create_time": str(int(time.time() * 1000)),
            }
            print(f"\n>>> {dispatcher.handle(msg)}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        dispatcher.executor.shutdown(wait=True)
        print("\nmock 结束", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书 ↔ 远端 agent 桥接")
    parser.add_argument("--mock", action="store_true", help="离线联调模式，不连飞书")
    parser.add_argument("--env", default=".env", help="环境变量文件路径")
    args = parser.parse_args()

    if args.mock:
        run_mock(sys.argv)
        return

    cfg = Config.load(args.env)
    setup_logging(cfg.log_level)
    run_real(cfg)


if __name__ == "__main__":
    main()
