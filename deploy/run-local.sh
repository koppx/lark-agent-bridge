#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a
source "$ROOT/.env"
set +a
unset CLAUDECODE CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_PID CLAUDE_CODE_ENTRYPOINT
export PATH="$HOME/.local/bin:$PATH"
case "${1:-}" in
  agent) exec "$ROOT/.venv/bin/python" -u remote_agent_server.py --host 127.0.0.1 --port 8787 ;;
  bridge) exec "$ROOT/.venv/bin/python" -u -m bridge.app ;;
  *) printf 'Usage: %s agent|bridge\n' "$0" >&2; exit 2 ;;
esac
