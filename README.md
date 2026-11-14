# 飞书 ↔ 远端 agent 桥接

在飞书里 @ 一个自建应用机器人 → 消息转给服务器上的 Claude（或任意 CLI）→ 干完把结果发回飞书。

```
飞书群/单聊
   │  @机器人 + 文本
   ▼
[长连接] bridge（WebSocket，无需公网 IP、无需域名、无需验签）
   │  HTTP + Bearer Token
   ▼
remote_agent_server.py ── subprocess ──> claude -p（带 --resume 续会话）
   │
   └── 结果回传 ──> 飞书（分片发送）
```

已实现的行为：
- 只认 @ 本机器人的消息；单聊直接触发
- open_id 白名单，未授权直接拒并回显 open_id
- 事件 3 秒内 ACK，任务丢线程池异步跑（先回「收到」，跑完再发结果）
- 同会话串行（一次只跑一个任务）、跨会话并行
- 会话续接：`chat_id → session_id` 落盘，重启不丢上下文；`/new` 清空
- 长结果自动分片，超出上限截断并提示
- 飞书事件重推按 message_id 去重

---

## 一、先在本地跑通链路（不碰飞书，2 分钟）

```bash
python3 -m pip install -r requirements.txt

# 终端 A：假的远端 agent，只回显，不调模型
python3 remote_agent_server.py --dry-run --port 8787 --token test-token

# 终端 B：把模拟事件喂给桥接服务
AGENT_URL=http://127.0.0.1:8787/task AGENT_TOKEN=test-token python3 -m bridge.app --mock
```

在终端 B 逐行贴（每行一个 JSON，Ctrl-D 结束）：

```json
{"text":"帮我跑一下并发测试，整理成一句话总结"}
{"text":"/new"}
{"text":"只看 p99 延迟"}
{"text":"我是路人","open_id":"ou_stranger"}
```

期望：第一条回「收到，已转给远端 agent…」，1 秒后回「任务完成（1.0s）」；`/new` 回「已开新会话」；路人回「你还没有权限（open_id: ou_stranger）」。

跑单测：`python3 -m unittest discover -s tests`（23 个用例，覆盖解析、分片、鉴权、去重、并发、失败回传）。

---

## 二、建飞书应用（这步错后面全白费）

1. 开放平台 → 创建企业自建应用 → 记下 **App ID / App Secret**（凭证与基础信息）
2. 添加应用能力 → **机器人**
3. 权限管理 → 开通下面这些（搜索 `im:message` 一把勾上）：
   - 以应用身份发消息
   - 接收单聊消息（`im:message.p2p_msg`）
   - **接收群聊中 @ 机器人 的消息**（`im:message.group_at_msg`）← 群聊没这个就不回话
   - 可选：读取群信息、上传图片/文件
4. 事件与回调 → 事件订阅 → **订阅方式选「使用长连接接收事件」** → 添加事件 **接收消息 `im.message.receive_v1`**
   - 长连接模式下「请求地址 / Encrypt Key / Verification Token」都不用填
5. 版本管理与发布 → 创建版本 → 申请发布 → 等管理员审核通过（**权限和事件在发布后才生效**）
6. 把机器人拉进群，或直接单聊

拿到自己的 open_id：先把 `ALLOW_OPEN_IDS` 留空启动，在群里 @ 一次机器人，日志里会打印
`未授权用户 open_id=ou_xxx`，把它填进 `.env` 再重启即可。

---

## 三、部署飞书侧

```bash
cp .env.example .env
vi .env          # 填 FEISHU_APP_ID / FEISHU_APP_SECRET / ALLOW_OPEN_IDS / AGENT_URL / AGENT_TOKEN
python3 -m bridge.app
```

`.env` 里几个关键项：

| 变量 | 说明 |
|---|---|
| `ALLOW_OPEN_IDS` | 谁能指挥这台机器，逗号分隔；**留空 = 拒绝所有人** |
| `ALLOW_CHAT_IDS` | 允许的会话，留空不限制 |
| `AGENT_TRANSPORT` | `http`（推荐）/ `ssh` / `local` |
| `AGENT_URL` / `AGENT_TOKEN` | http 模式下远端接口和令牌 |
| `SSH_TARGET` / `SSH_COMMAND` | ssh 模式：`root@1.2.3.4` + `claude -p --output-format text` |
| `AGENT_TIMEOUT` | 单任务超时，默认 900s |
| `WORKERS` | 跨会话并行数 |
| `MAX_CHUNK_CHARS` / `MAX_CHUNKS` | 单条消息上限与最多条数 |

常驻（systemd，见 `deploy/lark-bridge.service`）：

```bash
sudo cp deploy/lark-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lark-bridge
journalctl -u lark-bridge -f
```

---

## 四、部署远端（Claude 那台机器）

```bash
# 自测
python3 remote_agent_server.py --dry-run --port 8787 --token <TOKEN>

# 正式：--workdir 决定 Claude 在哪个仓库里干活
python3 remote_agent_server.py --port 8787 --token <TOKEN> \
    --workdir /srv/repo \
    --command "claude -p --output-format json"
```

接口：

```
POST /task   Authorization: Bearer <TOKEN>
{"text": "...", "session_id": "可选", "meta": {...}}
→ {"reply": "...", "session_id": "uuid", "elapsed": 12.3}

GET /health → {"ok": true}
```

**安全要点**：不要 `--host 0.0.0.0` 裸奔在公网。两种稳妥做法：

1. 只监听 `127.0.0.1`，用 SSH 隧道从桥接机上打通：`ssh -L 8787:127.0.0.1:8787 user@server -N`
2. 必须跨机时，只开安全组给桥接机 IP，并一定设 `--token`

Claude 命令不用 `--resume` 之外的花活：桥接侧把 `session_id` 传回来，服务端自动拼 `--resume <id>`，所以同一个飞书会话能连续对话。

---

## 五、飞书里怎么用

| 你发 | 效果 |
|---|---|
| `@机器人 把昨天的测试日志整理成结论` | 转给远端，跑完回结果 |
| `@机器人 /new` | 丢掉上下文，从新会话开始 |
| `@机器人 /status` | 看后端类型、当前 session、是否在跑 |
| `@机器人 /ping` | 确认机器人活着 |
| `@机器人 /help` | 用法说明 |

并发规则：同一个群同一时间只跑一个任务（第二次会回「上一个任务还在跑」），不同群/单聊并行。

---

## 六、排错对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 日志里完全没有事件 | 长连接没建立 / 事件没订阅 / 版本没发布 | 看启动日志有没有「长连接启动中」；开放平台确认 `im.message.receive_v1` 已订阅且版本已过审 |
| 群聊 @ 了不回 | 缺 `im:message.group_at_msg` | 补权限后重新发版 |
| 单聊不回 | 缺 `im:message.p2p_msg` | 同上 |
| 回「你还没有权限」 | open_id 不在白名单 | 把日志里的 open_id 填进 `ALLOW_OPEN_IDS` |
| 报 `code=99991672` / `230027` | 权限不足或被管理员限制 | 开放平台补权限、申请审核 |
| 报 `code=230002` | 机器人不在该会话 | 把机器人拉进群 |
| 一直「任务完成」但内容是空的 | 远端返回格式不对 | 远端要返回 `{"reply": "..."}` |
| 「连不上远端 agent」 | URL/端口/防火墙 | `curl <AGENT_URL> -H 'Authorization: Bearer ...'` 自测 |
| 「远端 agent 鉴权失败」 | `AGENT_TOKEN` 两边不一致 | 对齐 token |
| 结果只出来一截 | 消息超长 | 调小 `MAX_CHUNK_CHARS`，或让远端把长结果写成文档发链接 |
| 任务跑一半被新消息顶掉 | 同会话串行 | 等它出结果，或先 `/new` |

---

## 七、安全清单（上线前逐条确认）

- [ ] `ALLOW_OPEN_IDS` 只放自己（和可信的人），**别留空**
- [ ] 远端服务只监听内网 / 走 SSH 隧道，`--token` 必设且随机
- [ ] 远端 agent 用独立账号或容器跑，不要 root，不要挂生产凭证
- [ ] `--workdir` 指到专用仓库，避免它在系统目录乱改
- [ ] 定期看日志里有没有陌生的 open_id 尝试

---

## 八、文件说明

| 文件 | 作用 |
|---|---|
| `bridge/app.py` | 入口：长连接收事件；`--mock` 离线联调 |
| `bridge/dispatch.py` | 鉴权、去重、指令、单会话串行、异步执行、结果回发 |
| `bridge/feishu.py` | token 缓存、回复/发送、分片、错误码提示 |
| `bridge/agent.py` | 三种后端：HTTP / SSH / 本地命令；会话落盘 |
| `bridge/text.py` | 纯函数：@ 剥离、富文本提取、分片、指令解析 |
| `remote_agent_server.py` | 服务器侧：把 `claude -p` 包成 HTTP，带 session 续接 |
| `tests/test_core.py` | 离线单测 |
| `deploy/*.service` | systemd 单元 |

想改行为基本只动 `.env`，代码不用碰。
