"""精简版 Telegram 多账号控制台 - 单文件 HTML + FastAPI"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .manager import AccountManager

LOGGER = logging.getLogger("telegram-ai-userbot.dashboard")

COOKIE_NAME = "sdf_session"

# 会话存储: session_id -> {csrf_token, expires_at, username}
session_store: dict[str, dict] = {}

SESSION_TTL = 12 * 60 * 60


class DashboardServer:
    def __init__(self, username: str, password: str, port: int, manager: AccountManager) -> None:
        self.username = username
        self.password = password
        self.port = port
        self.manager = manager
        self.app = FastAPI()
        @self.app.middleware("http")
        async def security_middleware(request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-store"
            return response
        self._setup_routes()

    def _setup_routes(self) -> None:
        @self.app.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(CONTENT)

        @self.app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @self.app.post("/api/login")
        async def login(request: Request) -> JSONResponse:
            data = await request.json()
            user = data.get("username", "")
            pwd = data.get("password", "")
            if user != self.username or pwd != self.password:
                return JSONResponse({"error": "认证失败"}, status_code=401)
            csrf = secrets.token_urlsafe(32)
            session_id = secrets.token_urlsafe(32)
            session_store[session_id] = {
                "csrf_token": csrf,
                "expires_at": time.time() + SESSION_TTL,
                "username": user,
            }
            resp = JSONResponse({"session_id": session_id, "csrf_token": csrf})
            resp.set_cookie(COOKIE_NAME, session_id)
            return resp

        @self.app.get("/api/status")
        async def status(request: Request) -> JSONResponse:
            sid = request.cookies.get(COOKIE_NAME)
            if not sid or sid not in session_store:
                return JSONResponse({"error": "未登录"}, status_code=401)
            s = session_store[sid]
            if time.time() > s["expires_at"]:
                del session_store[sid]
                return JSONResponse({"error": "会话过期"}, status_code=401)
            status_data = await self.manager.status()
            summary = status_data.get("summary", {})
            accounts = status_data.get("accounts", [])
            return JSONResponse({
                "total": summary.get("total", 0),
                "enabled": summary.get("enabled", 0),
                "connected": summary.get("connected", 0),
                "accounts": accounts,
            }, headers={"X-CSRF-Token": s["csrf_token"]})

        @self.app.post("/api/accounts")
        async def create_account(request: Request) -> JSONResponse:
            sid = request.cookies.get(COOKIE_NAME)
            if not sid or sid not in session_store:
                return JSONResponse({"error": "未登录"}, status_code=401)
            data = await request.json()
            result = await self.manager.create_account(data, sid)
            return JSONResponse(result, status_code=201)

        @self.app.post("/api/accounts/{account_id}/start")
        async def start_account(account_id: str, request: Request) -> JSONResponse:
            sid = request.cookies.get(COOKIE_NAME)
            if not sid or sid not in session_store:
                return JSONResponse({"error": "未登录"}, status_code=401)
            await self.manager.start_account(account_id)
            return JSONResponse({"ok": True})

        @self.app.post("/api/accounts/{account_id}/stop")
        async def stop_account(account_id: str, request: Request) -> JSONResponse:
            sid = request.cookies.get(COOKIE_NAME)
            if not sid or sid not in session_store:
                return JSONResponse({"error": "未登录"}, status_code=401)
            await self.manager.stop_account(account_id)
            return JSONResponse({"ok": True})

        @self.app.post("/api/accounts/{account_id}/restart")
        async def restart_account(account_id: str, request: Request) -> JSONResponse:
            sid = request.cookies.get(COOKIE_NAME)
            if not sid or sid not in session_store:
                return JSONResponse({"error": "未登录"}, status_code=401)
            result = await self.manager.restart_account(account_id)
            return JSONResponse(result)

        @self.app.delete("/api/accounts/{account_id}")
        async def delete_account(account_id: str, request: Request) -> JSONResponse:
            sid = request.cookies.get(COOKIE_NAME)
            if not sid or sid not in session_store:
                return JSONResponse({"error": "未登录"}, status_code=401)
            await self.manager.stop_account(account_id)
            return JSONResponse({"ok": True})

    async def start(self) -> None:
        """启动 FastAPI 服务（在独立线程中运行 uvicorn）"""
        import threading
        import urllib.request
        import uvicorn
        
        config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        
        # 在独立线程中运行 uvicorn
        def run_uvicorn():
            import asyncio
            asyncio.run(self._run_server())
        
        thread = threading.Thread(target=run_uvicorn, daemon=True)
        thread.start()
        
        # 等待 uvicorn 启动（使用 asyncio.to_thread 避免阻塞事件循环）
        for _ in range(50):
            try:
                await asyncio.to_thread(urllib.request.urlopen, f"http://127.0.0.1:{self.port}/health")
                return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        
        raise RuntimeError(f"Dashboard failed to start on port {self.port}")
    
    async def _run_server(self) -> None:
        """内部方法：运行 uvicorn server"""
        await self.server.serve()

    async def close(self) -> None:
        """停止 FastAPI 服务"""
        if hasattr(self, "server"):
            self.server.should_exit = True


async def _security_middleware(request: Request, call_next):
    """添加安全响应头"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


CONTENT = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SDF 多账号控制台</title>
<style>
:root {
  --bg: #0a0c10;
  --panel: #12151c;
  --border: #2a2d35;
  --text: #e4e6eb;
  --muted: #8b8fa3;
  --green: #34d399;
  --red: #f87171;
  --yellow: #fbbf24;
  --blue: #60a5fa;
  --accent: #3b82f6;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, "Noto Sans TC", sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}
/* 登录页 */
.login-box {
  max-width: 360px;
  margin: 120px auto;
  padding: 2rem;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
}
.login-box h1 { font-size: 1.5rem; margin-bottom: 1.5rem; text-align: center; }
.login-box input {
  width: 100%;
  padding: 0.75rem;
  margin-bottom: 1rem;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  font-size: 1rem;
}
.login-box button {
  width: 100%;
  padding: 0.75rem;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 1rem;
  cursor: pointer;
}
.login-box button:hover { opacity: 0.9; }
.error { color: var(--red); text-align: center; margin-top: 1rem; }
/* 主界面 */
.container { display: none; max-width: 1200px; margin: 0 auto; padding: 1.5rem; }
.container.active { display: block; }
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 2rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid var(--border);
}
header h1 { font-size: 1.5rem; }
.btn {
  padding: 0.5rem 1rem;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.875rem;
}
.btn-danger { background: var(--red); }
.btn-success { background: var(--green); }
.btn-small { padding: 0.35rem 0.75rem; font-size: 0.8rem; }
/* 统计卡片 */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
  margin-bottom: 2rem;
}
.stat-card {
  background: var(--panel);
  padding: 1.5rem;
  border-radius: 12px;
  border: 1px solid var(--border);
}
.stat-card .label { color: var(--muted); font-size: 0.875rem; }
.stat-card .value { font-size: 2rem; font-weight: bold; margin-top: 0.5rem; }
.stat-card.green .value { color: var(--green); }
.stat-card.red .value { color: var(--red); }
.stat-card.blue .value { color: var(--blue); }
/* 账号列表 */
.account-list { display: flex; flex-direction: column; gap: 1rem; }
.account-card {
  background: var(--panel);
  padding: 1.5rem;
  border-radius: 12px;
  border: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.account-info { flex: 1; }
.account-info .name { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.25rem; }
.account-info .detail { color: var(--muted); font-size: 0.875rem; }
.status-badge {
  display: inline-block;
  padding: 0.25rem 0.75rem;
  border-radius: 20px;
  font-size: 0.75rem;
  font-weight: 600;
}
.status-connected { background: rgba(52,211,153,0.15); color: var(--green); }
.status-disconnected { background: rgba(248,113,113,0.15); color: var(--red); }
.status-enabled { background: rgba(251,191,36,0.15); color: var(--yellow); }
.status-disabled { background: rgba(139,143,163,0.15); color: var(--muted); }
.account-actions { display: flex; gap: 0.5rem; }
/* 空状态 */
.empty {
  text-align: center;
  padding: 3rem;
  color: var(--muted);
}
</style>
</head>
<body>
<!-- 登录页 -->
<div class="login-box" id="loginBox">
  <h1>🤖 SDF 控制台</h1>
  <input type="text" id="username" placeholder="用户名" autocomplete="username">
  <input type="password" id="password" placeholder="密码" autocomplete="current-password">
  <button onclick="doLogin()">登录</button>
  <div class="error" id="loginError"></div>
</div>

<!-- 主界面 -->
<div class="container" id="mainContainer">
  <header>
    <h1>🤖 SDF 多账号控制台</h1>
    <div>
      <button class="btn btn-small" onclick="doRefresh()">刷新</button>
      <button class="btn btn-danger btn-small" onclick="doLogout()">登出</button>
    </div>
  </header>

  <div class="stats" id="statsSection">
    <div class="stat-card">
      <div class="label">总账号数</div>
      <div class="value" id="totalCount">0</div>
    </div>
    <div class="stat-card green">
      <div class="label">已启用</div>
      <div class="value" id="enabledCount">0</div>
    </div>
    <div class="stat-card blue">
      <div class="label">已连接</div>
      <div class="value" id="connectedCount">0</div>
    </div>
  </div>

  <div style="margin-bottom:1rem">
    <h2 style="margin-bottom:1rem">账号列表</h2>
    <div class="account-list" id="accountList">
      <div class="empty">加载中...</div>
    </div>
  </div>
</div>

<script>
let csrfToken = '';
let sessionId = '';

// 登录
async function doLogin() {
  const user = document.getElementById('username').value;
  const pwd = document.getElementById('password').value;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: user, password: pwd})
    });
    const data = await res.json();
    if (res.ok) {
      sessionId = data.session_id;
      csrfToken = data.csrf_token;
      document.cookie = 'sdf_session=' + sessionId;
      showMain();
      loadStatus();
    } else {
      document.getElementById('loginError').textContent = data.error || '登录失败';
    }
  } catch(e) {
    document.getElementById('loginError').textContent = '网络错误';
  }
}

// 登出
async function doLogout() {
  try {
    await fetch('/api/logout', {method: 'POST'});
  } finally {
    document.cookie = 'sdf_session=; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    showLogin();
  }
}

// 加载状态
async function loadStatus() {
  try {
    const res = await fetch('/api/status', {
      headers: {'Cookie': 'sdf_session=' + sessionId}
    });
    const data = await res.json();
    if (data.error) {
      showLogin();
      return;
    }
    // 更新统计
    document.getElementById('totalCount').textContent = data.total || 0;
    document.getElementById('enabledCount').textContent = data.enabled || 0;
    document.getElementById('connectedCount').textContent = data.connected || 0;
    // 渲染账号列表
    renderAccounts(data.accounts || []);
  } catch(e) {
    console.error(e);
  }
}

// 渲染账号列表
function renderAccounts(accounts) {
  const list = document.getElementById('accountList');
  if (!accounts.length) {
    list.innerHTML = '<div class="empty">暂无账号，请在 Telegram 中添加</div>';
    return;
  }
  list.innerHTML = accounts.map(acc => `
    <div class="account-card">
      <div class="account-info">
        <div class="name">${acc.phone_number || '未知号码'}</div>
        <div class="detail">
          ${acc.name || ''}
          ${acc.proactive_enabled ? ' · 主动聊天' : ''}
        </div>
      </div>
      <div style="display:flex;gap:0.75rem;align-items:center">
        <span class="status-badge ${acc.connected ? 'status-connected' : 'status-disconnected'}">
          ${acc.connected ? '已连接' : '已断开'}
        </span>
        <span class="status-badge ${acc.enabled ? 'status-enabled' : 'status-disabled'}">
          ${acc.enabled ? '已启用' : '已禁用'}
        </span>
        <div class="account-actions">
          ${!acc.connected ? '<button class="btn btn-success btn-small" onclick="doStart(\'' + acc.id + '\')">启动</button>' : ''}
          ${acc.connected ? '<button class="btn btn-danger btn-small" onclick="doStop(\'' + acc.id + '\')">停止</button>' : ''}
          ${acc.connected ? '<button class="btn btn-small" onclick="doRestart(\'' + acc.id + '\')">重启</button>' : ''}
        </div>
      </div>
    </div>
  `).join('');
}

// 启动账号
async function doStart(id) {
  await fetch('/api/accounts/' + id + '/start', {
    method: 'POST',
    headers: {'Cookie': 'sdf_session=' + sessionId}
  });
  loadStatus();
}

// 停止账号
async function doStop(id) {
  await fetch('/api/accounts/' + id + '/stop', {
    method: 'POST',
    headers: {'Cookie': 'sdf_session=' + sessionId}
  });
  loadStatus();
}

// 重启账号
async function doRestart(id) {
  await fetch('/api/accounts/' + id + '/restart', {
    method: 'POST',
    headers: {'Cookie': 'sdf_session=' + sessionId}
  });
  loadStatus();
}

// 刷新
async function doRefresh() {
  loadStatus();
}

// 显示主界面
function showMain() {
  document.getElementById('loginBox').style.display = 'none';
  document.getElementById('mainContainer').classList.add('active');
}

// 显示登录页
function showLogin() {
  document.getElementById('loginBox').style.display = 'block';
  document.getElementById('mainContainer').classList.remove('active');
}
</script>
</body>
</html>
"""
