"""纯函数：消息解析、分片、指令识别。全部可离线单测。"""
from __future__ import annotations

import json
from typing import Iterable


def _json_loads(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def extract_text(message_type: str, content: str, mentions: Iterable[dict] | None = None) -> str:
    """把飞书消息的 content 转成纯文本，并去掉 @占位符。

    text 类型：{"text": "@_user_1 帮我跑个测试"}
    post 类型：{"title": "...", "content": [[{"tag": "text", "text": "..."}, ...]]}
    """
    mentions = list(mentions or [])
    if message_type == "text":
        text = _json_loads(content).get("text", "") or ""
        for m in mentions:
            key = m.get("key")
            if key:
                text = text.replace(key, " ")
        # 兜底：清掉可能残留的 @_user_N
        while "@_user_" in text:
            idx = text.find("@_user_")
            end = idx + len("@_user_")
            while end < len(text) and (text[end].isalnum() or text[end] == "_"):
                end += 1
            text = text[:idx] + " " + text[end:]
    elif message_type == "post":
        parts: list[str] = []
        title = _json_loads(content).get("title")
        if title:
            parts.append(str(title))
        for row in _json_loads(content).get("content") or []:
            if not isinstance(row, list):
                continue
            for node in row:
                if not isinstance(node, dict):
                    continue
                if node.get("tag") == "text":
                    parts.append(str(node.get("text", "")))
                elif node.get("tag") == "a":
                    parts.append(str(node.get("text", "")) or str(node.get("href", "")))
                elif node.get("tag") == "code_block":
                    parts.append(str(node.get("text", "")))
        text = "\n".join(p for p in parts if p)
    else:
        text = ""

    return " ".join(text.split()).strip() if "\n" not in text else _tidy(text)


def _tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def is_at_me(chat_type: str, mentions: Iterable[dict] | None, bot_open_id: str = "") -> bool:
    """单聊必然直达；群聊要求消息里 @ 了机器人。"""
    if chat_type != "group":
        return True
    mentions = list(mentions or [])
    if not mentions:
        return False
    if not bot_open_id:
        return True
    for m in mentions:
        mid = m.get("open_id") or ""
        nested = m.get("id")
        if not mid and isinstance(nested, dict):
            mid = nested.get("open_id", "") or ""
        if mid and mid == bot_open_id:
            return True
    return False


def split_text(text: str, limit: int = 3500, max_chunks: int = 12) -> list[str]:
    """按段落切成不超过 limit 的片段；单行过长则硬切。"""
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0:
        limit = 3500

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        # 超长单行先硬切
        while len(para) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:limit])
            para = para[limit:]
            if len(chunks) >= max_chunks:
                break
        candidate = f"{buf}\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            buf = para
        if len(chunks) >= max_chunks:
            break
    if buf and len(chunks) < max_chunks:
        chunks.append(buf)

    chunks = [c for c in chunks if c.strip()]
    if len(chunks) >= max_chunks:
        # 判断是否真的被截断（还有剩余内容）
        joined = "\n".join(chunks)
        if len(joined) < len(text):
            chunks = chunks[:max_chunks]
            chunks[-1] = chunks[-1] + "\n\n……（内容过长已截断，完整结果建议写入文档后发链接）"
    return chunks


def parse_command(text: str) -> tuple[str, str]:
    """识别 /new、/help 这类指令，返回 (命令名, 剩余内容)。"""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    cmd = head[1:].lower().strip()
    if cmd in ("new", "reset", "clear", "help", "ping", "status"):
        return cmd, rest.strip()
    return "", stripped
