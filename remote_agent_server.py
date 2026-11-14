"""把服务器上的 Claude Code / 任意 CLI 包成一个 HTTP 接口，给飞书侧调用。

启动（先自测，不调真实模型）：
    python3 remote_agent_server.py --dry-run --port 8787

正式：
    python3 remote_agent_server.py --port 8787 --token change-me \
        --workdir /srv/repo --command "claude -p --output-format json"

接口：
    POST /task  Authorization: Bearer <token>
        {"text": "任务描述", "session_id": "可选，上次返回的会话 id", "meta": {...}}
        -> {"reply": "...", "session_id": "uuid", "elapsed": 12.3}
    GET  /health -> {"ok": true, "channel": "http", "mode": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG = {
    "token": "",
    "command": "claude -p --output-format json",
    "workdir": None,
    "timeout": 900,
    "dry_run": False,
}

PRINT_LOCK = threading.Lock()


def _parse_claude_json(stdout: str) -> tuple[str, str | None]:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return str(obj.get("result") or obj.get("text") or stdout.strip()), obj.get("session_id")
    return stdout.strip(), None


def run_task(prompt: str, session_id: str | None, meta: dict) -> dict:
    started = time.time()
    if CONFIG["dry_run"]:
        time.sleep(1.0)
        text = (
            f"[dry-run] 收到任务（{len(prompt)} 字）：\n{prompt[:200]}\n\n"
            f"会话：{session_id or '（新会话）'}\n元信息：{json.dumps(meta, ensure_ascii=False)}"
        )
        return {"reply": text, "session_id": session_id or "mock-session-0001", "elapsed": time.time() - started}

    argv = shlex.split(CONFIG["command"])
    if session_id:
        argv += ["--resume", session_id]

    with PRINT_LOCK:
        print(f"[task] session={session_id or '-'} chars={len(prompt)} cwd={CONFIG['workdir']}", flush=True)

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=CONFIG["workdir"],
            timeout=CONFIG["timeout"],
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"任务超时（{CONFIG['timeout']}s）")
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到命令 {argv[0]}，检查 PATH：{exc}")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"命令退出码 {proc.returncode}：{tail}")

    text, new_sid = _parse_claude_json(proc.stdout)
    if not text.strip():
        raise RuntimeError("命令没有输出")
    return {"reply": text, "session_id": new_sid or session_id, "elapsed": time.time() - started}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 少刷点日志
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = CONFIG["token"]
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {token}"

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/health":
            self._json(200, {"ok": True, "channel": "http", "dry_run": CONFIG["dry_run"], "workdir": CONFIG["workdir"]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/task":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad json"})
            return

        prompt = (payload.get("text") or "").strip()
        if not prompt:
            self._json(400, {"error": "text 不能为空"})
            return

        try:
            result = run_task(prompt, payload.get("session_id"), payload.get("meta") or {})
            self._json(200, result)
        except Exception as exc:
            print(f"[task-fail] {exc}", flush=True)
            self._json(500, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="远端 agent HTTP 服务")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--token", default=os.environ.get("AGENT_TOKEN", ""))
    parser.add_argument("--command", default=os.environ.get("CLAUDE_COMMAND", "claude -p --output-format json"))
    parser.add_argument("--workdir", default=os.environ.get("CLAUDE_WORKDIR"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("AGENT_TIMEOUT", "900")))
    parser.add_argument("--dry-run", action="store_true", help="不调模型，回显，用来联调链路")
    args = parser.parse_args()

    CONFIG.update(
        token=args.token,
        command=args.command,
        workdir=args.workdir,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    if not args.token:
        print("[warn] 没设 --token，任何能访问端口的人都能指挥这台机器上的 agent，生产环境务必设置", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on http://{args.host}:{args.port}  command={args.command}  dry_run={args.dry_run}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
