"""Queue Feishu input for the current terminal agent, without spawning a model."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from bridge.app import normalize
from bridge.config import Config
from bridge.feishu import FeishuClient
from bridge.text import extract_text, is_at_me, split_text

ROOT = Path(__file__).resolve().parent


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(ROOT / "state" / "terminal-queue.sqlite", timeout=10)
    connection.execute("CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, owner TEXT NOT NULL, enabled INTEGER NOT NULL)")
    connection.execute("INSERT OR IGNORE INTO control VALUES (1, '', 0)")
    connection.execute("CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL)")
    connection.commit()
    return connection


def receive(connection: sqlite3.Connection, message: dict, config: Config, bot_open_id: str) -> bool:
    if message.get("open_id") not in config.allow_open_ids or message.get("chat_id") not in config.allow_chat_ids:
        return False
    if not bot_open_id or message.get("open_id") == bot_open_id or not is_at_me(message.get("chat_type", "group"), message.get("mentions"), bot_open_id):
        return False
    text = extract_text(message.get("message_type", "text"), message.get("content", ""), message.get("mentions"))
    if not text or not message.get("message_id"):
        return False
    with connection:
        enabled = connection.execute("SELECT enabled FROM control WHERE id=1").fetchone()[0]
        if not enabled:
            return False
        cursor = connection.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'queued', ?)", (message["message_id"], message["chat_id"], text, time.time()))
        return cursor.rowcount == 1


def listen(config: Config) -> None:
    import lark_oapi as lark

    with (ROOT / "state" / "terminal-listener.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        client = FeishuClient(config.app_id, config.app_secret)
        bot_open_id = client.bot_info()["open_id"]

        def on_event(event) -> None:
            try:
                with connect() as connection:
                    if receive(connection, normalize(event), config, bot_open_id):
                        logging.info("Queued authorized Feishu message")
            except Exception:
                logging.exception("Failed to queue Feishu message")

        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_event).build()
        logging.info("Starting terminal queue listener")
        lark.ws.Client(config.app_id, config.app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING).start()


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["listen", "on", "off", "status", "wait", "reply"])
    parser.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--message-id")
    arguments = parser.parse_args()
    config = Config.load(ROOT / ".env")
    if arguments.action == "listen":
        listen(config)
        return
    if not arguments.session:
        parser.error("Pass the current terminal session ID with --session")
    with connect() as connection:
        if arguments.action == "on":
            with connection:
                owner, enabled = connection.execute("SELECT owner, enabled FROM control WHERE id=1").fetchone()
                if enabled and owner != arguments.session:
                    raise SystemExit("Another terminal session owns this bridge; disable it there first")
                connection.execute("UPDATE control SET owner=?, enabled=1 WHERE id=1", (arguments.session,))
            print(json.dumps({"enabled": True, "session": arguments.session}))
            return
        owner, enabled = connection.execute("SELECT owner, enabled FROM control WHERE id=1").fetchone()
        if arguments.action == "status":
            print(json.dumps({"enabled": bool(enabled), "owner": owner, "this_session": owner == arguments.session, "queued": connection.execute("SELECT COUNT(*) FROM messages WHERE state='queued'").fetchone()[0]}))
            return
        if owner != arguments.session:
            raise SystemExit("This terminal session does not own the bridge")
        if arguments.action == "off":
            with connection:
                connection.execute("UPDATE control SET enabled=0 WHERE id=1")
                connection.execute("UPDATE messages SET state='discarded' WHERE state='queued'")
            print('{"enabled": false}')
        elif arguments.action == "reply":
            text = sys.stdin.read().strip()
            row = connection.execute("SELECT chat_id, state FROM messages WHERE message_id=?", (arguments.message_id,)).fetchone()
            if not row or row[1] != "claimed" or not text:
                raise SystemExit("Expected a claimed message ID and nonempty reply on stdin")
            FeishuClient(config.app_id, config.app_secret).send_chunks(row[0], arguments.message_id, split_text(text, config.max_chunk_chars, config.max_chunks))
            with connection:
                connection.execute("UPDATE messages SET state='replied' WHERE message_id=?", (arguments.message_id,))
            print('{"sent": true}')
        elif arguments.action == "wait":
            with (ROOT / "state" / "terminal-waiter.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                deadline = time.monotonic() + arguments.timeout
                while time.monotonic() < deadline:
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        owner, enabled = connection.execute("SELECT owner, enabled FROM control WHERE id=1").fetchone()
                        if not enabled or owner != arguments.session:
                            print('{"event": "disabled"}')
                            return
                        row = connection.execute("SELECT message_id, chat_id, text FROM messages WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                        if row:
                            connection.execute("UPDATE messages SET state='claimed' WHERE message_id=?", (row[0],))
                    if row:
                        print(json.dumps({"event": "feishu_user_message", "message_id": row[0], "chat_id": row[1], "text": row[2]}, ensure_ascii=False), flush=True)
                        return
                    time.sleep(1)
                print('{"event": "wait_timeout"}')


if __name__ == "__main__":
    main()
