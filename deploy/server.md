# 服务端部署：不同网络，通过飞书长连接收任务

本节目的：在远端服务器同时运行消息桥接和 agent 执行服务，让飞书负责跨网络传递任务。

本文的正式部署示例使用 Linux、systemd 和 Claude Code。你的电脑仅使用飞书；`bridge` 和 `remote_agent_server.py` 都在服务器运行，HTTP 接口只监听服务器自己的 `127.0.0.1:8787`。

## 一、部署位置与网络条件

本节目的：确认两个进程运行在哪里，以及谁主动建立连接。

| 部件 | 运行位置 | 连接方向 |
|---|---|---|
| 飞书客户端 | 你的电脑或手机 | 客户端访问飞书 |
| `bridge` | 远端服务器 | 服务器主动连接飞书，沿长连接接收消息 |
| `remote_agent_server.py` | 同一台远端服务器 | 接收本机 `bridge` 的 HTTP 请求 |
| Claude Code | 同一台远端服务器 | agent 服务调用 CLI；CLI 访问已配置的模型服务 |

服务器需要允许飞书 API 的 HTTPS 出站访问，以及 SDK 返回的飞书 WebSocket 地址的出站访问。无需开放 8787 的公网入站访问，也无需从电脑 SSH 到服务器。

通过服务器网页终端、云平台控制台或已有远端管理工具执行以下命令即可。如果服务器无法访问飞书外网，需要先让网络管理员开通出站访问；应用代码不能建立不存在的网络连接。

同一机器上的 `127.0.0.1` 才指向同一服务。本文两个服务都运行在主机上；若放在不同容器，需另外配置容器间网络，不能直接照用这个地址。

## 二、准备运行用户与下载项目

本节目的：让桥接服务和 Claude 使用同一位服务用户的登录、模型配置与文件权限。

推荐使用已有 Claude Code 配置的普通用户。先在该用户的终端执行：

```bash
id -un
python3 --version
command -v claude
claude --version
```

推荐 Python 3.10+。确认该用户能够访问任务工作目录，并已完成 Claude Code 登录或已有的模型网关配置；非交互任务需要的工具权限也应提前设置。

仓库是私有仓库。若服务器可以访问 GitHub 且已安装 `gh`，使用有仓库权限的账号授权后下载：

```bash
gh auth login --hostname github.com --git-protocol https --web

sudo mkdir -p /opt/lark-agent-bridge
sudo chown "$(id -u):$(id -g)" /opt/lark-agent-bridge
gh repo clone koppx/lark-agent-bridge /opt/lark-agent-bridge
cd /opt/lark-agent-bridge
```

登录命令提供的授权可以在自己电脑的浏览器中完成。不要把 GitHub 令牌拼进克隆 URL。目标目录已有代码时，直接进入已有目录，按本文最后的更新步骤操作。

没有 `gh` 或服务器无法访问 GitHub 时，在有仓库权限的电脑上通过 GitHub “Code → Download ZIP” 下载，再通过服务器平台的文件上传功能解压到 `/opt/lark-agent-bridge`。目录中应直接包含 `README.md`、`bridge/` 和 `remote_agent_server.py`。

没有 sudo 权限时，可把项目放在自己的目录，先完成前台验证，再通过服务器平台提供的常驻进程功能运行。后面的 systemd 安装步骤需要管理员权限。

## 三、安装依赖并验证 agent

本节目的：在接入飞书前确认 Python 环境与 CLI 执行正常。

以下命令均在项目根目录执行：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m unittest discover -s tests -v
```

预期 23 个测试通过。若缺少 venv/pip，先使用服务器操作系统提供的包管理工具安装对应 Python 组件。

进入实际任务仓库，使用同一位运行用户验证 Claude：

```bash
cd /替换为实际任务仓库
printf '%s\n' '请只输出当前工作目录' | claude -p --output-format json
cd /opt/lark-agent-bridge
```

预期 JSON 中的 `result` 给出任务目录，并包含可续接的 `session_id`。命令退出码、模型登录或工具权限有错误时，先修好这个步骤，再接入飞书。

本教程针对 Claude Code 的 JSON 输出和 `--resume`。其他 agent 若已有服务，应实现兼容的 `POST /task`；若替换 CLI，需确认 stdout 格式以及续会话参数兼容，不能只替换名称就假定会话续接可用。

## 四、配置同一个机器人与本机接口

本节目的：把“wusi的飞书 CLI”、授权用户和服务器 agent 关联起来。

先确认[飞书应用后台配置](../README.md#二机器人与飞书配置)。使用该应用的 App Secret，不需要在服务器上登录飞书用户账号。

```bash
test -f .env || cp .env.example .env
chmod 600 .env
./.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
```

把随机令牌填入 `.env`，随后用编辑器填写：

```dotenv
FEISHU_APP_ID=cli_aa9c105a7678dbdb
FEISHU_APP_SECRET=填写该应用的真实密钥
BOT_OPEN_ID=
ALLOW_OPEN_IDS=
ALLOW_CHAT_IDS=
AGENT_TRANSPORT=http
AGENT_URL=http://127.0.0.1:8787/task
AGENT_TOKEN=填写刚生成的随机令牌
CLAUDE_COMMAND="/实际绝对路径/claude -p --output-format json"
CLAUDE_WORKDIR=/实际任务仓库的绝对路径
AGENT_TIMEOUT=900
WORKERS=4
STATE_DIR=/opt/lark-agent-bridge/state
LOG_LEVEL=INFO
```

将示例值全部替换成真实配置。`CLAUDE_COMMAND` 的命令路径使用 `command -v claude` 的结果；工作目录必须已存在且该用户能访问。命令含空格，保留配置项外层引号；可执行文件路径含空格时，再用内层单引号包住命令路径。

`.env` 会同时用于 shell 加载、`bridge` 的配置解析与 systemd `EnvironmentFile`。使用上面的简单赋值格式，路径写绝对值，不写 `~`、`$HOME`，也不要在配置项后添加行尾注释。`.env` 已被 `.gitignore` 排除。

`ALLOW_OPEN_IDS` 可先留空取得自己的 ID：启动桥接后 @ 一次机器人，复制它拒绝回复或日志里的 `open_id`，填入此字段后重启桥接。这个 ID 必须来自“wusi的飞书 CLI”应用；多个授权用户用逗号分隔。

若机器人自身信息获取失败，先确认配置和权限，再按需填写 `BOT_OPEN_ID` 为机器人自己的 ID。它与 `ALLOW_OPEN_IDS` 中的用户 ID 职责不同。

## 五、前台验证两个服务

本节目的：先验证接口，再验证飞书消息和真实 agent，避免直接把故障服务设为开机自启。

通过网页终端打开两个会话，都进入 `/opt/lark-agent-bridge`。结束本机电脑上同一机器人应用的消息监听。

终端 A：加载 `.env` 并启动 agent 服务。

```bash
set -a
. ./.env
set +a

./.venv/bin/python remote_agent_server.py \
    --host 127.0.0.1 \
    --port 8787 \
    --timeout 840
```

`remote_agent_server.py` 不自动读取 `.env`，因此前面的加载步骤必须执行。这里的 token、命令和工作目录都来自已导出的配置。服务端超时为 840 秒，留出 60 秒让 `bridge` 在 900 秒超时前收到失败响应。

终端 B：验证健康接口，启动桥接。

```bash
curl --fail --silent --show-error http://127.0.0.1:8787/health
./.venv/bin/python -m bridge.app --env .env
```

健康接口返回 `ok: true` 只证明 HTTP 服务在线。观察 SDK 长连接是否建立，然后在飞书 @ 列表选择机器人，逐条发送并等待结果：

```text
@wusi的飞书 CLI /ping
@wusi的飞书 CLI /status
@wusi的飞书 CLI 请只输出当前工作目录
```

如果白名单尚未填写，先完成上一节的 ID 获取与重启。前两条验证桥接，第三条应返回服务器上 `CLAUDE_WORKDIR` 的目录，才证明任务在服务器执行。

正式任务也应等待前一条完成后再发送。同一个应用先只启动一个桥接实例；多实例可能接收不同事件，当前代码没有共享去重或会话锁。

## 六、配置 systemd 开机自启

本节目的：使用已验证的虚拟环境、同一份 `.env` 和同一位运行用户常驻运行。

这一步适用于有 systemd 和 sudo 权限的 Linux 主机。先用 Ctrl-C 停止前台两个进程，释放 8787 端口并避免重复监听。

模板默认项目路径为 `/opt/lark-agent-bridge`。将模板中的 `YOUR_SERVER_USER` 替换为当前已配置 Claude 的用户，再安装：

```bash
sed "s/^User=YOUR_SERVER_USER$/User=$(id -un)/" deploy/remote-agent.service \
    | sudo tee /etc/systemd/system/remote-agent.service >/dev/null
sed "s/^User=YOUR_SERVER_USER$/User=$(id -un)/" deploy/lark-bridge.service \
    | sudo tee /etc/systemd/system/lark-bridge.service >/dev/null

sudo systemd-analyze verify \
    /etc/systemd/system/remote-agent.service \
    /etc/systemd/system/lark-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now remote-agent
curl --fail --silent --show-error --retry 5 --retry-connrefused --retry-delay 1 \
    http://127.0.0.1:8787/health
sudo systemctl enable --now lark-bridge
```

两份模板都使用 `/opt/lark-agent-bridge/.venv/bin/python` 和 `EnvironmentFile=/opt/lark-agent-bridge/.env`。agent 服务只监听 `127.0.0.1`，不在命令行中写真实 token。安装到其他目录时，同步修改两个模板的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。

如果 Claude 或其 Node 运行时通过 nvm/npm 安装，交互式终端的 PATH 不会自动被 systemd 继承。在 `remote-agent.service` 的 `[Service]` 中补充真实路径，例如：

```ini
Environment="PATH=/实际node目录:/实际claude目录:/usr/local/bin:/usr/bin:/bin"
```

填写实际目录后重新执行 `daemon-reload` 和服务重启。`CLAUDE_COMMAND` 仍使用绝对路径。服务用户的模型配置应可在非交互环境使用；需要额外的模型环境变量时，写入私有 `.env`。

查看启动结果：

```bash
sudo systemctl status remote-agent lark-bridge --no-pager
sudo journalctl -u remote-agent -u lark-bridge -n 100 --no-pager
sudo journalctl -u remote-agent -u lark-bridge -f
```

日志跟踪用 Ctrl-C 结束，不会停止服务。修改 `.env` 后重启两个服务：

```bash
sudo systemctl restart remote-agent lark-bridge
```

## 七、接口与会话续接

本节目的：说明 bridge 传给 agent 的输入和 agent 必须返回的输出。

接口是服务器本机的 `POST /task`，请求头使用 `Authorization: Bearer <AGENT_TOKEN>`：

```json
{
  "text": "任务描述",
  "session_id": "可选，上次返回的会话 ID",
  "meta": {
    "source": "feishu",
    "chat_id": "oc_...",
    "open_id": "ou_...",
    "message_id": "om_..."
  }
}
```

正常响应：

```json
{
  "reply": "任务执行结果",
  "session_id": "agent 返回的会话 ID",
  "elapsed": 12.3
}
```

HTTP 服务把 `text` 从 stdin 传给 Claude，并在有 `session_id` 时添加 `--resume`。`bridge` 按飞书 `chat_id` 保存会话 ID；`/new` 清除后续任务使用的 ID，但不取消正在运行的任务。

`/health` 是 GET 健康检查；访问 `/task` 需要 POST 和 JSON，不能用普通 `curl <AGENT_URL>` 的 GET 来验证执行接口。

## 八、服务端排错

本节目的：按连接、鉴权、命令和运行环境定位失败环节。

| 现象 | 检查与处理 |
|---|---|
| 本机不能 SSH 到服务器 | 不影响运行；两项服务在服务器，消息由飞书长连接下发 |
| SDK 一直连接失败 | 检查服务器出站访问飞书 API/WebSocket、应用凭证与 SDK 日志 |
| 群聊 @ 后没有消息 | 确认正确提及、群聊 @ 权限、事件、应用发布、可用范围和其他监听实例 |
| 只有单聊能收到消息 | 核对 `im:message.group_at_msg:readonly` 并发布新版本 |
| 返回“你还没有权限” | 填入这个应用下的用户 `open_id`，重启 `lark-bridge` |
| `/ping` 正常但任务无法执行 | 查看 `remote-agent` 日志；验证 `/health` 和模型 CLI |
| “连不上远端 agent” | 检查 agent 服务和端口；两个进程应在同一主机，URL 为 `127.0.0.1:8787/task` |
| agent 返回 401 | 检查两项服务是否加载同一份 `.env`、令牌是否为空或不一致 |
| 前台可运行，systemd 失败 | 核对 `User`、虚拟环境、配置文件权限、命令绝对路径、Node PATH 和模型环境变量 |
| 启动报 `Address already in use` | 前台服务或其他进程仍占用 8787；先查明占用来源再结束该进程 |
| 返回的目录属于本机电脑 | 电脑上同一应用还有桥接监听；结束后仅保留服务器实例 |
| 模型回复工具权限错误 | 在服务用户下配置所需权限；非交互 CLI 无法现场确认 |
| 改配置后没有生效 | 重启两项服务；已有环境变量优先于 bridge 自动加载的 `.env` |

## 九、更新与停止

本节目的：更新 GitHub 上的代码和教程，同时保留服务器私有配置与会话状态。

使用 Git 克隆的部署，在没有本地未提交改动时执行：

```bash
cd /opt/lark-agent-bridge
git status --short
git pull --ff-only
./.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart remote-agent lark-bridge
```

存在本地改动时先查看并保存改动，不要强制覆盖。`.env` 和 `state/` 不在仓库中。通过 ZIP 部署时，用平台文件上传功能更新代码，保留 `.env`、`.venv/` 和 `state/`。

如果更新了 `deploy/*.service`，按第六节重新安装模板，保留之前对路径、用户和 PATH 的调整，然后运行 `daemon-reload` 再重启。

停止运行：

```bash
sudo systemctl stop lark-bridge remote-agent
```

关闭开机自启：

```bash
sudo systemctl disable lark-bridge remote-agent
```

停机或重启服务可能中断正在执行的任务，先等待任务完成。完成部署后，电脑只需使用飞书，不需要保留本机转发进程。
