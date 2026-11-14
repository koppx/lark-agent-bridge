# 将飞书消息接入当前终端会话

这个模式把飞书任务交给已经打开的 Claude Code 会话，不会另起 `claude -p`，也不会并发恢复同一个会话。它不使用 Channels：本地 listener 接收消息并写入 SQLite 队列，后台等待任务结束后，通过终端的任务通知唤醒当前对话。

## 安装

需要 Linux、Python 3.10+、tmux，以及支持后台 Bash 任务完成通知的终端环境。

```bash
git clone https://github.com/koppx/lark-agent-bridge.git
cd lark-agent-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
mkdir -p state logs
```

编辑 `.env`，填写应用凭证，并同时设置 `ALLOW_OPEN_IDS` 和 `ALLOW_CHAT_IDS`。当前会话模式要求用户、聊天都在白名单中；空列表拒绝接收。群内还必须真正 @ 机器人。配置说明见 README。脚本复用现有配置解析，因此 `.env` 中的 transport 配置也需要合法；使用模板中的默认 HTTP 配置即可，但此模式不会调用 HTTP agent 服务。

安装 skill，使用实际仓库绝对路径替换模板中的 `__BRIDGE_ROOT__`：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
root = Path.cwd().resolve()
target = Path.home() / '.claude/skills/lark/SKILL.md'
if target.exists():
    raise SystemExit('已有 lark skill，请先检查并备份，不自动覆盖')
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text((root / 'skills/lark/SKILL.md').read_text().replace('__BRIDGE_ROOT__', str(root)))
PY
```

启动独立 listener（不要让旧的 `bridge.app` 同时处理同一机器人的任务，避免重复执行）：

```bash
tmux new-session -d -s lark-terminal-listener \
  "cd '$PWD' && exec .venv/bin/python session_bridge.py listen >> logs/terminal-listener.log 2>&1"
tmux has-session -t lark-terminal-listener
```

tmux 会话存在只说明进程会话存在；部署后还需在飞书 @ 机器人发送简单任务，确认收取与回复都成功。凭证或网络错误查看本地 listener 日志，不要把包含私有信息的日志公开上传。

## 日常使用

在加载了 skill 的 Claude Code 终端里输入：

```text
/lark agent on
/lark agent status
/lark agent off
```

开启后，在配置的飞书群中发送 `@机器人 任务描述`。当前终端处理并回复原消息。必须保持终端会话打开；每次收到消息或等待超时后，助手需要续接一个后台 waiter。开启标志不代表 waiter 一定存在，status 应同时检查 listener 和当前任务。

关闭会丢弃尚未领取的队列消息，但不会取消已在执行的工作。终端普通对话不会自动同步到群里。飞书消息没有额外权限，敏感操作仍需确认，终端权限弹窗仍需要在终端处理。

## 会话占用与恢复

同一时间只允许一个 owner 和一个 waiter。其他终端开启时会被拒绝。正常切换应先在旧终端执行 `/lark agent off`，再在新终端开启。旧会话关闭后，状态不会自动释放；需要操作者明确确认恢复，不能猜测旧 session ID 或绕过 owner 检查。

已领取的消息在崩溃后不会自动重试，以免重复执行。应检查状态和已完成动作，再让用户明确重发。不要删除 SQLite 数据库来解决锁问题；其中包含消息去重和领取记录。

## 数据与测试

`state/terminal-queue.sqlite` 保存消息正文、聊天 ID、领取状态和 owner；`state/` 与 `logs/` 只留本机，不提交。脚本启动使用私有 umask，但已有目录仍应由操作者检查访问权限。

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`session_bridge.py --help` 可以查看底层命令。`on/off/status/wait/reply` 默认使用工具环境里的 `CLAUDE_CODE_SESSION_ID`，不要手工填入其他会话的 ID。回复只能发送到已领取的原始消息。
