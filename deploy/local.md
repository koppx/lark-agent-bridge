# 本机部署与联调

本节目的：先在自己的电脑验证消息解析、HTTP 转发与结果回传，再按需验证真实机器人。

本教程适用于 macOS/Linux 的 Bash 或 Zsh 终端，推荐 Python 3.10+。离线联调不会连接飞书，也不会调用模型。连接真实机器人后，任务在本机执行；控制远端机器请转到[服务端教程](server.md)。

## 一、取得项目与安装依赖

本节目的：准备独立的 Python 环境，避免影响系统 Python。

仓库 `koppx/lark-agent-bridge` 是私有仓库。使用有仓库访问权限的 GitHub 账号登录；已有登录态可跳过登录命令。

```bash
gh auth login --hostname github.com --git-protocol https --web
gh repo clone koppx/lark-agent-bridge
cd lark-agent-bridge

python3 --version
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

如果没有 `gh`，可以在 GitHub 仓库页面通过 “Code → Download ZIP” 下载并解压，然后在解压目录执行 Python 环境准备命令。不要在 Git URL 中填写访问令牌。

后续命令均在项目根目录执行，并直接使用 `./.venv/bin/python`，不需要激活虚拟环境。

## 二、运行离线测试

本节目的：验证解析、白名单、去重、会话传递与失败回传等已有逻辑。

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

预期：23 个测试通过，结尾显示 `OK`。测试用的是模拟 agent 和飞书客户端，不代表已完成真实网络部署。

## 三、模拟完整消息链路

本节目的：验证 `模拟 @ 消息 → bridge → HTTP 服务 → 模拟回复` 的完整路径。

打开两个终端，均进入项目根目录。

终端 A：启动只监听本机的模拟 agent 服务。

```bash
./.venv/bin/python remote_agent_server.py \
    --host 127.0.0.1 \
    --port 8787 \
    --token test-token \
    --dry-run
```

终端 B：确认服务在线，再启动模拟飞书入口。

```bash
curl --fail --silent --show-error http://127.0.0.1:8787/health

AGENT_URL=http://127.0.0.1:8787/task \
AGENT_TOKEN=test-token \
./.venv/bin/python -m bridge.app --mock
```

在终端 B 输入一行后按回车：

```json
{"text":"帮我确认这条任务能收到"}
```

预期先打印 `[SEND reply=...] 收到...`，约一秒后打印“任务完成”与 `[dry-run] 收到任务...`。这些是终端输出，不会发送到真实飞书。

等待任务完成后，再逐条验证：

```json
{"text":"/new"}
{"text":"继续验证第二条任务"}
```

同样等待第二条任务完成，然后验证白名单：

```json
{"text":"我是未授权用户","open_id":"ou_stranger"}
```

预期返回“你还没有权限”，不会执行这条任务。不要把多条任务一次性粘贴进去；前一条尚未完成时，新任务可能返回“上一个任务还在跑”。

结束时，终端 B 按 Ctrl-D 等待已提交任务结束，终端 A 按 Ctrl-C 关闭模拟服务。`test-token` 只用于这个本机模拟示例。

## 四、可选：连接真实飞书

本节目的：验证“wusi的飞书 CLI”的真实消息能够到达本机，并触发本机 agent。

先完成[飞书后台配置](../README.md#二机器人与飞书配置)。此模式需要本机能访问飞书和模型服务，且本机已安装并配置 Claude Code。

准备配置；已有 `.env` 时保留原文件并手动修改：

```bash
test -f .env || cp .env.example .env
chmod 600 .env
command -v claude
claude --version
```

用编辑器修改 `.env`：

```dotenv
FEISHU_APP_ID=cli_aa9c105a7678dbdb
FEISHU_APP_SECRET=填写该应用的真实密钥
BOT_OPEN_ID=
ALLOW_OPEN_IDS=
ALLOW_CHAT_IDS=
AGENT_TRANSPORT=http
AGENT_URL=http://127.0.0.1:8787/task
AGENT_TOKEN=填写自己生成的随机令牌
CLAUDE_COMMAND="/填写实际绝对路径/claude -p --output-format json"
CLAUDE_WORKDIR=/填写本机任务仓库的绝对路径
AGENT_TIMEOUT=900
STATE_DIR=./state
```

可用下面的命令生成 `AGENT_TOKEN`，将输出填入私有 `.env`；不要上传该文件：

```bash
./.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
```

`CLAUDE_COMMAND` 的可执行文件路径取自 `command -v claude`。工作目录必须存在。两个路径都写绝对路径；路径含空格时，除整项配置的外层双引号外，还要用内层单引号包住命令路径。

终端 A：加载自己编辑的 `.env`，启动真实 agent 服务。`.env` 中含空格的值按示例保留引号，加载前不应有未替换的占位值。

```bash
set -a
. ./.env
set +a

./.venv/bin/python remote_agent_server.py \
    --host 127.0.0.1 \
    --port 8787 \
    --timeout 840
```

终端 B：启动桥接进程；它会自动读取 `.env`。

```bash
./.venv/bin/python -m bridge.app --env .env
```

`ALLOW_OPEN_IDS` 留空时，先在飞书 @ 一次机器人，从拒绝回复或终端日志复制用户 `open_id`，填入 `.env` 后重启终端 B 的桥接进程。`/ping`、`/status` 也需要通过白名单检查。

依次发送：

```text
@wusi的飞书 CLI /ping
@wusi的飞书 CLI 请只输出当前工作目录
```

实际发送时在飞书的 @ 列表选择机器人，再输入后面的文字；只输入一个文本形式的名称不一定形成有效提及。第二条应返回 `CLAUDE_WORKDIR` 所指定的本机目录。

## 五、切换到服务器正式运行

本节目的：确保正式任务由远端机器执行，电脑不承担常驻转发。

结束本机真实桥接进程，再按[服务端教程](server.md)在服务器运行两项服务。同一应用若同时有多个消息接收实例，事件可能分配到不同实例；本机上为该应用启动的 SDK 监听或 `lark-cli event consume` 也应结束。

正式链路是：

```text
你的飞书 → 飞书云端 → 服务器 bridge → 服务器 agent → 飞书回复
```

服务器主动建立飞书长连接。电脑和服务器不需要处于同一网络，电脑也不需要保持开机。

## 六、本机排错

本节目的：区分 Python 环境问题、接口问题与真实飞书配置问题。

| 现象 | 检查方法 |
|---|---|
| `No module named requests/lark_oapi` | 使用 `./.venv/bin/python`，重新安装 `requirements.txt` |
| 8787 端口已被占用 | 结束此前的测试服务；若改端口，同时修改启动参数和 `AGENT_URL` |
| 模拟任务鉴权失败 | 终端 A 的 `--token` 与终端 B 的 `AGENT_TOKEN` 必须一致 |
| 模拟模式没有真实飞书回复 | 正常；`--mock` 只打印结果 |
| 真实机器人没有消息 | 检查同一应用的事件、权限、发布状态、SDK 连接日志与其他监听实例 |
| 收到任务但 agent 失败 | 查看终端 A 的错误；核对命令、工作目录、模型配置与工具权限 |
| 修改 `.env` 后仍使用旧值 | 当前进程环境变量优先；重新打开终端，或重新加载 `.env` 并重启进程 |
