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
      grid-template-columns: repeat(5, minmax(100px, 1fr));
      gap: 10px;
      width: min(760px, 100%);
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
    .account-list { display: grid; gap: 9px; }
    .account-card {
      min-width: 0;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 13px;
      background: #151711;
    }
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
    .manual-send-row {
      display: grid;
      grid-template-columns: minmax(0, .85fr) minmax(0, 1.4fr) 38px;
      gap: 6px;
      align-items: center;
      padding: 4px;
    }
    .manual-send-row select, .manual-send-row textarea {
      width: 100%;
      min-width: 0;
      height: 38px;
      padding: 8px 9px;
      color: #fff;
      border: 1px solid var(--line);
      border-radius: 9px;
      outline: none;
      background: #0f110d;
    }
    .manual-send-row textarea {
      min-height: 38px;
      max-height: 38px;
      resize: none;
      line-height: 1.35;
      overflow-y: auto;
      overscroll-behavior: contain;
    }
    .manual-send-row select:focus, .manual-send-row textarea:focus {
      border-color: var(--accent);
    }
    .manual-send-button {
      width: 38px;
      min-height: 38px;
      padding: 0;
      border-radius: 50%;
      font-size: 18px;
      line-height: 1;
    }
    .manual-send-notice {
      min-height: 17px;
      padding: 0 5px 3px;
      color: var(--muted);
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    .manual-send-notice.error { color: var(--danger); }
    .manual-send-notice.success { color: var(--accent); }
    .account-item strong, .account-item small {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .account-item small { margin-top: 3px; color: var(--muted); }
    .account-title-row {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 7px;
    }
    .account-title-row strong { min-width: 0; flex: 1; }
    .private-alert-badge {
      flex: 0 0 auto;
      min-width: 21px;
      padding: 2px 6px;
      color: #17200d;
      border-radius: 999px;
      background: var(--accent);
      font-size: 11px;
      font-weight: 850;
      line-height: 1.45;
      text-align: center;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #666d5d;
    }
    .dot.online { background: var(--accent); box-shadow: 0 0 14px rgba(184, 244, 109, .65); }
    .dot.error { background: var(--danger); }
    .content { min-width: 0; }
    .panel { padding: 14px; margin-bottom: 10px; }
    .collapsible-panel { padding-top: 0; padding-bottom: 0; }
    .collapse-toggle {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 7px 2px;
      color: inherit;
      border: 0;
      background: transparent;
      text-align: left;
      font-size: 16px;
      font-weight: 800;
    }
    .collapse-toggle-copy {
      min-width: 0;
      display: grid;
      gap: 3px;
    }
    .account-compact-summary {
      max-width: min(72vw, 820px);
      overflow: hidden;
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      line-height: 1.35;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .collapse-toggle-icon {
      color: var(--muted);
      font-size: 18px;
      transition: transform .16s ease;
    }
    .collapse-toggle[aria-expanded="true"] .collapse-toggle-icon {
      transform: rotate(180deg);
    }
    .collapse-toggle[aria-expanded="true"] .account-compact-summary { display: none; }
    .collapsible-content[hidden] { display: none; }
    .account-section {
      padding: 14px 2px;
      border-top: 1px solid var(--line);
    }
    .account-section:first-child {
      padding-top: 4px;
      border-top: 0;
    }
    .account-section:last-child { padding-bottom: 10px; }
    .account-section > h3 { margin-bottom: 7px; }
    .account-section.danger-zone { border-top-color: #553b35; }
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
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 7px;
      margin-top: 12px;
    }
    .metric { padding: 9px; border: 1px solid var(--line); border-radius: 12px; background: #151711; }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong { display: block; margin-top: 4px; font-size: 17px; overflow-wrap: anywhere; }
    .section-title { margin-bottom: 12px; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 9px 10px;
    }
    .field { grid-column: span 6; display: grid; gap: 6px; color: var(--muted); font-size: 13px; }
    .field.third { grid-column: span 4; }
    .field.quarter { grid-column: span 3; }
    .field.full { grid-column: 1 / -1; }
    .field input, .field select, .field textarea {
      width: 100%;
      padding: 9px 10px;
      color: #fff;
      border: 1px solid var(--line);
      border-radius: 10px;
      outline: none;
      background: #11130f;
    }
    .field textarea { min-height: 64px; resize: vertical; line-height: 1.4; }
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
    .divider { height: 1px; margin: 14px 0; background: var(--line); }
    .group-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 9px 0;
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
    .conversation-toolbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 130px auto;
      gap: 9px;
      align-items: end;
      margin: 9px 0;
    }
    .conversation-list {
      display: grid;
      gap: 9px;
      max-height: 620px;
      overflow-y: auto;
      padding-right: 4px;
    }
    .conversation-message {
      padding: 12px 13px;
      border: 1px solid var(--line);
      border-radius: 13px;
      background: #151711;
    }
    .conversation-message.assistant { border-color: #50633a; background: #192014; }
    .conversation-message-head {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 7px;
      color: var(--muted);
      font-size: 12px;
    }
    .conversation-message-body {
      margin: 0;
      line-height: 1.6;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .private-alert-toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin: 9px 0;
    }
    .private-alert-list {
      display: grid;
      gap: 8px;
      max-height: 430px;
      overflow-y: auto;
      padding-right: 4px;
    }
    .private-alert {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px 10px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #151711;
    }
    .private-alert.unread {
      border-color: #647d43;
      background: #192014;
    }
    .private-alert-head {
      min-width: 0;
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 5px 8px;
    }
    .private-alert-head strong,
    .private-alert-head small {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .private-alert-head small,
    .private-alert time { color: var(--muted); font-size: 12px; }
    .private-alert-preview {
      grid-column: 1 / -1;
      margin: 0;
      line-height: 1.5;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .private-alert .btn {
      grid-column: 2;
      justify-self: end;
    }
    .provider-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 9px;
    }
    .provider-state {
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 11px;
      background: #151711;
    }
    .provider-state span { display: block; color: var(--muted); font-size: 12px; }
    .provider-state strong { display: block; margin-top: 4px; }
    .provider-state.ready strong { color: var(--accent); }
    .media-job-list {
      display: grid;
      gap: 9px;
      margin-top: 12px;
    }
    .media-job {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 7px 12px;
      padding: 12px 13px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #151711;
    }
    .media-job small { color: var(--muted); overflow-wrap: anywhere; }
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
      .manual-send-row { grid-template-columns: minmax(0, 1fr) 38px; }
      .manual-send-row select { grid-column: 1 / -1; }
      .account-head, .row-between { flex-direction: column; align-items: stretch; }
      .field, .field.third, .field.quarter { grid-column: 1 / -1; }
      .group-list { grid-template-columns: 1fr; }
      .conversation-toolbar { grid-template-columns: 1fr; }
      .provider-grid { grid-template-columns: 1fr; }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .status-line { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 440px) {
      .account-list { grid-template-columns: 1fr; }
      .manual-send-row { grid-template-columns: minmax(0, .9fr) minmax(0, 1.3fr) 38px; }
      .manual-send-row select { grid-column: auto; }
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
        <div class="summary-item"><span>私聊未讀</span><strong id="summaryPrivateUnread">0</strong></div>
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
            <label class="field full">任務名稱
              <input id="addTaskName" maxlength="120" value="一般群聊互動">
            </label>
            <label class="field full">任務資訊
              <textarea id="addTaskInfo" maxlength="3000" placeholder="說明這個帳號應關注的話題與互動目標"></textarea>
            </label>
            <label class="field full">輸出屏蔽詞／詞組
              <textarea id="addBlockedTerms" maxlength="4000" spellcheck="false" placeholder="每行一個，也可用半形或全形逗號分隔&#10;例如：代購、博彩&#10;私下匯款"></textarea>
              <span class="hint">AI 草稿出現任一詞組、插入符號的變形或近似寫法時，不會發送。最多 100 項，每項最多 80 字。</span>
            </label>
            <label class="field full">輸出屏蔽主題
              <textarea id="addBlockedTopics" maxlength="8000" placeholder="每行一個完整主題&#10;例如：政治宣傳&#10;借貸與投資招攬"></textarea>
              <span class="hint">禁止輸出相關解釋、改寫或暗示。每行一項，最多 50 項、每項最多 300 字；語意審核會增加模型呼叫，審核失敗時不發送。</span>
            </label>
            <label class="field">AI Base URL（可留白使用系統預設）
              <input id="addBaseUrl" inputmode="url" maxlength="500" placeholder="https://openrouter.ai/api/v1">
            </label>
            <label class="field">模型（可留白使用系統預設）
              <input id="addModel" maxlength="200" placeholder="模型名稱">
            </label>
            <label class="check field full"><input id="addAdultTextEnabled" type="checkbox"> 啟用成人純文字模式</label>
            <div class="hint field full">勾選即確認此帳號的允許群組為 18+；群內成人文字聊天預設為成年、自願，不必每句重複確認。明確未成年、拒絕、強迫、剝削、偷拍、私密資料濫用與非法內容仍固定攔截；不套用到圖片、語音或影片。</div>
            <div class="hint field full">AI 與媒體 Provider API Key 僅由 Railway Variables 提供；控制台不接受、保存或顯示任何 Key。</div>
            <label class="check field full"><input id="addEnabled" type="checkbox"> 建立後立即啟用</label>
            <div class="hint field full">帳號建立後先手動啟用以載入群組；尚未選擇群組時只會連線，不會自動回覆。選好允許群組後即可開始。</div>
            <div class="actions field full">
              <button id="addSubmitButton" class="btn primary" type="submit">傳送 Telegram 驗證碼</button>
            </div>
          </form>
          <div id="addNotice" class="notice"></div>
        </article>

        <article id="emptyPanel" class="panel empty">請從左側選擇帳號，或新增第一個帳號。</article>

        <div id="accountPanels" class="hidden">
          <article class="panel collapsible-panel account-overview-accordion" data-collapsible data-panel-title="帳號概覽" data-expanded="false">
            <section class="account-section account-section-primary">
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
              <div class="metric"><span>私聊未讀</span><strong id="metricPrivateUnread">0</strong></div>
              <div class="metric"><span>政策攔截</span><strong id="metricBlocked">0</strong></div>
              <div class="metric"><span>錯誤</span><strong id="metricErrors">0</strong></div>
            </div>
            <div id="accountNotice" class="notice"></div>
            </section>

            <section class="account-section">
            <div class="row-between">
              <div>
                <h3>私聊提醒</h3>
                <div class="hint">顯示最近 24 小時收到的私聊提示；AI 不會在私聊自動回覆，也不會把私聊帶入群聊記憶。標記已處理只會清除控制台未讀提示，不會向 Telegram 傳送已讀回條。</div>
              </div>
              <div class="private-alert-toolbar">
                <button id="refreshPrivateAlertsButton" class="btn small" type="button">重新整理</button>
                <button id="ackAllPrivateAlertsButton" class="btn small" type="button">全部標記已處理</button>
              </div>
            </div>
            <div id="privateAlertsNotice" class="notice"></div>
            <div id="privateAlertList" class="private-alert-list"></div>
            </section>

            <section class="account-section">
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
              <label class="field full">任務名稱
                <input id="editTaskName" required maxlength="120">
              </label>
              <label class="field full">任務資訊
                <textarea id="editTaskInfo" maxlength="3000"></textarea>
              </label>
              <label class="field full">輸出屏蔽詞／詞組
                <textarea id="editBlockedTerms" maxlength="4000" spellcheck="false" placeholder="每行一個，也可用半形或全形逗號分隔"></textarea>
                <span class="hint">AI 草稿出現任一詞組、插入符號的變形或近似寫法時，不會發送。最多 100 項，每項最多 80 字。</span>
              </label>
              <label class="field full">輸出屏蔽主題
                <textarea id="editBlockedTopics" maxlength="8000" placeholder="每行一個完整主題"></textarea>
                <span class="hint">禁止輸出相關解釋、改寫或暗示。每行一項，最多 50 項、每項最多 300 字；語意審核會增加模型呼叫，審核失敗時不發送。</span>
              </label>
              <label class="field">AI Base URL
                <input id="editBaseUrl" required maxlength="500" inputmode="url">
              </label>
              <label class="field">模型名稱
                <input id="editModel" required maxlength="200">
              </label>
              <label class="check field full"><input id="editAdultTextEnabled" type="checkbox"> 啟用成人純文字模式</label>
              <div class="hint field full">勾選即確認允許群組為 18+；群內成人文字聊天預設為成年、自願，明確拒絕或其他相反證據會立即覆蓋此預設。媒體安全政策不會因此放寬。</div>
              <div class="hint field full">AI 與媒體 Provider API Key 僅由 Railway Variables 提供；控制台不接受、保存或顯示任何 Key。</div>

              <div class="field full"><div class="divider"></div><h3>媒體生成</h3></div>
              <div class="provider-grid field full" aria-label="媒體 Provider 狀態">
                <div id="imageProviderState" class="provider-state"><span>OpenRouter 圖片</span><strong>未就緒</strong></div>
                <div id="voiceProviderState" class="provider-state"><span>OpenRouter 語音</span><strong>未就緒</strong></div>
                <div id="videoProviderState" class="provider-state"><span>OpenRouter 影片</span><strong>未就緒</strong></div>
              </div>
              <div class="hint field full">媒體允許群組與帳號的回覆群組分開管理。啟用每種媒體時都要明確選擇至少一個群組；若帳號目前只有一個可回覆群組，控制台會自動預選。</div>

              <label class="check field full"><input id="editImageEnabled" type="checkbox"> 啟用圖片生成</label>
              <label class="field">圖片模型
                <input id="editImageModel" maxlength="200" autocomplete="off">
              </label>
              <label class="field quarter">圖片每日上限
                <input id="editImageDailyLimit" type="number" min="0" max="1000" step="1" required>
              </label>
              <label class="field quarter">圖片冷卻時間（秒）
                <input id="editImageCooldown" type="number" min="0" max="604800" step="1" required>
              </label>
              <label class="field full">圖片允許群組 ID
                <textarea id="editImageGroupIds" maxlength="12000" spellcheck="false" placeholder="-1001234567890, -1009876543210"></textarea>
                <span class="hint">可用逗號、空白或換行輸入；也可在下方從此帳號已知群組複選。</span>
              </label>
              <label class="field full">從已知群組複用到圖片
                <select id="editImageKnownGroups" multiple size="4"></select>
              </label>

              <label class="check field full"><input id="editVoiceEnabled" type="checkbox"> 啟用語音生成</label>
              <label class="field">語音模型
                <input id="editVoiceModel" maxlength="200" autocomplete="off">
              </label>
              <label class="field">語音聲線
                <input id="editVoiceName" maxlength="120" autocomplete="off">
              </label>
              <label class="field">語音每日上限
                <input id="editVoiceDailyLimit" type="number" min="0" max="1000" step="1" required>
              </label>
              <label class="field">語音冷卻時間（秒）
                <input id="editVoiceCooldown" type="number" min="0" max="604800" step="1" required>
              </label>
              <label class="field full">語音允許群組 ID
                <textarea id="editVoiceGroupIds" maxlength="12000" spellcheck="false" placeholder="-1001234567890"></textarea>
              </label>
              <label class="field full">從已知群組複用到語音
                <select id="editVoiceKnownGroups" multiple size="4"></select>
              </label>

              <label class="check field full"><input id="editVideoEnabled" type="checkbox"> 啟用影片生成</label>
              <label class="field">影片模型
                <input id="editVideoModel" maxlength="200" autocomplete="off">
              </label>
              <label class="field quarter">影片每日上限
                <input id="editVideoDailyLimit" type="number" min="0" max="1000" step="1" required>
              </label>
              <label class="field quarter">影片冷卻時間（秒）
                <input id="editVideoCooldown" type="number" min="0" max="604800" step="1" required>
              </label>
              <label class="field full">影片允許群組 ID
                <textarea id="editVideoGroupIds" maxlength="12000" spellcheck="false" placeholder="-1001234567890"></textarea>
              </label>
              <label class="field full">從已知群組複用到影片
                <select id="editVideoKnownGroups" multiple size="4"></select>
              </label>

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
              <label class="field">最長主動間隔（分鐘）
                <input id="editIntervalMax" type="number" min="1" max="1440" step="1" required>
              </label>
              <label class="field">每日主動發言上限
                <input id="editProactiveMax" type="number" min="0" max="200" step="1" required>
              </label>
              <div class="actions field full">
                <button id="saveSettingsButton" class="btn primary" type="submit">儲存帳號設定</button>
              </div>
            </form>
            <div id="settingsNotice" class="notice"></div>
            </section>

            <section class="account-section">
            <div class="row-between">
              <div>
                <h3>媒體任務狀態</h3>
                <div class="hint">只顯示任務類型、狀態、群組與時間；不回傳 Prompt、結果內容或 Provider Key。</div>
              </div>
              <div class="actions">
                <label class="field">顯示筆數
                  <select id="mediaJobsLimit">
                    <option value="20" selected>最近 20 筆</option>
                    <option value="50">最近 50 筆</option>
                    <option value="100">最近 100 筆</option>
                  </select>
                </label>
                <button id="refreshMediaJobsButton" class="btn" type="button">重新整理任務</button>
              </div>
            </div>
            <div id="mediaJobsNotice" class="notice"></div>
            <div id="mediaJobList" class="media-job-list"></div>
            </section>

            <section class="account-section">
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
            </section>

            <section class="account-section">
            <div>
              <h3>聊天記錄</h3>
              <div class="hint">顯示此帳號 24 小時記憶中的群聊內容；不顯示 Telegram sender ID。</div>
            </div>
            <div class="conversation-toolbar">
              <label class="field full">群組
                <select id="conversationGroup"></select>
              </label>
              <label class="field full">顯示則數
                <select id="conversationLimit">
                  <option value="20">最近 20 則</option>
                  <option value="50">最近 50 則</option>
                  <option value="100" selected>最近 100 則</option>
                </select>
              </label>
              <button id="refreshConversationButton" class="btn" type="button">重新整理記錄</button>
            </div>
            <div id="conversationNotice" class="notice"></div>
            <div id="conversationList" class="conversation-list"></div>
            </section>

            <section class="account-section danger-zone">
            <h3>記憶管理</h3>
            <div class="hint">清除聊天記憶不會刪除帳號、Telegram Session 或模型設定。此操作無法復原。</div>
            <div class="actions">
              <button id="clearMemoryButton" class="btn danger">清除這個帳號的聊天記憶</button>
            </div>
            </section>
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
const conversationGroupByAccount = new Map();
const manualGroupByAccount = new Map();
const manualMessageDraftByAccount = new Map();
const manualMessagePendingAccounts = new Set();
let conversationLoadedKey = "";
let conversationSelectionKey = "";
let conversationRequestSequence = 0;
let conversationPendingKey = "";
let conversationPendingSequence = 0;
let mediaJobsRequestSequence = 0;
let mediaJobsLoadedAccountId = "";
let privateAlertsRequestSequence = 0;
let privateAlertsLoadedAccountId = "";
let privateAlertsLoadedUnreadCount = -1;
let privateAlertsLoadedLatestAt = 0;
let privateIndicatorRefreshPending = false;

function showLogin() {
  document.title = "Telegram AI 多帳號控制台";
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
}

function showApp() {
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
}

function resetDashboardClientState() {
  dashboardState = null;
  selectedAccountId = "";
  formDirty = false;
  groupsDirty = false;
  manualGroupByAccount.clear();
  manualMessageDraftByAccount.clear();
  manualMessagePendingAccounts.clear();
  conversationRequestSequence += 1;
  conversationLoadedKey = "";
  conversationSelectionKey = "";
  mediaJobsRequestSequence += 1;
  mediaJobsLoadedAccountId = "";
  privateAlertsRequestSequence += 1;
  privateAlertsLoadedAccountId = "";
  privateAlertsLoadedUnreadCount = -1;
  privateAlertsLoadedLatestAt = 0;
  clearConversationList();
  clearMediaJobs();
  clearPrivateAlerts();
  document.title = "Telegram AI 多帳號控制台";
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
    resetDashboardClientState();
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

function compactAccountSummary(account) {
  const groupCount = Array.isArray(account.joined_groups) ? account.joined_groups.length : 0;
  const connectionState = account.connected ? "已連線" : "未連線";
  const privateUnread = Math.max(0, Number(account.private_unread_count || 0));
  const adultTextMode = account.adult_text_enabled ? "成人純文字開" : "成人純文字關";
  return `${account.label || "未命名帳號"} · ${stateName(account)}／${connectionState} · ` +
    `${roleName(account)} · ${account.ai_model || "未設定模型"} · ${adultTextMode} · ${groupCount} 個群組 · ` +
    `私聊未讀 ${privateUnread}`;
}

function activeManualMessageEditor() {
  const active = document.activeElement;
  return active?.classList?.contains("manual-message-input") ? active : null;
}

function dashboardEditorHasFocus() {
  const active = document.activeElement;
  return Boolean(
    active?.matches?.("input, textarea, select") &&
    active.closest("#app")
  );
}

function setupCollapsiblePanels() {
  let index = 0;
  for (const panel of document.querySelectorAll("[data-collapsible]")) {
    if (panel.querySelector(":scope > .collapse-toggle")) continue;
    index += 1;
    const content = document.createElement("div");
    content.className = "collapsible-content";
    content.id = `collapsiblePanel${index}`;
    while (panel.firstChild) content.appendChild(panel.firstChild);
    const duplicateHeading = content.querySelector("h3");
    if (duplicateHeading?.textContent?.trim() === panel.dataset.panelTitle) {
      duplicateHeading.classList.add("hidden");
    }

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "collapse-toggle";
    toggle.setAttribute("aria-controls", content.id);
    const expanded = panel.dataset.expanded === "true";
    toggle.setAttribute("aria-expanded", String(expanded));
    content.hidden = !expanded;

    const copy = document.createElement("span");
    copy.className = "collapse-toggle-copy";
    const label = document.createElement("span");
    label.textContent = panel.dataset.panelTitle || "內容";
    const summary = document.createElement("span");
    summary.id = "selectedCompactSummary";
    summary.className = "account-compact-summary";
    summary.textContent = "請選擇帳號";
    copy.append(label, summary);
    const icon = document.createElement("span");
    icon.className = "collapse-toggle-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "⌃";
    toggle.append(copy, icon);
    toggle.addEventListener("click", () => {
      const nextExpanded = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(nextExpanded));
      content.hidden = !nextExpanded;
    });
    panel.append(toggle, content);
  }
}

function numberValue(id) {
  return Number($(id).value);
}

function integerValue(id) {
  return Number.parseInt($(id).value, 10);
}

function parseGroupIdList(value, label) {
  const tokens = String(value || "").trim().split(/[\s,，]+/u).filter(Boolean);
  if (tokens.length > 500) throw new Error(`${label}最多 500 個群組 ID`);
  const result = [];
  const seen = new Set();
  for (const token of tokens) {
    if (!/^-[1-9][0-9]*$/u.test(token)) {
      throw new Error(`${label}包含無效群組 ID：${token.slice(0, 30)}`);
    }
    const groupId = Number(token);
    if (!Number.isSafeInteger(groupId)) {
      throw new Error(`${label}包含超出安全範圍的群組 ID`);
    }
    if (!seen.has(groupId)) {
      seen.add(groupId);
      result.push(groupId);
    }
  }
  return result;
}

function mediaGroupIds(prefix, label) {
  const typed = parseGroupIdList($(`edit${prefix}GroupIds`).value, label);
  const known = [...$(`edit${prefix}KnownGroups`).selectedOptions]
    .map((option) => Number(option.value))
    .filter((groupId) => Number.isSafeInteger(groupId));
  const result = [...new Set([...typed, ...known])];
  if ($(`edit${prefix}Enabled`).checked && !result.length) {
    throw new Error(`啟用${label}時，請至少選擇一個允許群組`);
  }
  return result;
}

function normalizeBlockedItems(rawValue, options) {
  const normalized = String(rawValue || "").normalize("NFKC");
  const parts = normalized.split(options.separator);
  const items = [];
  const seen = new Set();
  let totalLength = 0;
  for (const part of parts) {
    let item = part.trim();
    if (!item) continue;
    if (/\p{C}/u.test(item)) {
      throw new Error(`${options.label}不可包含控制字元`);
    }
    item = item.replace(/\s+/gu, " ");
    const itemLength = [...item].length;
    if (itemLength > options.maxItemLength) {
      throw new Error(`${options.label}「${item.slice(0, 20)}」超過 ${options.maxItemLength} 字`);
    }
    const key = item.toLocaleLowerCase("zh-Hant");
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(item);
    totalLength += itemLength;
  }
  if (items.length > options.maxItems) {
    throw new Error(`${options.label}最多只能設定 ${options.maxItems} 項`);
  }
  if (totalLength > options.maxTotalLength) {
    throw new Error(`${options.label}總長度不可超過 ${options.maxTotalLength} 字`);
  }
  return items;
}

function parseBlockedTerms(value) {
  return normalizeBlockedItems(value, {
    label: "不回覆關鍵詞",
    separator: /[\n,，]+/u,
    maxItems: 100,
    maxItemLength: 80,
    maxTotalLength: 4000,
  });
}

function parseBlockedTopics(value) {
  return normalizeBlockedItems(value, {
    label: "不回覆主題",
    separator: /\r?\n/u,
    maxItems: 50,
    maxItemLength: 300,
    maxTotalLength: 8000,
  });
}

function createManualSendRow(account) {
  const container = document.createElement("div");
  const row = document.createElement("div");
  row.className = "manual-send-row";
  const pending = manualMessagePendingAccounts.has(account.id);
  const groupSelect = document.createElement("select");
  groupSelect.setAttribute("aria-label", `選擇 ${String(account.label || "帳號")} 的發送群組`);
  const groups = (Array.isArray(account.joined_groups) ? account.joined_groups : [])
    .filter((group) => {
      const groupId = Number(group?.id);
      return Number.isSafeInteger(groupId) && groupId < 0 && group?.enabled !== false;
    });
  const savedGroup = Number(manualGroupByAccount.get(account.id));
  let selectedGroup = groups.some((group) => Number(group.id) === savedGroup)
    ? savedGroup
    : Number(groups[0]?.id || 0);
  if (!groups.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = account.connected ? "沒有可用群組" : "尚未載入群組";
    groupSelect.appendChild(option);
    groupSelect.disabled = true;
  } else {
    for (const group of groups) {
      const option = document.createElement("option");
      option.value = String(group.id);
      option.textContent = String(group.title || group.id);
      option.selected = Number(group.id) === selectedGroup;
      groupSelect.appendChild(option);
    }
    manualGroupByAccount.set(account.id, selectedGroup);
    groupSelect.disabled = pending;
  }
  groupSelect.addEventListener("change", () => {
    const groupId = Number(groupSelect.value);
    if (Number.isSafeInteger(groupId) && groupId < 0) {
      manualGroupByAccount.set(account.id, groupId);
    }
  });

  const message = document.createElement("textarea");
  message.className = "manual-message-input";
  message.rows = 1;
  message.placeholder = "手動發送訊息";
  message.setAttribute("aria-label", `輸入 ${String(account.label || "帳號")} 的手動訊息`);
  message.value = String(manualMessageDraftByAccount.get(account.id) || "");
  message.disabled = pending;
  message.addEventListener("input", () => {
    manualMessageDraftByAccount.set(account.id, message.value);
  });

  const sendButton = document.createElement("button");
  sendButton.type = "button";
  sendButton.className = "btn primary manual-send-button";
  sendButton.textContent = "➤";
  sendButton.title = "發送訊息";
  sendButton.setAttribute("aria-label", `以 ${String(account.label || "帳號")} 發送訊息`);
  sendButton.disabled = !account.connected || !groups.length || pending;

  const notice = document.createElement("div");
  notice.className = "manual-send-notice";
  notice.setAttribute("role", "status");
  sendButton.addEventListener("click", async () => {
    const groupId = Number(groupSelect.value);
    const text = message.value;
    notice.className = "manual-send-notice";
    if (!Number.isSafeInteger(groupId) || groupId >= 0) {
      notice.textContent = "請先選擇群組";
      notice.classList.add("error");
      return;
    }
    if (!text.trim()) {
      notice.textContent = "請輸入訊息";
      notice.classList.add("error");
      message.focus();
      return;
    }
    manualMessagePendingAccounts.add(account.id);
    groupSelect.disabled = true;
    message.disabled = true;
    try {
      await runButton(sendButton, async () => {
        const result = await api(`/api/accounts/${encodeURIComponent(account.id)}/manual-message`, {
          method: "POST",
          body: JSON.stringify({group_id: groupId, text}),
        });
        if (result.partial === true) {
          const sentUnits = Number(result.sent_utf16_units);
          const safeUnits = Number.isSafeInteger(sentUnits) && sentUnits > 0
            ? Math.min(sentUnits, text.length)
            : 0;
          const remaining = text.slice(safeUnits);
          message.value = remaining;
          if (remaining) manualMessageDraftByAccount.set(account.id, remaining);
          else manualMessageDraftByAccount.delete(account.id);
          notice.textContent = `部分送出 ${Number(result.message_count) || 0} 則；剩餘內容已保留`;
          notice.classList.add("error");
          return;
        }
        if (result.ok !== true) {
          throw new Error("Telegram 未確認訊息已送出");
        }
        message.value = "";
        manualMessageDraftByAccount.delete(account.id);
        notice.textContent = "已發送";
        notice.classList.add("success");
      });
    } catch (error) {
      notice.textContent = error.message;
      notice.classList.add("error");
    } finally {
      manualMessagePendingAccounts.delete(account.id);
      groupSelect.disabled = !groups.length;
      message.disabled = false;
      sendButton.disabled = !account.connected || !groups.length;
    }
  });
  message.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      sendButton.click();
    }
  });

  row.append(groupSelect, message, sendButton);
  container.append(row, notice);
  return container;
}

function createAccountItem(account) {
  const card = document.createElement("article");
  card.className = "account-card";
  const button = document.createElement("button");
  button.type = "button";
  button.className = `account-item${account.id === selectedAccountId ? " active" : ""}`;
  button.setAttribute("aria-pressed", String(account.id === selectedAccountId));
  const dot = document.createElement("span");
  dot.className = `dot${account.connected ? " online" : account.state === "error" ? " error" : ""}`;
  const copy = document.createElement("span");
  const titleRow = document.createElement("span");
  titleRow.className = "account-title-row";
  const title = document.createElement("strong");
  title.textContent = account.label;
  const unreadCount = Math.max(0, Number(account.private_unread_count || 0));
  titleRow.appendChild(title);
  if (unreadCount > 0) {
    const badge = document.createElement("span");
    badge.className = "private-alert-badge";
    badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
    badge.setAttribute("aria-label", `${unreadCount} 則私聊未讀`);
    titleRow.appendChild(badge);
  }
  const meta = document.createElement("small");
  meta.textContent = `${stateName(account)} · ${roleName(account)}`;
  copy.append(titleRow, meta);
  button.append(dot, copy);
  button.addEventListener("click", () => {
    selectedAccountId = account.id;
    formDirty = false;
    groupsDirty = false;
    renderDashboard();
  });
  card.append(button, createManualSendRow(account));
  return card;
}

function renderAccountList() {
  // Preserve the live textarea node while it is being edited. Replacing it
  // would reset the caret, internal scroll position and mobile/Chinese IME.
  // Keep the same row during a send as well, so an already in-flight status
  // response cannot replace it with a permanently disabled pending snapshot.
  if (activeManualMessageEditor() || manualMessagePendingAccounts.size > 0) return;
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

function unlistedMediaGroupIds(account, feature) {
  const knownGroupIds = new Set(
    (Array.isArray(account.joined_groups) ? account.joined_groups : [])
      .map((group) => String(group.id)),
  );
  const allowedGroupIds = Array.isArray(feature.allowed_group_ids)
    ? feature.allowed_group_ids
    : [];
  return allowedGroupIds.filter((groupId) => !knownGroupIds.has(String(groupId)));
}

function fillEditor(account) {
  const media = account.media && typeof account.media === "object" ? account.media : {};
  const image = media.image && typeof media.image === "object" ? media.image : {};
  const voice = media.voice && typeof media.voice === "object" ? media.voice : {};
  const video = media.video && typeof media.video === "object" ? media.video : {};
  const providers = account.media_providers && typeof account.media_providers === "object"
    ? account.media_providers
    : {};
  $("editLabel").value = account.label || "";
  $("editGender").value = account.gender;
  $("editStage").value = account.stage;
  $("editStyle").value = account.style || "";
  $("editTaskName").value = account.task_name || "";
  $("editTaskInfo").value = account.task_info || "";
  $("editBlockedTerms").value = Array.isArray(account.blocked_terms)
    ? account.blocked_terms.join("\n")
    : "";
  $("editBlockedTopics").value = Array.isArray(account.blocked_topics)
    ? account.blocked_topics.join("\n")
    : "";
  $("editBaseUrl").value = account.ai_base_url || "";
  $("editModel").value = account.ai_model || "";
  $("editAdultTextEnabled").checked = Boolean(account.adult_text_enabled);
  $("editImageEnabled").checked = Boolean(image.enabled);
  $("editImageModel").value = image.model || "x-ai/grok-imagine-image-quality";
  $("editImageDailyLimit").value = String(image.daily_limit || 5);
  $("editImageCooldown").value = String(image.cooldown_seconds || 300);
  $("editImageGroupIds").value = unlistedMediaGroupIds(account, image).join("\n");
  $("editVoiceEnabled").checked = Boolean(voice.enabled);
  $("editVoiceModel").value = voice.model || "x-ai/grok-voice-tts-1.0";
  $("editVoiceName").value = voice.voice || (
    account.gender === "male"
      ? "rex"
      : "eve"
  );
  $("editVoiceDailyLimit").value = String(voice.daily_limit || 10);
  $("editVoiceCooldown").value = String(voice.cooldown_seconds || 120);
  $("editVoiceGroupIds").value = unlistedMediaGroupIds(account, voice).join("\n");
  $("editVideoEnabled").checked = Boolean(video.enabled);
  $("editVideoModel").value = video.model || "x-ai/grok-imagine-video-1.5";
  $("editVideoDailyLimit").value = String(video.daily_limit || 1);
  $("editVideoCooldown").value = String(video.cooldown_seconds || 1800);
  $("editVideoGroupIds").value = unlistedMediaGroupIds(account, video).join("\n");
  fillMediaKnownGroups(account, media);
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
  setProviderState("imageProviderState", providers.openrouter_media);
  setProviderState("voiceProviderState", providers.openrouter_media);
  setProviderState("videoProviderState", providers.openrouter_media);
}

function fillMediaKnownGroups(account, media) {
  const groups = Array.isArray(account.joined_groups) ? account.joined_groups : [];
  const accountScopeGroupIds = new Set(
    groups.filter((group) => Boolean(group.enabled)).map((group) => String(group.id)),
  );
  const enabledByPrefix = {
    Image: Boolean(media.image?.enabled),
    Voice: Boolean(media.voice?.enabled),
    Video: Boolean(media.video?.enabled),
  };
  const selectedByPrefix = {
    Image: new Set((media.image?.allowed_group_ids || []).map(String)),
    Voice: new Set((media.voice?.allowed_group_ids || []).map(String)),
    Video: new Set((media.video?.allowed_group_ids || []).map(String)),
  };
  for (const prefix of ["Image", "Voice", "Video"]) {
    const select = $(`edit${prefix}KnownGroups`);
    const configuredGroupIds = selectedByPrefix[prefix];
    const useSingleGroupDefault = enabledByPrefix[prefix] &&
      configuredGroupIds.size === 0 &&
      accountScopeGroupIds.size === 1;
    select.replaceChildren();
    for (const group of groups) {
      const option = document.createElement("option");
      option.value = String(group.id);
      option.textContent = `${String(group.title || group.id)} · ${String(group.id)}`;
      option.selected = configuredGroupIds.has(option.value) ||
        (useSingleGroupDefault && accountScopeGroupIds.has(option.value));
      select.appendChild(option);
    }
    select.disabled = !groups.length;
  }
}

function preselectSoleMediaGroup(prefix) {
  if (!$(`edit${prefix}Enabled`).checked) return;
  if ($(`edit${prefix}GroupIds`).value.trim()) return;
  const select = $(`edit${prefix}KnownGroups`);
  if (select.selectedOptions.length) return;
  const account = selectedAccount();
  if (!account) return;
  const eligibleGroups = (Array.isArray(account.joined_groups) ? account.joined_groups : [])
    .filter((group) => Boolean(group.enabled));
  if (eligibleGroups.length !== 1) return;
  const soleGroupId = String(eligibleGroups[0].id);
  const option = [...select.options].find((item) => item.value === soleGroupId);
  if (option) option.selected = true;
}

function setProviderState(id, readyValue) {
  const node = $(id);
  const ready = readyValue === true;
  node.classList.toggle("ready", ready);
  node.querySelector("strong").textContent = ready ? "已就緒" : "未就緒";
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

function clearConversationList(message = "") {
  const list = $("conversationList");
  list.replaceChildren();
  if (message) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = message;
    list.appendChild(empty);
  }
}

function setConversationSelection(key) {
  if (conversationSelectionKey === key) return;
  conversationSelectionKey = key;
  conversationRequestSequence += 1;
}

function conversationRequestIsCurrent(requestSequence, requestKey) {
  const account = selectedAccount();
  const groupId = $("conversationGroup").value;
  const currentKey = account && groupId ? `${account.id}:${groupId}` : "";
  return requestSequence === conversationRequestSequence &&
    requestKey === conversationSelectionKey &&
    requestKey === currentKey;
}

function renderConversationGroups(account) {
  const select = $("conversationGroup");
  const groups = Array.isArray(account.joined_groups) ? account.joined_groups : [];
  const availableIds = new Set(groups.map((group) => String(group.id)));
  let selected = String(conversationGroupByAccount.get(account.id) || "");
  if (!availableIds.has(selected)) selected = groups.length ? String(groups[0].id) : "";
  conversationGroupByAccount.set(account.id, selected);
  select.replaceChildren();
  if (!groups.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = account.connected ? "目前沒有已加入群組" : "帳號連線後才可選擇群組";
    select.appendChild(option);
  } else {
    for (const group of groups) {
      const option = document.createElement("option");
      option.value = String(group.id);
      option.textContent = String(group.title || group.id);
      option.selected = option.value === selected;
      select.appendChild(option);
    }
  }
  select.disabled = !groups.length;
  const expectedKey = selected ? `${account.id}:${selected}` : "";
  setConversationSelection(expectedKey);
  $("refreshConversationButton").disabled = !groups.length ||
    (conversationPendingKey === expectedKey &&
      conversationPendingSequence === conversationRequestSequence);
  if (conversationLoadedKey && conversationLoadedKey !== expectedKey) {
    conversationLoadedKey = "";
    setNotice("conversationNotice", "");
    clearConversationList(groups.length ? "選擇群組後按「重新整理記錄」。" : "目前沒有可讀取的群組。");
  } else if (!conversationLoadedKey && !$("conversationList").childElementCount) {
    clearConversationList(groups.length ? "選擇群組後按「重新整理記錄」。" : "目前沒有可讀取的群組。");
  }
}

function formatConversationTime(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "時間未知";
  return new Intl.DateTimeFormat("zh-TW", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp * 1000));
}

function renderConversationLog(data) {
  const list = $("conversationList");
  list.replaceChildren();
  const messages = Array.isArray(data.messages) ? data.messages.slice(-100) : [];
  if (!messages.length) {
    clearConversationList("這個群組目前沒有保留中的聊天記錄。");
    return;
  }
  for (const message of messages) {
    const item = document.createElement("article");
    const role = message.role === "assistant" ? "assistant" : "user";
    item.className = `conversation-message ${role}`;
    const head = document.createElement("div");
    head.className = "conversation-message-head";
    const sender = document.createElement("strong");
    sender.textContent = String(
      message.sender_name || (role === "assistant" ? "AI 帳號" : "群組成員")
    );
    const created = document.createElement("time");
    created.textContent = formatConversationTime(message.created_at);
    const body = document.createElement("p");
    body.className = "conversation-message-body";
    body.textContent = String(message.content || "");
    head.append(sender, created);
    item.append(head, body);
    list.appendChild(item);
  }
  list.scrollTop = list.scrollHeight;
}

function clearMediaJobs(message = "") {
  const list = $("mediaJobList");
  list.replaceChildren();
  if (message) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = message;
    list.appendChild(empty);
  }
}

function formatMediaJobTime(value) {
  return formatConversationTime(value);
}

function renderMediaJobs(data) {
  const jobs = Array.isArray(data.jobs) ? data.jobs : [];
  const list = $("mediaJobList");
  list.replaceChildren();
  if (!jobs.length) {
    clearMediaJobs("這個帳號目前沒有媒體任務。");
    return;
  }
  for (const job of jobs) {
    const item = document.createElement("article");
    item.className = "media-job";
    const title = document.createElement("strong");
    title.textContent = `${String(job.kind || "media")} · ${String(job.status || "unknown")}`;
    const created = document.createElement("time");
    created.textContent = formatMediaJobTime(job.updated_at || job.created_at);
    const meta = document.createElement("small");
    const groupId = Number(job.group_id);
    const groupText = Number.isSafeInteger(groupId) && groupId < 0
      ? `群組 ${String(groupId)}`
      : "群組未知";
    meta.textContent = `${groupText} · 任務 ${String(job.job_id || "—")}`;
    item.append(title, created, meta);
    list.appendChild(item);
  }
}

function clearPrivateAlerts(message = "") {
  const list = $("privateAlertList");
  list.replaceChildren();
  if (message) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = message;
    list.appendChild(empty);
  }
}

function updatePrivateUnreadCount(accountId, value, latestAt = 0) {
  const unreadCount = Math.max(0, Number(value || 0));
  const latest = Math.max(0, Number(latestAt || 0));
  const account = dashboardState?.accounts.find((item) => item.id === accountId);
  if (account) {
    account.private_unread_count = unreadCount;
    account.private_alert_latest_at = latest;
  }
  if (dashboardState?.summary) {
    dashboardState.summary.private_unread_count = (dashboardState.accounts || [])
      .reduce((total, item) => total + Math.max(0, Number(item.private_unread_count || 0)), 0);
  }
  const totalPrivateUnread = Math.max(
    0,
    Number(dashboardState?.summary?.private_unread_count || 0),
  );
  $("summaryPrivateUnread").textContent = String(totalPrivateUnread);
  document.title = totalPrivateUnread > 0
    ? `(${totalPrivateUnread}) Telegram AI 多帳號控制台`
    : "Telegram AI 多帳號控制台";
  if (selectedAccountId === accountId) {
    $("metricPrivateUnread").textContent = String(unreadCount);
    $("ackAllPrivateAlertsButton").disabled = unreadCount <= 0;
    if (account) $("selectedCompactSummary").textContent = compactAccountSummary(account);
  }
  renderAccountList();
}

function renderPrivateAlerts(data) {
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  const accountId = String(data.account_id || "");
  const list = $("privateAlertList");
  list.replaceChildren();
  if (!alerts.length) {
    clearPrivateAlerts("最近 24 小時沒有私聊提醒。");
    return;
  }
  for (const alert of alerts) {
    const alertId = String(alert.alert_id || "");
    const unread = !alert.acknowledged;
    const item = document.createElement("article");
    item.className = `private-alert${unread ? " unread" : ""}`;
    const head = document.createElement("div");
    head.className = "private-alert-head";
    const sender = document.createElement("strong");
    sender.textContent = String(alert.sender_name || "Telegram 使用者");
    head.appendChild(sender);
    const username = String(alert.sender_username || "");
    if (username) {
      const handle = document.createElement("small");
      handle.textContent = `@${username.replace(/^@/u, "")}`;
      head.appendChild(handle);
    }
    const created = document.createElement("time");
    created.textContent = formatConversationTime(alert.created_at);
    const preview = document.createElement("p");
    preview.className = "private-alert-preview";
    preview.textContent = String(alert.preview || "（非文字訊息）");
    item.append(head, created, preview);
    if (unread && alertId) {
      const acknowledge = document.createElement("button");
      acknowledge.type = "button";
      acknowledge.className = "btn small";
      acknowledge.textContent = "標記已處理";
      acknowledge.addEventListener("click", async () => {
        await acknowledgePrivateAlerts(accountId, [alertId], acknowledge);
      });
      item.appendChild(acknowledge);
    }
    list.appendChild(item);
  }
}

async function loadPrivateAlerts(accountId) {
  if (!accountId) return;
  const requestSequence = ++privateAlertsRequestSequence;
  setNotice("privateAlertsNotice", "正在讀取私聊提醒…", "warning");
  try {
    const query = new URLSearchParams({limit: "20", unread_only: "false"});
    const result = await api(
      `/api/accounts/${encodeURIComponent(accountId)}/private-alerts?${query.toString()}`
    );
    if (requestSequence !== privateAlertsRequestSequence ||
        selectedAccountId !== accountId) return;
    privateAlertsLoadedAccountId = accountId;
    privateAlertsLoadedUnreadCount = Math.max(0, Number(result.unread_count || 0));
    privateAlertsLoadedLatestAt = Math.max(0, Number(result.private_alert_latest_at || 0));
    updatePrivateUnreadCount(
      accountId,
      privateAlertsLoadedUnreadCount,
      privateAlertsLoadedLatestAt,
    );
    renderPrivateAlerts(result);
    setNotice(
      "privateAlertsNotice",
      `最近 24 小時 ${Array.isArray(result.alerts) ? result.alerts.length : 0} 則提醒`,
      "success",
    );
  } catch (error) {
    if (requestSequence !== privateAlertsRequestSequence ||
        selectedAccountId !== accountId) return;
    setNotice("privateAlertsNotice", error.message, "error");
  }
}

async function acknowledgePrivateAlerts(accountId, alertIds, button) {
  if (!accountId) return;
  await runButton(button, async () => {
    try {
      const body = alertIds === null ? {all: true} : {alert_ids: alertIds};
      const result = await api(
        `/api/accounts/${encodeURIComponent(accountId)}/private-alerts/ack`,
        {method: "POST", body: JSON.stringify(body)},
      );
      if (selectedAccountId !== accountId) return;
      const nextUnreadCount = Math.max(0, Number(result.unread_count || 0));
      const nextLatestAt = Math.max(0, Number(result.private_alert_latest_at || 0));
      privateAlertsLoadedUnreadCount = nextUnreadCount;
      privateAlertsLoadedLatestAt = nextLatestAt;
      updatePrivateUnreadCount(accountId, nextUnreadCount, nextLatestAt);
      await loadPrivateAlerts(accountId);
    } catch (error) {
      if (selectedAccountId !== accountId) return;
      setNotice("privateAlertsNotice", error.message, "error");
    }
  });
  if (button === $("ackAllPrivateAlertsButton")) {
    button.disabled = Number(selectedAccount()?.private_unread_count || 0) <= 0;
  }
}

function renderSelected(account) {
  $("emptyPanel").classList.add("hidden");
  $("accountPanels").classList.remove("hidden");
  const privateUnread = Math.max(0, Number(account.private_unread_count || 0));
  $("selectedCompactSummary").textContent = compactAccountSummary(account);
  $("selectedState").textContent = stateName(account);
  $("selectedLabel").textContent = account.label;
  $("selectedIdentity").textContent = `${account.telegram_name || "Telegram 帳號"} · ${roleName(account)} · ID ${account.telegram_user_id}`;
  $("controlButton").textContent = account.enabled ? "停用帳號" : "啟用帳號";
  $("metricConnection").textContent = account.connected ? "已連線" : "未連線";
  $("metricMessages").textContent = Number(account.message_count || 0).toLocaleString();
  $("metricReplies").textContent = Number(account.replies_sent || 0).toLocaleString();
  $("metricGroups").textContent = String(account.joined_groups?.length || 0);
  $("metricPrivateUnread").textContent = String(privateUnread);
  $("ackAllPrivateAlertsButton").disabled = privateUnread <= 0;
  $("metricBlocked").textContent = String(account.blocked_messages || 0);
  $("metricBlocked").title =
    `政策攔截：${account.policy_rejections || 0}；風格重試：${account.style_rejections || 0}`;
  $("metricErrors").textContent = String(account.errors || 0);
  setNotice("accountNotice", account.last_error || "", account.last_error ? "error" : "");
  // A status request may have started just before the user edited a field.
  // Re-check dirty state at the render boundary to avoid overwriting that edit.
  if (!formDirty) fillEditor(account);
  if (!groupsDirty) renderGroups(account);
  renderConversationGroups(account);
  if (privateAlertsLoadedAccountId !== account.id ||
      privateAlertsLoadedUnreadCount !== privateUnread ||
      privateAlertsLoadedLatestAt !== Math.max(0, Number(account.private_alert_latest_at || 0))) {
    privateAlertsLoadedAccountId = "";
    privateAlertsLoadedUnreadCount = -1;
    privateAlertsLoadedLatestAt = 0;
    clearPrivateAlerts("正在讀取私聊提醒…");
    loadPrivateAlerts(account.id);
  }
  if (mediaJobsLoadedAccountId !== account.id) {
    mediaJobsRequestSequence += 1;
    mediaJobsLoadedAccountId = "";
    setNotice("mediaJobsNotice", "");
    clearMediaJobs("按「重新整理任務」讀取這個帳號的媒體任務。");
  }
}

function renderDashboard() {
  const summary = dashboardState?.summary || {};
  const totalPrivateUnread = Math.max(
    0,
    Number(summary.private_unread_count || 0),
  );
  $("summaryTotal").textContent = String(summary.total || 0);
  $("summaryEnabled").textContent = String(summary.enabled || 0);
  $("summaryConnected").textContent = String(summary.connected || 0);
  $("summaryPrivateUnread").textContent = String(totalPrivateUnread);
  $("summaryMemory").textContent = `${summary.memory_ttl_hours || 24}h`;
  document.title = totalPrivateUnread > 0
    ? `(${totalPrivateUnread}) Telegram AI 多帳號控制台`
    : "Telegram AI 多帳號控制台";
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

async function refreshPrivateIndicators() {
  if (privateIndicatorRefreshPending || !dashboardState) return;
  privateIndicatorRefreshPending = true;
  try {
    const data = await api("/api/status");
    const nextAccounts = Array.isArray(data.accounts) ? data.accounts : [];
    const nextById = new Map(
      nextAccounts.map((account) => [
        String(account.id || ""),
        {
          unread: Math.max(0, Number(account.private_unread_count || 0)),
          latest: Math.max(0, Number(account.private_alert_latest_at || 0)),
        },
      ]),
    );
    for (const account of dashboardState.accounts || []) {
      if (nextById.has(String(account.id))) {
        const next = nextById.get(String(account.id));
        account.private_unread_count = next?.unread || 0;
        account.private_alert_latest_at = next?.latest || 0;
      }
    }
    const totalPrivateUnread = (dashboardState.accounts || []).reduce(
      (total, account) =>
        total + Math.max(0, Number(account.private_unread_count || 0)),
      0,
    );
    if (dashboardState.summary) {
      dashboardState.summary.private_unread_count = totalPrivateUnread;
    }
    $("summaryPrivateUnread").textContent = String(totalPrivateUnread);
    document.title = totalPrivateUnread > 0
      ? `(${totalPrivateUnread}) Telegram AI 多帳號控制台`
      : "Telegram AI 多帳號控制台";
    renderAccountList();
    const account = selectedAccount();
    if (!account) return;
    const privateUnread = Math.max(
      0,
      Number(account.private_unread_count || 0),
    );
    const privateLatest = Math.max(0, Number(account.private_alert_latest_at || 0));
    $("metricPrivateUnread").textContent = String(privateUnread);
    $("ackAllPrivateAlertsButton").disabled = privateUnread <= 0;
    $("selectedCompactSummary").textContent = compactAccountSummary(account);
    if (
      privateAlertsLoadedUnreadCount !== privateUnread ||
      privateAlertsLoadedLatestAt !== privateLatest
    ) {
      await loadPrivateAlerts(account.id);
    }
  } finally {
    privateIndicatorRefreshPending = false;
  }
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
    blocked_terms: parseBlockedTerms($("addBlockedTerms").value),
    blocked_topics: parseBlockedTopics($("addBlockedTopics").value),
    adult_text_enabled: $("addAdultTextEnabled").checked,
    enabled: $("addEnabled").checked,
  };
  if ($("addBaseUrl").value.trim()) payload.ai_base_url = $("addBaseUrl").value;
  if ($("addModel").value.trim()) payload.ai_model = $("addModel").value;
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
  $("addEnabled").checked = false;
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
    try {
      const payload = collectNewAccountPayload();
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

for (const prefix of ["Image", "Voice", "Video"]) {
  $(`edit${prefix}Enabled`).addEventListener("change", () => {
    preselectSoleMediaGroup(prefix);
  });
}

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const account = selectedAccount();
  if (!account) return;
  setNotice("settingsNotice", "正在儲存…", "warning");
  await runButton($("saveSettingsButton"), async () => {
    try {
      const payload = {
        revision: account.revision,
        label: $("editLabel").value,
        gender: $("editGender").value,
        stage: $("editStage").value,
        style: $("editStyle").value,
        task_name: $("editTaskName").value,
        task_info: $("editTaskInfo").value,
        blocked_terms: parseBlockedTerms($("editBlockedTerms").value),
        blocked_topics: parseBlockedTopics($("editBlockedTopics").value),
        ai_base_url: $("editBaseUrl").value,
        ai_model: $("editModel").value,
        adult_text_enabled: $("editAdultTextEnabled").checked,
        media: {
          image: {
            enabled: $("editImageEnabled").checked,
            model: $("editImageModel").value,
            voice: "",
            daily_limit: integerValue("editImageDailyLimit"),
            cooldown_seconds: integerValue("editImageCooldown"),
            allowed_group_ids: mediaGroupIds("Image", "圖片生成"),
          },
          voice: {
            enabled: $("editVoiceEnabled").checked,
            model: $("editVoiceModel").value,
            voice: $("editVoiceName").value,
            daily_limit: integerValue("editVoiceDailyLimit"),
            cooldown_seconds: integerValue("editVoiceCooldown"),
            allowed_group_ids: mediaGroupIds("Voice", "語音生成"),
          },
          video: {
            enabled: $("editVideoEnabled").checked,
            model: $("editVideoModel").value,
            voice: "",
            daily_limit: integerValue("editVideoDailyLimit"),
            cooldown_seconds: integerValue("editVideoCooldown"),
            allowed_group_ids: mediaGroupIds("Video", "影片生成"),
          },
        },
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

$("refreshMediaJobsButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  const accountId = account.id;
  const limit = Number.parseInt($("mediaJobsLimit").value, 10);
  if (![20, 50, 100].includes(limit)) {
    setNotice("mediaJobsNotice", "顯示筆數不正確", "error");
    return;
  }
  const requestSequence = ++mediaJobsRequestSequence;
  setNotice("mediaJobsNotice", "正在讀取媒體任務…", "warning");
  await runButton($("refreshMediaJobsButton"), async () => {
    try {
      const query = new URLSearchParams({limit: String(limit)});
      const result = await api(
        `/api/accounts/${encodeURIComponent(accountId)}/media-jobs?${query.toString()}`
      );
      if (requestSequence !== mediaJobsRequestSequence ||
          selectedAccountId !== accountId) return;
      mediaJobsLoadedAccountId = accountId;
      renderMediaJobs(result);
      setNotice(
        "mediaJobsNotice",
        `已顯示 ${Array.isArray(result.jobs) ? result.jobs.length : 0} 筆任務`,
        "success",
      );
    } catch (error) {
      if (requestSequence !== mediaJobsRequestSequence ||
          selectedAccountId !== accountId) return;
      setNotice("mediaJobsNotice", error.message, "error");
    }
  });
});

$("refreshPrivateAlertsButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  await runButton($("refreshPrivateAlertsButton"), async () => {
    await loadPrivateAlerts(account.id);
  });
});

$("ackAllPrivateAlertsButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account || Number(account.private_unread_count || 0) <= 0) return;
  await acknowledgePrivateAlerts(account.id, null, $("ackAllPrivateAlertsButton"));
});

$("conversationGroup").addEventListener("change", () => {
  const account = selectedAccount();
  if (!account) return;
  conversationGroupByAccount.set(account.id, $("conversationGroup").value);
  const selectedGroup = $("conversationGroup").value;
  setConversationSelection(selectedGroup ? `${account.id}:${selectedGroup}` : "");
  conversationLoadedKey = "";
  setNotice("conversationNotice", "");
  clearConversationList("按「重新整理記錄」讀取這個群組的聊天內容。");
});

$("refreshConversationButton").addEventListener("click", async () => {
  const account = selectedAccount();
  if (!account) return;
  const rawGroupId = $("conversationGroup").value;
  const groupId = Number(rawGroupId);
  const limit = Number.parseInt($("conversationLimit").value, 10);
  if (!/^-[1-9][0-9]*$/u.test(rawGroupId) || !Number.isSafeInteger(groupId)) {
    setNotice("conversationNotice", "請先選擇有效群組", "error");
    return;
  }
  if (![20, 50, 100].includes(limit)) {
    setNotice("conversationNotice", "顯示則數不正確", "error");
    return;
  }
  const requestKey = `${account.id}:${rawGroupId}`;
  setConversationSelection(requestKey);
  const requestSequence = ++conversationRequestSequence;
  conversationPendingKey = requestKey;
  conversationPendingSequence = requestSequence;
  setNotice("conversationNotice", "正在讀取聊天記錄…", "warning");
  const refreshButton = $("refreshConversationButton");
  refreshButton.disabled = true;
  try {
    const query = new URLSearchParams({
      group_id: String(groupId),
      limit: String(limit),
    });
    const result = await api(
      `/api/accounts/${encodeURIComponent(account.id)}/conversation-log?${query.toString()}`
    );
    if (!conversationRequestIsCurrent(requestSequence, requestKey)) return;
    conversationLoadedKey = requestKey;
    renderConversationLog(result);
    setNotice(
      "conversationNotice",
      `已顯示 ${Array.isArray(result.messages) ? result.messages.length : 0} 則記錄`,
      "success",
    );
  } catch (error) {
    if (!conversationRequestIsCurrent(requestSequence, requestKey)) return;
    setNotice("conversationNotice", error.message, "error");
  } finally {
    if (conversationPendingSequence === requestSequence) {
      conversationPendingKey = "";
      conversationPendingSequence = 0;
    }
    if (conversationRequestIsCurrent(requestSequence, requestKey)) {
      refreshButton.disabled = false;
    }
  }
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
      conversationRequestSequence += 1;
      conversationPendingKey = "";
      conversationPendingSequence = 0;
      conversationLoadedKey = "";
      clearConversationList("聊天記憶已清除。");
      setNotice("conversationNotice", "");
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
    resetDashboardClientState();
    showLogin();
  }
});

setupCollapsiblePanels();

refresh().catch((error) => {
  if (error.message !== "unauthorized") {
    showLogin();
    setNotice("loginNotice", "控制台暫時無法讀取，請稍後再試", "error");
  }
});

setInterval(() => {
  const canRefreshEverything = !document.hidden &&
    !formDirty && !groupsDirty &&
    !dashboardEditorHasFocus() &&
    manualMessagePendingAccounts.size === 0 &&
    $("addPanel").classList.contains("hidden");
  if (canRefreshEverything) {
    refresh().catch(() => {});
  } else {
    refreshPrivateIndicators().catch(() => {});
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

    @staticmethod
    def _manual_message_payload(
        payload: dict[str, object],
    ) -> tuple[int, str]:
        if set(payload) != {"group_id", "text"}:
            raise ValueError("manual message requires only group_id and text")
        group_id = payload.get("group_id")
        if (
            isinstance(group_id, bool)
            or not isinstance(group_id, int)
            or not -(2**63) <= group_id < 0
        ):
            raise ValueError("group_id must be a negative integer")
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise ValueError("text must be a string")
        if not raw_text.strip():
            raise ValueError("text cannot be empty")
        return group_id, raw_text

    @staticmethod
    def _conversation_group_id(raw_value: str) -> int:
        if (
            not raw_value.startswith("-")
            or not raw_value[1:].isascii()
            or not raw_value[1:].isdigit()
            or raw_value[1:].startswith("0")
        ):
            raise ValueError("group_id must be a negative integer")
        group_id = int(raw_value)
        if group_id < -(2**63):
            raise ValueError("group_id is outside the supported range")
        return group_id

    @staticmethod
    def _conversation_limit(raw_value: str) -> int:
        if (
            not raw_value
            or not raw_value.isascii()
            or not raw_value.isdigit()
            or (len(raw_value) > 1 and raw_value.startswith("0"))
        ):
            raise ValueError("limit must be an integer between 1 and 100")
        limit = int(raw_value)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return limit

    @staticmethod
    def _private_unread_only(raw_value: str) -> bool:
        if raw_value == "true":
            return True
        if raw_value == "false":
            return False
        raise ValueError("unread_only must be true or false")

    @staticmethod
    def _private_ack_payload(
        payload: dict[str, object],
    ) -> tuple[list[str] | None, bool]:
        if set(payload) == {"all"}:
            if payload["all"] is not True:
                raise ValueError("all must be true")
            return None, True
        if set(payload) != {"alert_ids"}:
            raise ValueError("provide either alert_ids or all")
        raw_ids = payload["alert_ids"]
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 100:
            raise ValueError("alert_ids must contain between 1 and 100 items")
        alert_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in raw_ids:
            if not isinstance(raw_id, str):
                raise ValueError("alert_ids must contain strings")
            alert_id = raw_id.strip()
            if not alert_id or len(alert_id) > 160:
                raise ValueError("alert_id must contain between 1 and 160 characters")
            if alert_id in seen:
                raise ValueError("alert_ids must not contain duplicates")
            seen.add(alert_id)
            alert_ids.append(alert_id)
        return alert_ids, False

    @classmethod
    def _reject_api_key_fields(cls, payload: dict[str, object]) -> None:
        def contains_api_key(value: object) -> bool:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = "".join(
                        char for char in str(key).casefold() if char.isalnum()
                    )
                    if "apikey" in normalized or contains_api_key(nested):
                        return True
            elif isinstance(value, list):
                return any(contains_api_key(item) for item in value)
            return False

        if contains_api_key(payload):
            raise ValueError(
                "API keys are configured only through Railway Variables"
            )

    @classmethod
    def _without_api_key_fields(cls, value: object) -> object:
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for raw_key, nested in value.items():
                key = str(raw_key)
                normalized = "".join(
                    char for char in key.casefold() if char.isalnum()
                )
                if "apikey" in normalized:
                    continue
                result[key] = cls._without_api_key_fields(nested)
            return result
        if isinstance(value, list):
            return [cls._without_api_key_fields(item) for item in value]
        if isinstance(value, tuple):
            return [cls._without_api_key_fields(item) for item in value]
        return value

    @staticmethod
    def _public_media_jobs(
        result: object,
        *,
        account_id: str,
        limit: int,
    ) -> dict[str, object]:
        if isinstance(result, dict):
            raw_jobs = result.get("jobs", [])
        elif isinstance(result, list):
            raw_jobs = result
        else:
            raise RuntimeError("invalid media jobs response")
        if not isinstance(raw_jobs, list):
            raise RuntimeError("invalid media jobs list")

        jobs: list[dict[str, object]] = []
        for raw_job in raw_jobs[:limit]:
            if not isinstance(raw_job, dict):
                continue
            group_id = raw_job.get("group_id", 0)
            if isinstance(group_id, bool) or not isinstance(group_id, int):
                group_id = 0
            created_at = raw_job.get("created_at", 0)
            if isinstance(created_at, bool) or not isinstance(created_at, int):
                created_at = 0
            updated_at = raw_job.get("updated_at", 0)
            if isinstance(updated_at, bool) or not isinstance(updated_at, int):
                updated_at = 0
            jobs.append(
                {
                    "job_id": str(
                        raw_job.get("job_id", raw_job.get("id", ""))
                    )[:160],
                    "kind": str(
                        raw_job.get("kind", raw_job.get("media_type", "media"))
                    )[:40],
                    "status": str(raw_job.get("status", "unknown"))[:80],
                    "group_id": group_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
        return {
            "account_id": account_id,
            "count": len(jobs),
            "jobs": jobs,
        }

    @staticmethod
    def _public_conversation_log(
        result: object,
        *,
        account_id: str,
        group_id: int,
        limit: int,
    ) -> dict[str, object]:
        if isinstance(result, dict):
            raw_messages = result.get("messages", [])
        elif isinstance(result, list):
            raw_messages = result
        else:
            raise RuntimeError("invalid conversation log response")
        if not isinstance(raw_messages, list):
            raise RuntimeError("invalid conversation messages response")

        messages: list[dict[str, object]] = []
        for raw_message in raw_messages[-limit:]:
            if not isinstance(raw_message, dict):
                continue
            role = str(raw_message.get("role", ""))
            if role not in {"user", "assistant"}:
                continue
            created_at = raw_message.get("created_at", 0)
            if isinstance(created_at, bool) or not isinstance(created_at, int):
                created_at = 0
            messages.append(
                {
                    "role": role,
                    "sender_name": str(raw_message.get("sender_name", ""))[:120],
                    "content": str(raw_message.get("content", ""))[:4000],
                    "created_at": created_at,
                }
            )
        return {
            "account_id": account_id,
            "group_id": group_id,
            "count": len(messages),
            "messages": messages,
        }

    @staticmethod
    def _public_private_alerts(
        result: object,
        *,
        account_id: str,
        limit: int,
    ) -> dict[str, object]:
        if not isinstance(result, dict):
            raise RuntimeError("invalid private alerts response")
        raw_alerts = result.get("alerts", [])
        if not isinstance(raw_alerts, list):
            raise RuntimeError("invalid private alerts list")
        raw_unread = result.get("unread_count", 0)
        unread_count = (
            raw_unread
            if isinstance(raw_unread, int) and not isinstance(raw_unread, bool)
            else 0
        )
        raw_latest_at = result.get("latest_at", 0)
        latest_at = (
            raw_latest_at
            if isinstance(raw_latest_at, int) and not isinstance(raw_latest_at, bool)
            else 0
        )
        alerts: list[dict[str, object]] = []
        for raw_alert in raw_alerts[:limit]:
            if not isinstance(raw_alert, dict):
                continue
            alert_id = str(raw_alert.get("alert_id", ""))[:160]
            if not alert_id:
                continue
            created_at = raw_alert.get("created_at", 0)
            if isinstance(created_at, bool) or not isinstance(created_at, int):
                created_at = 0
            alerts.append(
                {
                    "alert_id": alert_id,
                    "sender_name": str(raw_alert.get("sender_name", ""))[:120],
                    "preview": str(raw_alert.get("preview", ""))[:280],
                    "created_at": created_at,
                    "acknowledged": bool(raw_alert.get("acknowledged", False)),
                }
            )
        return {
            "account_id": account_id,
            "unread_count": max(0, unread_count),
            "latest_at": max(0, latest_at),
            "count": len(alerts),
            "alerts": alerts,
        }

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
                self._without_api_key_fields(await self.manager.status()),
                headers={"X-CSRF-Token": session.csrf_token},
            )

        @web.get("/api/accounts/{account_id}/private-alerts")
        async def private_alerts(account_id: str, request: Request) -> JSONResponse:
            session, blocked = self._require_auth(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            if set(request.query_params.keys()) - {"limit", "unread_only"}:
                raise ValueError("unsupported private alerts query parameter")
            limit_values = request.query_params.getlist("limit")
            unread_values = request.query_params.getlist("unread_only")
            if len(limit_values) > 1 or len(unread_values) > 1:
                raise ValueError("private alerts query parameters must not be repeated")
            limit = self._conversation_limit(
                limit_values[0] if limit_values else "20"
            )
            unread_only = self._private_unread_only(
                unread_values[0] if unread_values else "false"
            )
            result = await self.manager.private_alerts(  # type: ignore[attr-defined]
                account_id,
                limit=limit,
                unread_only=unread_only,
            )
            return JSONResponse(
                self._public_private_alerts(
                    result,
                    account_id=account_id,
                    limit=limit,
                )
            )

        @web.post("/api/accounts/{account_id}/private-alerts/ack")
        async def acknowledge_private_alerts(
            account_id: str,
            request: Request,
        ) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked  # type: ignore[return-value]
            alert_ids, acknowledge_all = self._private_ack_payload(
                await self._read_payload(request)
            )
            result = await self.manager.acknowledge_private_alerts(  # type: ignore[attr-defined]
                account_id,
                alert_ids=alert_ids,
                acknowledge_all=acknowledge_all,
            )
            if not isinstance(result, dict):
                raise RuntimeError(
                    "invalid private alerts acknowledgement response"
                )
            raw_acknowledged = result.get("acknowledged", 0)
            raw_unread_count = result.get("unread_count", 0)
            raw_latest_at = result.get("latest_at", 0)
            acknowledged = (
                max(0, raw_acknowledged)
                if isinstance(raw_acknowledged, int)
                and not isinstance(raw_acknowledged, bool)
                else 0
            )
            unread_count = (
                max(0, raw_unread_count)
                if isinstance(raw_unread_count, int)
                and not isinstance(raw_unread_count, bool)
                else 0
            )
            latest_at = (
                max(0, raw_latest_at)
                if isinstance(raw_latest_at, int)
                and not isinstance(raw_latest_at, bool)
                else 0
            )
            return JSONResponse(
                {
                    "account_id": account_id,
                    "acknowledged": acknowledged,
                    "unread_count": unread_count,
                    "latest_at": latest_at,
                }
            )

        @web.get("/api/accounts/{account_id}/media-jobs")
        async def media_jobs(account_id: str, request: Request) -> JSONResponse:
            session, blocked = self._require_auth(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            limit_values = request.query_params.getlist("limit")
            if len(limit_values) > 1:
                raise ValueError("limit must not be repeated")
            limit = self._conversation_limit(
                limit_values[0] if limit_values else "20"
            )
            result = await self.manager.media_jobs(account_id, limit)  # type: ignore[attr-defined]
            return JSONResponse(
                self._public_media_jobs(
                    result,
                    account_id=account_id,
                    limit=limit,
                )
            )

        @web.get("/api/accounts/{account_id}/conversation-log")
        async def conversation_log(account_id: str, request: Request) -> JSONResponse:
            session, blocked = self._require_auth(request)
            if blocked is not None or session is None:
                return blocked  # type: ignore[return-value]
            group_values = request.query_params.getlist("group_id")
            limit_values = request.query_params.getlist("limit")
            if len(group_values) != 1 or len(limit_values) > 1:
                raise ValueError("group_id and limit must not be repeated")
            group_id = self._conversation_group_id(group_values[0])
            limit = self._conversation_limit(
                limit_values[0] if limit_values else "100"
            )
            result = await self.manager.conversation_log(
                account_id,
                group_id,
                limit,
            )
            return JSONResponse(
                self._public_conversation_log(
                    result,
                    account_id=account_id,
                    group_id=group_id,
                    limit=limit,
                )
            )

        @web.post("/api/accounts")
        async def create_account(request: Request) -> JSONResponse:
            session, blocked = self._require_action(request)
            if blocked is not None or session is None:
                return blocked
            payload = await self._read_payload(request)
            self._reject_api_key_fields(payload)
            return JSONResponse(
                self._without_api_key_fields(
                    await self.manager.create_account(payload, session.session_id)
                ),
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
            self._reject_api_key_fields(payload)
            return JSONResponse(
                self._without_api_key_fields(
                    await self.manager.update_account(account_id, payload)
                )
            )

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
                self._without_api_key_fields(
                    await self.manager.set_enabled(account_id, enabled, revision)
                )
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
                self._without_api_key_fields(
                    await self.manager.set_groups(
                        account_id,
                        all_groups,
                        group_ids,
                        revision,
                    )
                )
            )

        @web.post("/api/accounts/{account_id}/restart")
        async def restart(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            return JSONResponse(
                self._without_api_key_fields(
                    await self.manager.restart_account(account_id)
                )
            )

        @web.post("/api/accounts/{account_id}/model/test")
        async def model_test(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            return JSONResponse(
                self._without_api_key_fields(
                    await self.manager.test_model(account_id)
                )
            )

        @web.post("/api/accounts/{account_id}/manual-message")
        async def manual_message(account_id: str, request: Request) -> JSONResponse:
            _, blocked = self._require_action(request)
            if blocked is not None:
                return blocked
            payload = await self._read_payload(request)
            self._reject_api_key_fields(payload)
            group_id, text = self._manual_message_payload(payload)
            result = await self.manager.manual_send_text(  # type: ignore[attr-defined]
                account_id,
                group_id,
                text,
            )
            if isinstance(result, dict):
                return JSONResponse(self._without_api_key_fields(result))
            return JSONResponse({"ok": True})

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
