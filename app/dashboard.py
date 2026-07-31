from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .manager import AccountConflictError, AccountManager, AccountNotFoundError
from .telegram_login import (
    TelegramLoginConflict,
    TelegramLoginExpired,
    TelegramLoginRateLimit,
    TelegramLoginUnavailable,
)


LOGGER = logging.getLogger("telegram-ai-userbot.dashboard")

COOKIE_NAME = "userbot_dashboard_session"
CSRF_HEADER = "x-csrf-token"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 5
MAX_REQUEST_BYTES = 128 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(slots=True)
class DashboardSession:
    session_id: str
    csrf_token: str
    expires_at: float


class LoginRateLimiter:
    def __init__(self) -> None:
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, client_ip: str) -> int:
        now = time.monotonic()
        attempts = self.failures[client_ip]
        while attempts and now - attempts[0] >= LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) < LOGIN_MAX_FAILURES:
            if not attempts:
                self.failures.pop(client_ip, None)
            return 0
        return max(1, int(LOGIN_WINDOW_SECONDS - (now - attempts[0])))

    def add_failure(self, client_ip: str) -> None:
        self.failures[client_ip].append(time.monotonic())

    def clear(self, client_ip: str) -> None:
        self.failures.pop(client_ip, None)


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>Telegram AI 多帳號控制台</title>
  <style>
    :root {
      font-family: Inter, "Noto Sans TC", system-ui, sans-serif;
      color: #f7f8f2;
      background: #10120e;
      --panel: #1a1d17;
      --panel-2: #22261d;
      --line: #343a2d;
      --muted: #a8af9d;
      --accent: #b8f46d;
      --accent-dark: #1b260f;
      --danger: #ff9387;
      --warning: #ffd078;
      --blue: #8fc8ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 10% 0%, #28361b 0, transparent 32rem),
        #10120e;
    }
    button, input, select, textarea { font: inherit; }
    button { cursor: pointer; }
    .hidden { display: none !important; }
    .login {
      width: min(430px, calc(100% - 30px));
      margin: 13vh auto 0;
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: rgba(26, 29, 23, .96);
      box-shadow: 0 24px 80px rgba(0, 0, 0, .3);
    }
    .eyebrow {
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .13em;
      text-transform: uppercase;
    }
    h1 { margin: 8px 0 24px; font-size: clamp(27px, 4vw, 40px); letter-spacing: -.04em; }
    h2 { margin: 0; font-size: 22px; letter-spacing: -.02em; }
    h3 { margin: 0 0 14px; font-size: 17px; }
    .login .field { margin-bottom: 14px; }
    .app {
      width: min(1500px, calc(100% - 30px));
      margin: 0 auto;
      padding: 26px 0 60px;
    }
    .topbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 18px;
    }
    .topbar h1 { margin-bottom: 0; }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(100px, 1fr));
      gap: 10px;
      width: min(620px, 100%);
    }
    .summary-item, .panel {
      border: 1px solid var(--line);
      background: rgba(26, 29, 23, .94);
      border-radius: 17px;
    }
    .summary-item { padding: 13px 15px; }
    .summary-item span { display: block; color: var(--muted); font-size: 12px; }
    .summary-item strong { display: block; margin-top: 4px; font-size: 21px; }
    .workspace {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      gap: 14px;
      align-items: start;
    }
    .sidebar { padding: 14px; position: sticky; top: 14px; }
    .sidebar-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }
    .account-list { display: grid; gap: 7px; }
    .account-item {
      width: 100%;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 10px;
      align-items: center;
      padding: 11px;
      text-align: left;
      color: inherit;
      border: 1px solid transparent;
      border-radius: 12px;
      background: transparent;
    }
    .account-item:hover, .account-item.active {
      border-color: var(--line);
      background: var(--panel-2);
    }
    .account-item strong, .account-item small {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .account-item small { margin-top: 3px; color: var(--muted); }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #666d5d;
    }
    .dot.online { background: var(--accent); box-shadow: 0 0 14px rgba(184, 244, 109, .65); }
    .dot.error { background: var(--danger); }
    .content { min-width: 0; }
    .panel { padding: 20px; margin-bottom: 14px; }
    .account-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
    }
    .account-head .sub { color: var(--muted); margin-top: 6px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .btn {
      min-height: 40px;
      padding: 9px 14px;
      border: 1px solid var(--line);
      border-radius: 11px;
      color: #f7f8f2;
      background: #282d22;
      font-weight: 720;
    }
    .btn:hover { border-color: #59634c; }
    .btn.primary { color: #17200d; border-color: var(--accent); background: var(--accent); }
    .btn.danger { color: var(--danger); }
    .btn.small { min-height: 34px; padding: 6px 10px; font-size: 13px; }
    .btn:disabled { opacity: .5; cursor: wait; }
    .status-line {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 9px;
      margin-top: 18px;
    }
    .metric { padding: 11px; border: 1px solid var(--line); border-radius: 12px; background: #151711; }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong { display: block; margin-top: 4px; font-size: 17px; overflow-wrap: anywhere; }
    .section-title { margin-bottom: 16px; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 12px;
    }
    .field { grid-column: span 6; display: grid; gap: 6px; color: var(--muted); font-size: 13px; }
    .field.third { grid-column: span 4; }
    .field.quarter { grid-column: span 3; }
    .field.full { grid-column: 1 / -1; }
    .field input, .field select, .field textarea {
      width: 100%;
      padding: 11px 12px;
      color: #fff;
      border: 1px solid var(--line);
      border-radius: 10px;
      outline: none;
      background: #11130f;
    }
    .field textarea { min-height: 90px; resize: vertical; line-height: 1.5; }
    .field input:focus, .field select:focus, .field textarea:focus { border-color: var(--accent); }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 40px;
      color: #e7e9df;
      font-size: 14px;
    }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.55; }
    .notice {
      min-height: 22px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .notice.error { color: var(--danger); }
    .notice.success { color: var(--accent); }
    .notice.warning { color: var(--warning); }
    .divider { height: 1px; margin: 20px 0; background: var(--line); }
    .group-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0;
    }
    .group-item {
      display: flex;
      align-items: flex-start;
      gap: 9px;
      padding: 11px;
      border: 1px solid var(--line);
      border-radius: 11px;
      background: #151711;
    }
    .group-item span { min-width: 0; overflow-wrap: anywhere; }
    .group-item small { display: block; margin-top: 3px; color: var(--muted); }
    .empty {
      padding: 30px 18px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 14px;
    }
    .add-panel { border-color: #53663c; }
    .row-between { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .danger-zone { border-color: #553b35; }
    footer { padding: 6px; color: var(--muted); text-align: center; font-size: 12px; }
    @media (max-width: 1050px) {
      .topbar { flex-direction: column; }
      .summary { width: 100%; }
      .status-line { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .app { width: min(100% - 18px, 1500px); padding-top: 16px; }
      .workspace { grid-template-columns: 1fr; }
      .sidebar { position: static; }
      .account-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .account-head, .row-between { flex-direction: column; align-items: stretch; }
      .field, .field.third, .field.quarter { grid-column: 1 / -1; }
      .group-list { grid-template-columns: 1fr; }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .status-line { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 440px) {
      .account-list { grid-template-columns: 1fr; }
      .status-line { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <section id="login" class="login hidden">
    <div class="eyebrow">Private access</div>
    <h1>多帳號控制台</h1>
    <label class="field full">管理員帳號<input id="loginUsername" autocomplete="username" value="admin"></label>
    <label class="field full">管理員密碼<input id="loginPassword" type="password" autocomplete="current-password"></label>
    <button id="loginButton" class="btn primary">登入控制台</button>
    <div id="loginNotice" class="notice"></div>
  </section>

  <main id="app" class="app hidden">
    <header class="topbar">
      <div>
        <div class="eyebrow">Telegram AI · Multi account</div>
        <h1>多帳號控制台</h1>
      </div>
      <section class="summary" aria-label="帳號摘要">
        <div class="summary-item"><span>帳號總數</span><strong id="summaryTotal">0</strong></div>
        <div class="summary-item"><span>已啟用</span><strong id="summaryEnabled">0</strong></div>
        <div class="summary-item"><span>已連線</span><strong id="summaryConnected">0</strong></div>
        <div class="summary-item"><span>記憶時間</span><strong id="summaryMemory">24h</strong></div>
      </section>
    </header>

    <div class="workspace">
      <aside class="panel sidebar">
        <div class="sidebar-head">
          <h2>Telegram 帳號</h2>
          <button id="showAddButton" class="btn small primary">新增</button>
        </div>
        <div id="accountList" class="account-list"></div>
        <div class="divider"></div>
        <div class="actions">
          <button id="refreshButton" class="btn small">重新整理</button>
          <button id="logoutButton" class="btn small">登出</button>
        </div>
      </aside>

      <section class="content">
        <article id="addPanel" class="panel add-panel hidden">
          <div class="row-between section-title">
            <div>
              <h2>新增 Telegram 帳號</h2>
              <div class="hint">推薦直接用手機號碼與 Telegram 驗證碼登入；Session 會在伺服器自動加密保存。</div>
            </div>
            <button id="cancelAddButton" class="btn small">取消</button>
          </div>
          <form id="addForm" class="form-grid">
            <label class="field">登入方式
              <select id="addLoginMode">
                <option value="phone" selected>手機號碼＋Telegram 驗證碼（推薦）</option>
                <option value="session">TG_SESSION_STRING（進階）</option>
              </select>
            </label>
            <div id="addPhonePanel" class="field full">
              <label class="field">Telegram 手機號碼
                <input id="addPhone" type="tel" required maxlength="20" autocomplete="tel" placeholder="+886912345678">
              </label>
              <div id="addPhoneHint" class="hint">請包含國碼；驗證碼會由 Telegram 官方帳號或 App 傳送。</div>
            </div>
            <div id="addCodePanel" class="field full hidden">
              <label class="field">Telegram 驗證碼
                <input id="addCode" type="text" disabled maxlength="8" inputmode="numeric" autocomplete="one-time-code" placeholder="輸入 Telegram 傳送的數字">
              </label>
              <button id="restartPhoneLoginButton" class="btn small" type="button">取消並重新開始</button>
            </div>
            <div id="addPasswordPanel" class="field full hidden">
              <label class="field">Telegram 兩步驗證密碼
                <input id="addTwoFactorPassword" type="password" disabled maxlength="512" autocomplete="off">
              </label>
              <div class="hint">只有已啟用 Telegram 兩步驗證的帳號才需要。</div>
            </div>
            <div id="addSessionPanel" class="field full hidden">
              <label class="field full">TG_SESSION_STRING
                <textarea id="addSession" disabled maxlength="12000" autocomplete="off"></textarea>
              </label>
              <div class="hint">進階備援方式；內容只傳到伺服器並加密保存，不會再次顯示。</div>
            </div>
            <label class="field">顯示名稱（可留白）
              <input id="addLabel" maxlength="60" autocomplete="off">
            </label>
            <label class="field quarter">性別
              <select id="addGender"><option value="male">男性</option><option value="female">女性</option></select>
            </label>
            <label class="field quarter">角色
              <select id="addStage"><option value="observer">觀望成員</option><option value="old_member">老成員</option></select>
            </label>
            <label class="field">角色風格
              <input id="addStyle" maxlength="500" placeholder="例如：自然、穩重、偶爾幽默">
            </label>
            <label class="field">任務名稱
              <input id="addTaskName" maxlength="120" value="一般群聊互動">
            </label>
            <label class="field full">任務資訊
              <textarea id="addTaskInfo" maxlength="3000" placeholder="說明這個帳號應關注的話題與互動目標"></textarea>
            </label>
            <label class="field">AI Base URL（可留白使用系統預設）
              <input id="addBaseUrl" inputmode="url" maxlength="500" placeholder="https://api.openai.com/v1">
            </label>
            <label class="field">模型（可留白使用系統預設）
              <input id="addModel" maxlength="200" placeholder="模型名稱">
            </label>
            <label class="field full">帳號專用 AI API Key（系統未設定共用 Key 時必填）
              <input id="addApiKey" type="password" maxlength="12000" autocomplete="new-password">
            </label>
            <label class="check field full"><input id="addEnabled" type="checkbox" checked> 建立後立即啟用</label>
            <div class="actions field full">
              <button id="addSubmitButton" class="btn primary" type="submit">傳送 Telegram 驗證碼</button>
            </div>
          </form>
          <div id="addNotice" class="notice"></div>
        </article>

        <article id="emptyPanel" class="panel empty">請從左側選擇帳號，或新增第一個帳號。</article>

        <div id="accountPanels" class="hidden">
          <article class="panel">
            <div class="account-head">
              <div>
                <div class="eyebrow" id="selectedState">—</div>
                <h2 id="selectedLabel">—</h2>
                <div id="selectedIdentity" class="sub">—</div>
              </div>
              <div class="actions">
                <button id="controlButton" class="btn primary">切換啟用狀態</button>
                <button id="restartButton" class="btn">重新啟動</button>
                <button id="modelTestButton" class="btn">測試模型</button>
              </div>
            </div>
            <div class="status-line">
              <div class="metric"><span>連線</span><strong id="metricConnection">—</strong></div>
              <div class="metric"><span>24 小時訊息</span><strong id="metricMessages">0</strong></div>
              <div class="metric"><span>已回覆</span><strong id="metricReplies">0</strong></div>
              <div class="metric"><span>群組</span><strong id="metricGroups">0</strong></div>
              <div class="metric"><span>錯誤</span><strong id="metricErrors">0</strong></div>
            </div>
            <div id="accountNotice" class="notice"></div>
          </article>

          <article class="panel">
            <h3>角色、任務與模型</h3>
            <form id="settingsForm" class="form-grid">
              <label class="field">帳號名稱
                <input id="editLabel" required maxlength="60">
              </label>
              <label class="field quarter">性別
                <select id="editGender"><option value="male">男性</option><option value="female">女性</option></select>
              </label>
              <label class="field quarter">角色
                <select id="editStage"><option value="old_member">老成員</option><option value="observer">觀望成員</option></select>
              </label>
              <label class="field full">角色風格
                <textarea id="editStyle" maxlength="500"></textarea>
              </label>
              <label class="field">任務名稱
                <input id="editTaskName" required maxlength="120">
              </label>
              <label class="field full">任務資訊
                <textarea id="editTaskInfo" maxlength="3000"></textarea>
              </label>
              <label class="field">AI Base URL
                <input id="editBaseUrl" required maxlength="500" inputmode="url">
              </label>
              <label class="field">模型名稱
                <input id="editModel" required maxlength="200">
              </label>
              <label class="field full">新的帳號專用 AI API Key
                <input id="editApiKey" type="password" maxlength="12000" autocomplete="new-password" placeholder="留白表示保留目前設定">
              </label>
              <label class="check field"><input id="clearApiKey" type="checkbox"> 清除帳號專用 API Key，改用 Railway 共用 Key</label>
              <div id="apiKeyState" class="hint field">—</div>

              <div class="field full"><div class="divider"></div><h3>回覆與主動發言</h3></div>
              <label class="field third">一般訊息回覆機率（0–1）
                <input id="editProbability" type="number" min="0" max="1" step="0.01" required>
              </label>
              <label class="field third">最短輸入延遲（秒）
                <input id="editDelayMin" type="number" min="0" max="60" step="0.1" required>
              </label>
              <label class="field third">最長輸入延遲（秒）
                <input id="editDelayMax" type="number" min="0" max="60" step="0.1" required>
              </label>
              <label class="check field"><input id="editReplyMention" type="checkbox"> 被提及時回覆</label>
              <label class="check field"><input id="editReplyReply" type="checkbox"> 被回覆時回覆</label>
              <label class="check field"><input id="editProactive" type="checkbox"> 啟用主動發言</label>
              <label class="field quarter">安靜多久後主動發言（分鐘）
                <input id="editIdle" type="number" min="1" max="1440" step="1" required>
              </label>
              <label class="field quarter">最短主動間隔（分鐘）
                <input id="editIntervalMin" type="number" min="1" max="1440" step="1" required>
              </label>
              <label class="field quarter">最長主動間隔（分鐘）
                <input id="editIntervalMax" type="number" min="1" max="1440" step="1" required>
              </label>
              <label class="field quarter">每日主動發言上限
                <input id="editProactiveMax" type="number" min="0" max="200" step="1" required>
              </label>
              <div class="actions field full">
                <button id="saveSettingsButton" class="btn primary" type="submit">儲存帳號設定</button>
              </div>
            </form>
            <div id="settingsNotice" class="notice"></div>
          </article>

          <article class="panel">
            <div class="row-between">
              <div>
                <h3>回覆群組</h3>
                <div class="hint">只會顯示這個 Telegram 帳號目前已加入的群組。</div>
              </div>
              <label class="check"><input id="allGroups" type="checkbox"> 回覆所有群組</label>
            </div>
            <div id="groupList" class="group-list"></div>
            <div class="actions">
              <button id="saveGroupsButton" class="btn primary">儲存群組範圍</button>
            </div>
            <div id="groupsNotice" class="notice"></div>
          </article>

          <article class="panel danger-zone">
            <h3>記憶管理</h3>
            <div class="hint">清除聊天記憶不會刪除帳號、Telegram Session 或模型設定。此操作無法復原。</div>
            <div class="actions">
              <button id="clearMemoryButton" class="btn danger">清除這個帳號的聊天記憶</button>
            </div>
          </article>
        </div>
      </section>
    </div>
    <footer>敏感憑證不會顯示於控制台；帳號不提供刪除功能。</footer>
  </main>
  <script src="/dashboard.js"></script>
</body>
</html>
"""


DASHBOARD_JS = r"""const $ = (id) => document.getElementById(id);
let dashboardState = null;
let selectedAccountId = "";
let csrfToken = "";
let formDirty = false;
let groupsDirty = false;
let telegramAuthId = "";
let telegramAuthState = "idle";

function showLogin() {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
}

function setNotice(id, message, kind = "") {
  const node = $(id);
  node.textContent = message || "";
  node.className = `notice${kind ? ` ${kind}` : ""}`;
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = {...(options.headers || {})};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    method,
    headers,
  });
  const nextToken = response.headers.get("X-CSRF-Token");
  if (nextToken) csrfToken = nextToken;
  let data = {};
  if (response.headers.get("content-type")?.includes("application/json")) {
    data = await response.json();
  }
  if (response.status === 401) {
    csrfToken = "";
    showLogin();
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const error = new Error(data.detail || `請求失敗（${response.status}）`);
    error.status = response.status;
    error.retryAfter = data.retry_after || 0;
    throw error;
  }
  if (data.csrf_token) csrfToken = data.csrf_token;
  return data;
}

function roleName(account) {
  const gender = account.gender === "female" ? "女性" : "男性";
  const stage = account.stage === "old_member" ? "老成員" : "觀望成員";
  return `${gender}${stage}`;
}

function stateName(account) {
  if (!account.enabled) return "已停用";
  return {
    online: "運行中",
    starting: "連線中",
    connecting: "連線中",
    stopped: "已停止",
    disabled: "已停用",
    error: "發生錯誤",
  }[account.state] || account.state || "未知";
}

function selectedAccount() {
  return dashboardState?.accounts.find((account) => account.id === selectedAccountId) || null;
}

function numberValue(id) {
  return Number($(id).value);
}

function integerValue(id) {
  return Number.parseInt($(id).value, 10);
}

function createAccountItem(account) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `account-item${account.id === selectedAccountId ? " active" : ""}`;
  const dot = document.createElement("span");
  dot.className = `dot${account.connected ? " online" : account.state === "error" ? " error" : ""}`;
  const copy = document.createElement("span");
  const title = document.createElement("strong");
  title.textContent = account.label;
  const meta = document.createElement("small");
  meta.textContent = `${stateName(account)} · ${roleName(account)}`;
  copy.append(title, meta);
  button.append(dot, copy);
  button.addEventListener("click", () => {
    selectedAccountId = account.id;
    formDirty = false;
    groupsDirty = false;
    renderDashboard();
  });
  return button;
}

function renderAccountList() {
  const list = $("accountList");
  list.replaceChildren();
  for (const account of dashboardState?.accounts || []) {
    list.appendChild(createAccountItem(account));
  }
  if (!dashboardState?.accounts.length) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "尚未新增帳號";
    list.appendChild(empty);
  }
}

function fillEditor(account) {
  $("editLabel").value = account.label || "";
  $("editGender").value = account.gender;
  $("editStage").value = account.stage;
  $("editStyle").value = account.style || "";
  $("editTaskName").value = account.task_name || "";
  $("editTaskInfo").value = account.task_info || "";
  $("editBaseUrl").value = account.ai_base_url || "";
  $("editModel").value = account.ai_model || "";
  $("editApiKey").value = "";
  $("clearApiKey").checked = false;
  $("editProbability").value = String(account.group_reply_probability);
  $("editDelayMin").value = String(account.typing_delay_min_seconds);
  $("editDelayMax").value = String(account.typing_delay_max_seconds);
  $("editReplyMention").checked = account.reply_on_mention;
  $("editReplyReply").checked = account.reply_on_reply;
  $("editProactive").checked = account.proactive_enabled;
  $("editIdle").value = String(account.proactive_idle_minutes);
  $("editIntervalMin").value = String(account.proactive_min_interval_minutes);
  $("editIntervalMax").value = String(account.proactive_max_interval_minutes);
  $("editProactiveMax").value = String(account.max_proactive_per_day);
  $("apiKeyState").textContent = account.has_custom_api_key
    ? "目前使用帳號專用 API Key（內容不會顯示）"
    : "目前使用 Railway 共用 API Key";
}

function renderGroups(account) {
  $("allGroups").checked = account.all_groups;
  const list = $("groupList");
  list.replaceChildren();
  const groups = account.joined_groups || [];
  if (!groups.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = account.connected ? "這個帳號目前沒有可用群組" : "帳號連線後才會載入群組";
    list.appendChild(empty);
    return;
  }
  for (const group of groups) {
    const label = document.createElement("label");
    label.className = "group-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(group.id);
    checkbox.checked = Boolean(group.enabled);
    checkbox.disabled = account.all_groups;
    const copy = document.createElement("span");
    copy.textContent = String(group.title || group.id);
    const id = document.createElement("small");
    id.textContent = String(group.id);
    copy.appendChild(id);
    label.append(checkbox, copy);
    list.appendChild(label);
  }
}

function renderSelected(account) {
  $("emptyPanel").classList.add("hidden");
  $("accountPanels").classList.remove("hidden");
  $("selectedState").textContent = stateName(account);
  $("selectedLabel").textContent = account.label;
  $("selectedIdentity").textContent = `${account.telegram_name || "Telegram 帳號"} · ${roleName(account)} · ID ${account.telegram_user_id}`;
  $("controlButton").textContent = account.enabled ? "停用帳號" : "啟用帳號";
  $("metricConnection").textContent = account.connected ? "已連線" : "未連線";
  $("metricMessages").textContent = Number(account.message_count || 0).toLocaleString();
  $("metricReplies").textContent = Number(account.replies_sent || 0).toLocaleString();
  $("metricGroups").textContent = String(account.joined_groups?.length || 0);
  $("metricErrors").textContent = String(account.errors || 0);
  setNotice("accountNotice", account.last_error || "", account.last_error ? "error" : "");
  fillEditor(account);
  renderGroups(account);
}

function renderDashboard() {
  const summary = dashboardState?.summary || {};
  $("summaryTotal").textContent = String(summary.total || 0);
  $("summaryEnabled").textContent = String(summary.enabled || 0);
  $("summaryConnected").textContent = String(summary.connected || 0);
  $("summaryMemory").textContent = `${summary.memory_ttl_hours || 24}h`;
  if (!selectedAccountId && dashboardState?.accounts.length) {
    selectedAccountId = dashboardState.accounts[0].id;
  }
  if (selectedAccountId && !selectedAccount()) {
    selectedAccountId = dashboardState?.accounts[0]?.id || "";
  }
  renderAccountList();
  const account = selectedAccount();
  if (account) renderSelected(account);
  else {
    $("accountPanels").classList.add("hidden");
    $("emptyPanel").classList.remove("hidden");
  }
  showApp();
}

async function refresh() {
  const data = await api("/api/status");
  dashboardState = data;
  renderDashboard();
}

async function runButton(button, operation) {
  const oldDisabled = button.disabled;
  button.disabled = true;
  try {
    return await operation();
  } finally {
    button.disabled = oldDisabled;
  }
}

$("loginButton").addEventListener("click", async () => {
  setNotice("loginNotice", "");
  await runButton($("loginButton"), async () => {
    try {
      const result = await api("/api/login", {
        method: "POST",
        body: JSON.stringify({
          username: $("loginUsername").value,
          password: $("loginPassword").value,
        }),
      });
      csrfToken = result.csrf_token || csrfToken;
      $("loginPassword").value = "";
      await refresh();
    } catch (error) {
      if (error.message !== "unauthorized") setNotice("loginNotice", error.message, "error");
      else setNotice("loginNotice", "帳號或密碼不正確", "error");
    }
  });
});

$("loginPassword").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("loginButton").click();
});

function collectNewAccountPayload() {
  const payload = {
    label: $("addLabel").value,
    gender: $("addGender").value,
    stage: $("addStage").value,
    style: $("addStyle").value,
    task_name: $("addTaskName").value,
    task_info: $("addTaskInfo").value,
    enabled: $("addEnabled").checked,
  };
  if ($("addBaseUrl").value.trim()) payload.ai_base_url = $("addBaseUrl").value;
  if ($("addModel").value.trim()) payload.ai_model = $("addModel").value;
  if ($("addApiKey").value) payload.ai_api_key = $("addApiKey").value;
  return payload;
}

function setTelegramAuthState(state, phoneHint = "") {
  telegramAuthState = state;
  const phoneMode = $("addLoginMode").value === "phone";
  const codeRequired = phoneMode && state === "code_required";
  const passwordRequired = phoneMode && state === "password_required";
  $("addPhone").disabled = !phoneMode || state !== "idle";
  $("addPhone").required = phoneMode && state === "idle";
  $("addCodePanel").classList.toggle("hidden", !codeRequired);
  $("addCode").disabled = !codeRequired;
  $("addCode").required = codeRequired;
  $("addPasswordPanel").classList.toggle("hidden", !passwordRequired);
  $("addTwoFactorPassword").disabled = !passwordRequired;
  $("addTwoFactorPassword").required = passwordRequired;
  if (phoneHint) $("addPhoneHint").textContent = `驗證碼已傳送至 ${phoneHint}`;
  else if (state === "idle") {
    $("addPhoneHint").textContent = "請包含國碼；驗證碼會由 Telegram 官方帳號或 App 傳送。";
  }
  $("addSubmitButton").textContent = {
    idle: "傳送 Telegram 驗證碼",
    code_required: "驗證並自動新增帳號",
    password_required: "完成兩步驗證並新增帳號",
    authorized: "儲存並啟動帳號",
  }[state] || "繼續";
}

function updateAddLoginMode() {
  const phoneMode = $("addLoginMode").value === "phone";
  $("addPhonePanel").classList.toggle("hidden", !phoneMode);
  $("addSessionPanel").classList.toggle("hidden", phoneMode);
  $("addSession").disabled = phoneMode;
  $("addSession").required = !phoneMode;
  if (phoneMode) setTelegramAuthState(telegramAuthState);
  else {
    $("addPhone").disabled = true;
    $("addPhone").required = false;
    $("addCodePanel").classList.add("hidden");
    $("addCode").disabled = true;
    $("addCode").required = false;
    $("addPasswordPanel").classList.add("hidden");
    $("addTwoFactorPassword").disabled = true;
    $("addTwoFactorPassword").required = false;
    $("addSubmitButton").textContent = "驗證並新增帳號";
  }
}

async function cancelTelegramAuth() {
  const authId = telegramAuthId;
  if (authId) {
    try {
      await api("/api/telegram-auth/cancel", {
        method: "POST",
        body: JSON.stringify({auth_id: authId}),
      });
    } catch (error) {
      if (error.status !== 410) {
        setNotice("addNotice", "暫時無法取消登入流程，請稍後再試。", "error");
        return false;
      }
    }
  }
  telegramAuthId = "";
  telegramAuthState = "idle";
  $("addPhone").value = "";
  $("addCode").value = "";
  $("addTwoFactorPassword").value = "";
  updateAddLoginMode();
  return true;
}

function resetAddForm() {
  telegramAuthId = "";
  telegramAuthState = "idle";
  $("addForm").reset();
  $("addTaskName").value = "一般群聊互動";
  $("addEnabled").checked = true;
  $("addLoginMode").value = "phone";
  updateAddLoginMode();
}

async function finishNewAccount(payload) {
  const created = await api("/api/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  selectedAccountId = created.id;
  resetAddForm();
  $("addPanel").classList.add("hidden");
  setNotice("addNotice", "");
  await refresh();
  setNotice("accountNotice", "帳號已登入、加密保存並啟動", "success");
}

$("showAddButton").addEventListener("click", () => {
  $("addPanel").classList.remove("hidden");
  updateAddLoginMode();
  if ($("addLoginMode").value === "phone") $("addPhone").focus();
  else $("addSession").focus();
});

$("cancelAddButton").addEventListener("click", async () => {
  if (!(await cancelTelegramAuth())) return;
  $("addPanel").classList.add("hidden");
  setNotice("addNotice", "");
});

$("addLoginMode").addEventListener("change", async () => {
  if (!(await cancelTelegramAuth())) {
    $("addLoginMode").value = "phone";
  }
  updateAddLoginMode();
});

$("restartPhoneLoginButton").addEventListener("click", async () => {
  if (!(await cancelTelegramAuth())) return;
  setNotice(
    "addNotice",
    "登入流程已取消；Telegram 可能要求等待 60 秒後才能重新傳送驗證碼。",
    "warning",
  );
  $("addPhone").focus();
});

$("addForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runButton($("addSubmitButton"), async () => {
    const payload = collectNewAccountPayload();
    try {
      if ($("addLoginMode").value === "session") {
        setNotice("addNotice", "正在驗證 Telegram Session…", "warning");
        payload.session_string = $("addSession").value;
        await finishNewAccount(payload);
        return;
      }

      if (telegramAuthState === "idle") {
        setNotice("addNotice", "正在請 Telegram 傳送驗證碼…", "warning");
        const result = await api("/api/telegram-auth/start", {
          method: "POST",
          body: JSON.stringify({phone: $("addPhone").value}),
        });
        telegramAuthId = result.auth_id;
        setTelegramAuthState(result.status, result.phone_hint);
        setNotice("addNotice", "驗證碼已傳送，請在 10 分鐘內完成登入。", "success");
        $("addCode").focus();
        return;
      }

      if (telegramAuthState === "code_required") {
        setNotice("addNotice", "正在驗證 Telegram 驗證碼…", "warning");
        const code = $("addCode").value;
        $("addCode").value = "";
        const result = await api("/api/telegram-auth/code", {
          method: "POST",
          body: JSON.stringify({auth_id: telegramAuthId, code}),
        });
        setTelegramAuthState(result.status, result.phone_hint);
        if (result.status === "password_required") {
          setNotice("addNotice", "此帳號已啟用兩步驗證，請輸入 Telegram 密碼。", "warning");
          $("addTwoFactorPassword").focus();
          return;
        }
      } else if (telegramAuthState === "password_required") {
        setNotice("addNotice", "正在完成 Telegram 兩步驗證…", "warning");
        const password = $("addTwoFactorPassword").value;
        $("addTwoFactorPassword").value = "";
        const result = await api("/api/telegram-auth/password", {
          method: "POST",
          body: JSON.stringify({auth_id: telegramAuthId, password}),
        });
        setTelegramAuthState(result.status, result.phone_hint);
      }

      if (telegramAuthState === "authorized") {
        setNotice("addNotice", "登入成功，正在加密保存並啟動帳號…", "warning");
        payload.telegram_auth_id = telegramAuthId;
        await finishNewAccount(payload);
      }
    } catch (error) {
      if (error.status === 410) await cancelTelegramAuth();
      setNotice("addNotice", error.message, "error");
    }
  });
});

updateAddLoginMode();

$("settingsForm").addEventListener("input", () => {
  formDirty = true;
});

$("editApiKey").addEventListener("input", () => {
  if ($("editApiKey").value) $("clearApiKey").checked = false;
});

$("clearApiKey").addEventListener("change", () => {
  if ($("clearApiKey").checked) $("editApiKey").value = "";
});

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const account = selectedAccount();
  if (!account) return;
  setNotice("settingsNotice", "正在儲存…", "warning");
  await runButton($("saveSettingsButton"), async () => {
    const payload = {
      revision: account.revision,
      label: $("editLabel").value,
      gender: $("editGender").value,
      stage: $("editStage").value,
      style: $("editStyle").value,
      task_name: $("editTaskName").value,
      task_info: $("editTaskInfo").value,
      ai_base_url: $("editBaseUrl").value,
      ai_model: $("editModel").value,
      ai_api_key: $("editApiKey").value,
      clear_ai_api_key: $("clearApiKey").checked,
      group_reply_probability: numberValue("editProbability"),
      reply_on_mention: $("editReplyMention").checked,
      reply_on_reply: $("editReplyReply").checked,
      typing_delay_min_seconds: numberValue("editDelayMin"),
      typing_delay_max_seconds: numberValue("editDelayMax"),
      proactive_enabled: $("editProactive").checked,
      proactive_idle_minutes: integerValue("editIdle"),
      proactive_min_interval_minutes: integerValue("editIntervalMin"),
      proactive_max_interval_minutes: integerValue("editIntervalMax"),
      max_proactive_per_day: integerValue("editProactiveMax"),
    };
    try {
      await api(`/api/accounts/${encodeURIComponent(account.id)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      formDirty = false;
      await refresh();
      setNotice("settingsNotice", "設定已儲存；帳號已套用最新設定", "success");
    } catch (error) {
      setNotice("settingsNotice", error.message, "error");
    }
  });
});

$("controlButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  await runButton($("controlButton"), async () => {
    try {
      await api(`/api/accounts/${encodeURIComponent(account.id)}/control`, {
        method: "POST",
        body: JSON.stringify({enabled: !account.enabled, revision: account.revision}),
      });
      formDirty = false;
      await refresh();
      setNotice("accountNotice", account.enabled ? "帳號已停用" : "帳號已啟用", "success");
    } catch (error) {
      setNotice("accountNotice", error.message, "error");
    }
  });
});

$("restartButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  setNotice("accountNotice", "正在重新啟動帳號…", "warning");
  await runButton($("restartButton"), async () => {
    try {
      await api(`/api/accounts/${encodeURIComponent(account.id)}/restart`, {
        method: "POST",
        body: "{}",
      });
      await refresh();
      setNotice("accountNotice", "帳號已重新啟動", "success");
    } catch (error) {
      setNotice("accountNotice", error.message, "error");
    }
  });
});

$("modelTestButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  setNotice("accountNotice", "正在測試模型連線…", "warning");
  await runButton($("modelTestButton"), async () => {
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(account.id)}/model/test`, {
        method: "POST",
        body: "{}",
      });
      if (result.ok) {
        setNotice("accountNotice", `模型連線正常，延遲 ${result.latency_ms || 0} ms`, "success");
      } else {
        setNotice("accountNotice", result.error || "模型測試失敗", "error");
      }
    } catch (error) {
      setNotice("accountNotice", error.message, "error");
    }
  });
});

$("allGroups").addEventListener("change", () => {
  groupsDirty = true;
  for (const input of $("groupList").querySelectorAll("input[type='checkbox']")) {
    input.disabled = $("allGroups").checked;
    if ($("allGroups").checked) input.checked = true;
  }
});

$("groupList").addEventListener("change", () => {
  groupsDirty = true;
});

$("saveGroupsButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  const allGroups = $("allGroups").checked;
  const groupIds = [...$("groupList").querySelectorAll("input[type='checkbox']:checked")]
    .map((input) => Number(input.value));
  if (!allGroups && groupIds.length === 0 &&
      !window.confirm("沒有選擇任何群組，儲存後此帳號不會在任何群組互動。確定繼續？")) return;
  await runButton($("saveGroupsButton"), async () => {
    try {
      await api(`/api/accounts/${encodeURIComponent(account.id)}/groups`, {
        method: "POST",
        body: JSON.stringify({
          all_groups: allGroups,
          group_ids: groupIds,
          revision: account.revision,
        }),
      });
      groupsDirty = false;
      await refresh();
      setNotice("groupsNotice", "群組範圍已儲存", "success");
    } catch (error) {
      setNotice("groupsNotice", error.message, "error");
    }
  });
});

$("clearMemoryButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account || !window.confirm("確定清除這個帳號的全部聊天記憶？此操作無法復原。")) return;
  await runButton($("clearMemoryButton"), async () => {
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(account.id)}/memory/clear`, {
        method: "POST",
        body: "{}",
      });
      await refresh();
      setNotice("accountNotice", `已清除 ${result.removed} 筆記憶`, "success");
    } catch (error) {
      setNotice("accountNotice", error.message, "error");
    }
  });
});

$("refreshButton").addEventListener("click", async () => {
  formDirty = false;
  groupsDirty = false;
  try {
    await refresh();
  } catch (error) {
    if (error.message !== "unauthorized") setNotice("accountNotice", error.message, "error");
  }
});

$("logoutButton").addEventListener("click", async () => {
  try {
    await api("/api/logout", {method: "POST", body: "{}"});
  } finally {
    csrfToken = "";
    dashboardState = null;
    selectedAccountId = "";
    showLogin();
  }
});

refresh().catch((error) => {
  if (error.message !== "unauthorized") {
    showLogin();
    setNotice("loginNotice", "控制台暫時無法讀取，請稍後再試", "error");
  }
});

setInterval(() => {
  if (!document.hidden && !formDirty && !groupsDirty &&
      $("addPanel").classList.contains("hidden")) {
    refresh().catch(() => {});
  }
}, 20000);
"""


class DashboardServer:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        port: int,
        manager: AccountManager,
    ) -> None:
        self.username = username
        self.password = password
        self.port = port
        self.manager = manager
        self.sessions: dict[str, DashboardSession] = {}
        self.login_limiter = LoginRateLimiter()
        self.app = self._build_app()
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[None] | None = None

    @staticmethod
    def _client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate:
                return candidate[:64]
        if request.client is not None:
            return request.client.host[:64]
        return "unknown"

    def _prune_sessions(self) -> None:
        now = time.time()
        expired_owner_ids: list[str] = []
        for token, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.sessions.pop(token, None)
                expired_owner_ids.append(session.session_id)
        for owner_id in expired_owner_ids:
            asyncio.create_task(
                self.manager.cancel_phone_logins_for_owner(owner_id)
            )

    def _get_session(self, request: Request) -> DashboardSession | None:
        self._prune_sessions()
        token = request.cookies.get(COOKIE_NAME, "")
        if not token:
            return None
        return self.sessions.get(token)

    def _require_auth(
        self,
        request: Request,
    ) -> tuple[DashboardSession | None, JSONResponse | None]:
        session = self._get_session(request)
        if session is None:
            return None, JSONResponse({"detail": "unauthorized"}, status_code=401)
        return session, None

    def _require_action(
        self,
        request: Request,
    ) -> tuple[DashboardSession | None, JSONResponse | None]:
        session, blocked = self._require_auth(request)
        if blocked is not None or session is None:
            return None, blocked
        supplied = request.headers.get(CSRF_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
            return None, JSONResponse({"detail": "invalid csrf token"}, status_code=403)
        return session, None

    @staticmethod
    async def _read_payload(request: Request) -> dict[str, object]:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    raise ValueError("request body is too large")
            except ValueError as exc:
                if str(exc) == "request body is too large":
                    raise
        body = await request.body()
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid JSON request") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    @staticmethod
    def _revision(payload: dict[str, object]) -> int:
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError("revision must be an integer")
        return revision

    def _build_app(self) -> FastAPI:
        web = FastAPI(
            title="Telegram AI Multi-account Dashboard",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @web.middleware("http")
        async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
            response = await call_next(request)
            for name, value in SECURITY_HEADERS.items():
                response.headers[name] = value
            return response

        @web.exception_handler(AccountNotFoundError)
        async def account_not_found(
            request: Request,
            exc: AccountNotFoundError,
        ) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=404)

        @web.exception_handler(AccountConflictError)
        async def account_conflict(
            request: Request,
            exc: AccountConflictError,
        ) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=409)

        @web.exception_handler(TelegramLoginConflict)
        async def telegram_login_conflict(
            request: Request,
            exc: TelegramLoginConflict,
        ) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=409)

        @web.exception_handler(TelegramLoginExpired)
        async def telegram_login_expired(
            request: Request,
            exc: TelegramLoginExpired,
        ) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=410)

        @web.exception_handler(TelegramLoginRateLimit)
        async def telegram_login_rate_limit(
            request: Request,
            exc: TelegramLoginRateLimit,
        ) -> JSONResponse:
            return JSONResponse(
                {"detail": str(exc), "retry_after": exc.retry_after},
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )

        @web.exception_handler(TelegramLoginUnavailable)
        async def telegram_login_unavailable(
            request: Request,
            exc: TelegramLoginUnavailable,
        ) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=503)

        @web.exception_handler(ValueError)
        async def invalid_value(
            request: Request,
            exc: ValueError,
        ) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=400)

        @web.get("/", response_class=HTMLResponse)
        async def index() -> HTMLResponse:
            return HTMLResponse(DASHBOARD_HTML)

        @web.get("/dashboard.js")
        async def dashboard_js() -> Response:
            return Response(DASHBOARD_JS, media_type="application/javascript")

        @web.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @web.post("/api/login")
        async def login(request: Request) -> JSONResponse:
            client_ip = self._client_ip(request)
            retry_after = self.login_limiter.retry_after(client_ip)
            if retry_after:
                return JSONResponse(
                    {"detail": "too many login attempts"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
            payload = await self._read_payload(request)
            username = str(payload.get("username", ""))
            password = str(payload.get("password", ""))
            valid_username = secrets.compare_digest(username, self.username)
            valid_password = secrets.compare_digest(password, self.password)
            if not (valid_username and valid_password):
                self.login_limiter.add_failure(client_ip)
                await asyncio.sleep(0.25)
                return JSONResponse({"detail": "invalid credentials"}, status_code=401)

            self.login_limiter.clear(client_ip)
            session_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            self.sessions[session_token] = DashboardSession(
                session_id=secrets.token_urlsafe(24),
                csrf_token=csrf_token,
                expires_at=time.time() + SESSION_TTL_SECONDS,
            )
            response = JSONResponse({"ok": True, "csrf_token": csrf_token})
            response.set_cookie(
                COOKIE_NAME,
                session_token,
                httponly=True,
                secure=True,
                samesite="strict",
                max_age=SESSION_TTL_SECONDS,
                path="/",
            )
            return response

        @web.post("/api/logout")
        async def logout(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            await self.manager.cancel_phone_logins_for_owner(session.session_id)
            token = request.cookies.get(COOKIE_NAME, "")
            self.sessions.pop(token, None)
            response = JSONResponse({"ok": True})
            response.delete_cookie(
                COOKIE_NAME,
                path="/",
                secure=True,
                httponly=True,
                samesite="strict",
            )
            return response

        @web.get("/api/status")
        async def status(request: Request) -> JSONResponse:
            session, blocked = self._require_auth(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            return JSONResponse(
                await self.manager.status(),
                headers={"X-CSRF-Token": session.csrf_token},
            )

        @web.post("/api/accounts")
        async def create_account(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked
            payload = await self._read_payload(request)
            return JSONResponse(
                await self.manager.create_account(payload, session.session_id),
                status_code=201,
            )

        @web.post("/api/telegram-auth/start")
        async def telegram_auth_start(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            payload = await self._read_payload(request)
            return JSONResponse(
                await self.manager.start_phone_login(
                    session.session_id,
                    payload.get("phone"),
                ),
                status_code=201,
            )

        @web.post("/api/telegram-auth/code")
        async def telegram_auth_code(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            payload = await self._read_payload(request)
            return JSONResponse(
                await self.manager.submit_phone_code(
                    session.session_id,
                    payload.get("auth_id"),
                    payload.get("code"),
                )
            )

        @web.post("/api/telegram-auth/password")
        async def telegram_auth_password(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            payload = await self._read_payload(request)
            return JSONResponse(
                await self.manager.submit_phone_password(
                    session.session_id,
                    payload.get("auth_id"),
                    payload.get("password"),
                )
            )

        @web.post("/api/telegram-auth/cancel")
        async def telegram_auth_cancel(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            payload = await self._read_payload(request)
            await self.manager.cancel_phone_login(
                session.session_id,
                payload.get("auth_id"),
            )
            return JSONResponse({"ok": True})

        @web.put("/api/accounts/{account_id}")
        async def update_account(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            payload = await self._read_payload(request)
            return JSONResponse(await self.manager.update_account(account_id, payload))

        @web.post("/api/accounts/{account_id}/control")
        async def control(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            payload = await self._read_payload(request)
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be boolean")
            revision = self._revision(payload)
            return JSONResponse(
                await self.manager.set_enabled(account_id, enabled, revision)
            )

        @web.post("/api/accounts/{account_id}/groups")
        async def groups(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            payload = await self._read_payload(request)
            all_groups = payload.get("all_groups")
            group_ids = payload.get("group_ids")
            if not isinstance(all_groups, bool) or not isinstance(group_ids, list):
                raise ValueError("invalid group selection")
            revision = self._revision(payload)
            return JSONResponse(
                await self.manager.set_groups(
                    account_id,
                    all_groups,
                    group_ids,
                    revision,
                )
            )

        @web.post("/api/accounts/{account_id}/restart")
        async def restart(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            return JSONResponse(await self.manager.restart_account(account_id))

        @web.post("/api/accounts/{account_id}/model/test")
        async def model_test(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            return JSONResponse(await self.manager.test_model(account_id))

        @web.post("/api/accounts/{account_id}/memory/clear")
        async def clear_memory(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            removed = await self.manager.clear_memory(account_id)
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
        self.sessions.clear()
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=10)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        LOGGER.info("Dashboard stopped")
