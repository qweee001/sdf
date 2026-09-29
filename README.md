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
- **Railway**：`railway.json` 已配置用 Dockerfile 构建、健康检查 `/health`、
  `ON_FAILURE` 最多重试 10 次。环境变量在 Railway 控制台里配，不要进仓库。
