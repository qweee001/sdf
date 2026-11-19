# sdf

一个跑在容器里的 Telegram 多账号机器人服务：用 Telethon 登录账号，用 OpenAI 兼容接口生成回复，
用 SQLite 存会话与记忆，自带一个 FastAPI 控制台。

## 结构

| 路径 | 职责 |
|---|---|
| `app/main.py` | 进程入口：起 FastAPI 服务 + 后台 worker |
| `app/worker.py` | 核心循环：回覆仲裁、主動發言、記憶清理（最大的一个文件） |
| `app/manager.py` | 账号/任务的生命周期管理 |
| `app/telegram_login.py` | Telegram 登录与 session 处理 |
| `app/database.py` | SQLite 存取层（aiosqlite） |
| `app/media.py` | 图片/媒体处理（Pillow） |
| `app/persona.py` | 人设与提示词 |
| `app/voice_assets.py` | 语音素材 |
| `app/crypto.py` | 账号 session 的对称加密 |
| `app/config.py` | 环境变量读取与校验 |
| `app/dashboard.py` | 控制台页面与接口 |
| `app/live_test.py` | 线上自检/演练入口 |
| `tests/` | pytest 测试套件 |

## 环境变量

复制 `.env.example` 为 `.env` 再填值；**`.env` 已在 `.gitignore` 里，不要提交**。

必填：

- `TG_API_ID` / `TG_API_HASH` —— my.telegram.org 申请
- `ACCOUNT_ENCRYPTION_KEY` —— Fernet 密钥，生成方式见 `.env.example` 注释
- `DASHBOARD_PASS` —— 控制台密码

AI 与行为参数（`AI_*`、`MEMORY_*`、`PROACTIVE_*`、`*_PROBABILITY`、打字延迟）都有默认值，
含义见 `.env.example`。生产环境的真实端点/密钥请在部署平台设置，不要写进仓库文件。

## 本地运行

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                           # 填好上面三个必填项
.venv/bin/python -m app.main                   # Windows: .venv\Scripts\python
```

服务默认监听 `PORT`（默认 8000），健康检查在 `/health`。

## 测试

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest -q
```

## 部署

- **Docker**：`docker build -t sdf . && docker run -p 8000:8000 --env-file .env -v sdfdata:/data sdf`
  镜像以非 root 用户 `app` 运行；数据卷挂到 `/data`（`DB_PATH` 默认 `/data/chat.db`）。
- **Railway**：`railway.json` 已配置用 Dockerfile 构建、健康检查 `/ready`、
  `ON_FAILURE` 最多重试 10 次。环境变量在 Railway 控制台里配，不要进仓库。

## 健康度：`/health` 与 `/ready` 的分工

- `/health`：给**人**看。永远 200，只把数据库、账号、静音数、24h 模型调用、
  失败原因计数摊开，不承担状态码语义。
- `/ready`：给**平台与探针**看。同一份判定，但真的哑了（全静音、25 分钟无输出、
  402 欠费）回 503；启动后 `READY_GRACE_SECONDS`（默认 300 秒）内一律 200。
- 外部轮询用 `python tools/ready_probe.py <base_url>`：非 200 即非 0 退出，
  可直接接 cron / 监控。

## 状态与待办（与代码同版本维护）

| 项 | 状态 |
|---|---|
| `/ready` + 外部探针 + 任务自愈 + loop 延迟探针 | 已实装 |
| 图片三段计数（抓到／看懂／被拒）、回复放弃原因上 `/health` | 已实装 |
| 场景状态机（warm→build→peak→aftercare，DB 持久化 + CAS） | 已实装，默认 shadow |
| 守门分层（硬结构／软语感） | 已实装，软语感默认只观察 |
| worker.py 拆分 | hints 三支已外移到 `app/reply_hints.py`；生成体抽段未做 |
| dashboard `_setup_routes`（798 行）拆分 | 已拆成五段方法（公开／读／群组写入／自检／账号） |
| 软语感改由决策层打分（替代正则） | 未做 |
| 成本日报（每小时调用数／p95 延迟／空回率） | 未做 |
| 场景与守门的完整说明 | 见 `docs/reliability_and_scene.md` |
| `analysis_msgs.json` 改存 gz（已 gzip） | 已实装 |

