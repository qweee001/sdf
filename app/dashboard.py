from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response


LOGGER = logging.getLogger("telegram-ai-userbot.dashboard")

StatusProvider = Callable[[], Awaitable[dict[str, object]]]
EnabledSetter = Callable[[bool], Awaitable[None]]
MemoryClearer = Callable[[], Awaitable[int]]
GroupFilterSetter = Callable[[bool, frozenset[int]], Awaitable[None]]

COOKIE_NAME = "userbot_dashboard_session"
ACTION_HEADER = "x-dashboard-action"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _token(username: str, password: str) -> str:
    digest = hmac.new(
        password.encode("utf-8"),
        f"{username}:telegram-userbot-dashboard".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Telegram AI 控制台</title>
  <style>
    :root {
      font-family: Inter, "Noto Sans TC", system-ui, sans-serif;
      color: #f7f7f5;
      background: #11120f;
      --panel: #1b1d18;
      --line: #30332b;
      --muted: #a5aa9b;
      --green: #b5f36a;
      --danger: #ff8d80;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background:
      radial-gradient(circle at 20% 0%, #27331a 0, transparent 35%),
      #11120f; }
    button, input { font: inherit; }
    .shell { width: min(980px, calc(100% - 32px)); margin: 0 auto; padding: 36px 0 64px; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 30px; }
    h1 { margin: 0; font-size: clamp(25px, 5vw, 42px); letter-spacing: -0.04em; }
    .eyebrow { color: var(--green); font-size: 12px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
    .status-pill { display: inline-flex; align-items: center; gap: 8px; padding: 9px 13px; border: 1px solid var(--line); border-radius: 999px; color: var(--muted); }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 16px var(--green); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
    .card { grid-column: span 4; padding: 20px; border: 1px solid var(--line); border-radius: 18px; background: rgba(27,29,24,.92); }
    .card.wide { grid-column: span 8; }
    .card.full { grid-column: 1 / -1; }
    .label { color: var(--muted); font-size: 13px; margin-bottom: 9px; }
    .value { font-size: 24px; font-weight: 750; word-break: break-word; }
    .meta { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.55; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
    .btn { border: 1px solid var(--line); border-radius: 12px; padding: 11px 15px; color: #f7f7f5; background: #252821; cursor: pointer; font-weight: 700; }
    .btn.primary { color: #17200d; background: var(--green); border-color: var(--green); }
    .btn.danger { color: var(--danger); }
    .btn:disabled { opacity: .45; cursor: wait; }
    .login { width: min(430px, calc(100% - 32px)); margin: 14vh auto 0; padding: 28px; border: 1px solid var(--line); border-radius: 20px; background: var(--panel); }
    .login h1 { font-size: 30px; margin: 8px 0 22px; }
    .field { display: grid; gap: 7px; margin-bottom: 14px; color: var(--muted); font-size: 13px; }
    .field input { width: 100%; padding: 12px 13px; color: white; background: #11120f; border: 1px solid var(--line); border-radius: 11px; outline: none; }
    .field input:focus { border-color: var(--green); }
    .notice { min-height: 22px; margin-top: 12px; color: var(--danger); font-size: 13px; }
    .hidden { display: none !important; }
    .group-all { display: flex; gap: 9px; align-items: center; margin: 17px 0 12px; font-weight: 700; }
    .group-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .group-option { display: flex; align-items: flex-start; gap: 9px; padding: 12px; border: 1px solid var(--line); border-radius: 12px; color: #e6e8df; background: #151711; }
    .group-option small { display: block; margin-top: 3px; color: var(--muted); }
    footer { margin-top: 22px; color: var(--muted); font-size: 12px; text-align: center; }
    @media (max-width: 720px) {
      .card, .card.wide { grid-column: 1 / -1; }
      header { align-items: flex-start; flex-direction: column; }
      .group-list { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <section id="login" class="login hidden">
    <div class="eyebrow">Private access</div>
    <h1>Telegram AI 控制台</h1>
    <label class="field">帳號<input id="username" autocomplete="username" value="admin"></label>
    <label class="field">密碼<input id="password" type="password" autocomplete="current-password"></label>
    <button id="loginButton" class="btn primary">登入控制台</button>
    <div id="loginNotice" class="notice"></div>
  </section>

  <main id="dashboard" class="shell hidden">
    <header>
      <div>
        <div class="eyebrow">Railway · Production</div>
        <h1>Telegram AI 控制台</h1>
      </div>
      <div class="status-pill"><span class="dot"></span><span id="connection">讀取中</span></div>
    </header>

    <section class="grid">
      <article class="card wide">
        <div class="label">互動狀態</div>
        <div id="enabled" class="value">—</div>
        <div class="meta">暫停後不接收群聊、不主動發言；重新啟用後立即恢復。</div>
        <div class="actions">
          <button id="toggleButton" class="btn primary">切換狀態</button>
          <button id="refreshButton" class="btn">重新整理</button>
          <button id="logoutButton" class="btn">登出</button>
        </div>
      </article>
      <article class="card">
        <div class="label">帳號角色</div>
        <div id="role" class="value">—</div>
        <div id="accountName" class="meta">—</div>
      </article>
      <article class="card">
        <div class="label">24 小時訊息</div>
        <div id="messages" class="value">0</div>
        <div id="groups" class="meta">0 個群組</div>
      </article>
      <article class="card">
        <div class="label">已發送回覆</div>
        <div id="replies" class="value">0</div>
        <div id="errors" class="meta">錯誤 0 次</div>
      </article>
      <article class="card">
        <div class="label">AI 模型</div>
        <div id="model" class="value">—</div>
        <div class="meta">密鑰不會顯示於控制台</div>
      </article>
      <article class="card full">
        <div class="label">回覆群組</div>
        <div id="scope" class="value">—</div>
        <label class="group-all"><input id="allGroups" type="checkbox"> 回覆所有已加入群組</label>
        <div id="groupList" class="group-list"></div>
        <div class="actions">
          <button id="saveGroupsButton" class="btn primary">儲存群組設定</button>
        </div>
      </article>
      <article class="card full">
        <div class="label">記憶管理</div>
        <div id="memory" class="value">—</div>
        <div class="meta">清除後無法復原；不會影響 Telegram Session 或登入狀態。</div>
        <div class="actions">
          <button id="clearButton" class="btn danger">清空全部聊天記憶</button>
        </div>
      </article>
    </section>
    <footer>設定與敏感憑證仍由 Railway Variables 管理</footer>
  </main>
  <script src="/dashboard.js"></script>
</body>
</html>
"""


DASHBOARD_JS = r"""const $ = (id) => document.getElementById(id);
let current = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  if (response.status === 401) throw new Error("unauthorized");
  if (!response.ok) throw new Error((await response.json()).detail || "request failed");
  return response.json();
}

function showLogin() {
  $("dashboard").classList.add("hidden");
  $("login").classList.remove("hidden");
}

function showDashboard() {
  $("login").classList.add("hidden");
  $("dashboard").classList.remove("hidden");
}

function roleName(role) {
  return {
    male_old_member: "男性老成員",
    female_old_member: "女性老成員",
    male_observer: "男性觀望成員",
    female_observer: "女性觀望成員",
  }[role] || role;
}

function render(data) {
  current = data;
  $("connection").textContent = data.connected ? "Telegram 已連線" : "Telegram 未連線";
  $("enabled").textContent = data.enabled ? "群聊互動中" : "已暫停";
  $("toggleButton").textContent = data.enabled ? "暫停互動" : "恢復互動";
  $("role").textContent = roleName(data.role);
  $("accountName").textContent = data.account_name;
  $("messages").textContent = data.message_count.toLocaleString();
  $("groups").textContent = `${data.group_count} 個活躍群組`;
  $("replies").textContent = data.replies_sent.toLocaleString();
  $("errors").textContent = `錯誤 ${data.errors} 次`;
  $("model").textContent = data.model;
  $("scope").textContent = data.all_groups ? "所有已加入群組" : `${data.configured_groups.length} 個指定群組`;
  $("memory").textContent = `保留 ${data.memory_ttl_hours} 小時 · 服務已運行 ${Math.floor(data.uptime_seconds / 60)} 分鐘`;
  $("allGroups").checked = data.all_groups;
  const list = $("groupList");
  list.replaceChildren();
  for (const group of data.joined_groups) {
    const label = document.createElement("label");
    label.className = "group-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = String(group.id);
    input.checked = group.enabled;
    input.disabled = data.all_groups;
    const text = document.createElement("span");
    text.textContent = group.title;
    const id = document.createElement("small");
    id.textContent = String(group.id);
    text.appendChild(id);
    label.append(input, text);
    list.appendChild(label);
  }
}

async function refresh() {
  try {
    render(await api("/api/status"));
    showDashboard();
  } catch (error) {
    if (error.message === "unauthorized") showLogin();
  }
}

$("loginButton").addEventListener("click", async () => {
  $("loginNotice").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({username: $("username").value, password: $("password").value}),
    });
    $("password").value = "";
    await refresh();
  } catch {
    $("loginNotice").textContent = "帳號或密碼不正確";
  }
});

$("password").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("loginButton").click();
});

$("toggleButton").addEventListener("click", async () => {
  if (!current) return;
  $("toggleButton").disabled = true;
  try {
    await api("/api/control", {
      method: "POST",
      headers: {"X-Dashboard-Action": "1"},
      body: JSON.stringify({enabled: !current.enabled}),
    });
    await refresh();
  } finally {
    $("toggleButton").disabled = false;
  }
});

$("allGroups").addEventListener("change", () => {
  for (const input of $("groupList").querySelectorAll("input")) {
    input.disabled = $("allGroups").checked;
    if ($("allGroups").checked) input.checked = true;
  }
});

$("saveGroupsButton").addEventListener("click", async () => {
  const allGroups = $("allGroups").checked;
  const groupIds = [...$("groupList").querySelectorAll("input:checked")].map((input) => Number(input.value));
  if (!allGroups && groupIds.length === 0 && !confirm("沒有選擇任何群組，儲存後將不會在任何群組互動。確定繼續？")) return;
  $("saveGroupsButton").disabled = true;
  try {
    await api("/api/groups", {
      method: "POST",
      headers: {"X-Dashboard-Action": "1"},
      body: JSON.stringify({all_groups: allGroups, group_ids: groupIds}),
    });
    await refresh();
  } finally {
    $("saveGroupsButton").disabled = false;
  }
});

$("clearButton").addEventListener("click", async () => {
  if (!confirm("確定清空目前保存的全部群聊記憶？此操作無法復原。")) return;
  $("clearButton").disabled = true;
  try {
    const result = await api("/api/memory/clear", {
      method: "POST",
      headers: {"X-Dashboard-Action": "1"},
      body: "{}",
    });
    alert(`已清除 ${result.removed} 筆記憶`);
    await refresh();
  } finally {
    $("clearButton").disabled = false;
  }
});

$("refreshButton").addEventListener("click", refresh);
$("logoutButton").addEventListener("click", async () => {
  await api("/api/logout", {method: "POST", headers: {"X-Dashboard-Action": "1"}, body: "{}"});
  showLogin();
});

refresh();
setInterval(refresh, 15000);
"""


class DashboardServer:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        port: int,
        status_provider: StatusProvider,
        enabled_setter: EnabledSetter,
        group_filter_setter: GroupFilterSetter,
        memory_clearer: MemoryClearer,
    ) -> None:
        self.username = username
        self.password = password
        self.port = port
        self.status_provider = status_provider
        self.enabled_setter = enabled_setter
        self.group_filter_setter = group_filter_setter
        self.memory_clearer = memory_clearer
        self.session_token = _token(username, password)
        self.app = self._build_app()
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[None] | None = None

    def _authenticated(self, request: Request) -> bool:
        supplied = request.cookies.get(COOKIE_NAME, "")
        return bool(supplied) and secrets.compare_digest(supplied, self.session_token)

    def _require_auth(self, request: Request) -> JSONResponse | None:
        if self._authenticated(request):
            return None
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    def _require_action(self, request: Request) -> JSONResponse | None:
        unauthorized = self._require_auth(request)
        if unauthorized is not None:
            return unauthorized
        if request.headers.get(ACTION_HEADER) != "1":
            return JSONResponse({"detail": "invalid action request"}, status_code=403)
        return None

    def _build_app(self) -> FastAPI:
        web = FastAPI(
            title="Telegram AI Dashboard",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @web.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(DASHBOARD_HTML, headers=SECURITY_HEADERS)

        @web.get("/dashboard.js")
        async def dashboard_js() -> Response:
            return Response(
                DASHBOARD_JS,
                media_type="application/javascript",
                headers=SECURITY_HEADERS,
            )

        @web.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @web.post("/api/login")
        async def login(request: Request) -> JSONResponse:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, ValueError):
                return JSONResponse({"detail": "invalid request"}, status_code=400)
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            valid = secrets.compare_digest(username, self.username) and secrets.compare_digest(
                password,
                self.password,
            )
            if not valid:
                await asyncio.sleep(0.25)
                return JSONResponse({"detail": "invalid credentials"}, status_code=401)
            response = JSONResponse({"ok": True})
            response.set_cookie(
                COOKIE_NAME,
                self.session_token,
                httponly=True,
                secure=True,
                samesite="strict",
                max_age=12 * 60 * 60,
                path="/",
            )
            return response

        @web.post("/api/logout")
        async def logout(request: Request) -> JSONResponse:
            blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            response = JSONResponse({"ok": True})
            response.delete_cookie(COOKIE_NAME, path="/")
            return response

        @web.get("/api/status")
        async def status(request: Request) -> JSONResponse:
            blocked = self._require_auth(request)
            if blocked is not None:
                return blocked
            return JSONResponse(await self.status_provider())

        @web.post("/api/control")
        async def control(request: Request) -> JSONResponse:
            blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            payload = await request.json()
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                return JSONResponse({"detail": "enabled must be boolean"}, status_code=400)
            await self.enabled_setter(enabled)
            return JSONResponse({"ok": True, "enabled": enabled})

        @web.post("/api/groups")
        async def groups(request: Request) -> JSONResponse:
            blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            payload = await request.json()
            all_groups = payload.get("all_groups")
            group_ids = payload.get("group_ids")
            if not isinstance(all_groups, bool) or not isinstance(group_ids, list):
                return JSONResponse({"detail": "invalid group selection"}, status_code=400)
            if (
                len(group_ids) > 500
                or any(isinstance(item, bool) or not isinstance(item, int) for item in group_ids)
            ):
                return JSONResponse({"detail": "invalid group IDs"}, status_code=400)
            selected = frozenset(group_ids)
            await self.group_filter_setter(all_groups, selected)
            return JSONResponse(
                {"ok": True, "all_groups": all_groups, "group_ids": sorted(selected)}
            )

        @web.post("/api/memory/clear")
        async def clear_memory(request: Request) -> JSONResponse:
            blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            removed = await self.memory_clearer()
            return JSONResponse({"ok": True, "removed": removed})

        return web

    async def start(self) -> None:
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                return
            if self.task.done():
                await self.task
                raise RuntimeError("Dashboard server stopped during startup")
            await asyncio.sleep(0.05)
        raise RuntimeError("Dashboard server did not start in time")

    async def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=10)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        LOGGER.info("Dashboard stopped")
