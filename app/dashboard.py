"""
控制台 - FastAPI Web UI（深色、繁體中文、單文件前端）
功能：登入/登出、帳號狀態、啟動/停止/刪除、新增帳號（TG 登入流程）、
      人設檢視/重新生成、私訊查看、統計
安全：session cookie（HttpOnly + SameSite=Strict）、登入限流、登出路由
"""

from __future__ import annotations

import hashlib
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .manager import AccountManager
from .telegram_login import (
    LoginConflict,
    LoginExpired,
    LoginRateLimit,
    TelegramLoginService,
)


class Dashboard:
    def __init__(self, config, manager: AccountManager,
                 login_service: TelegramLoginService):
        self.config = config
        self.manager = manager
        self.login_service = login_service
        self.app = FastAPI(title="SDF 控制台")
        self._sessions: dict[str, float] = {}
        self._login_attempts: dict[str, list[float]] = {}
        self._setup_routes()

    # ---------- 工具 ----------

    def _check_session(self, request: Request) -> bool:
        sid = request.cookies.get("sdf_session", "")
        if not sid or sid not in self._sessions:
            return False
        if time.time() > self._sessions[sid]:
            self._sessions.pop(sid, None)
            return False
        self._sessions[sid] = time.time() + 3600
        return True

    def _ip(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _rate_limited(self, key: str, max_n: int = 10, window: float = 300) -> bool:
        now = time.time()
        attempts = [t for t in self._login_attempts.get(key, []) if now - t < window]
        self._login_attempts[key] = attempts
        return len(attempts) >= max_n

    # ---------- 路由 ----------

    def _setup_routes(self):
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse(PAGE)

        @app.get("/health")
        async def health():
            return {"status": "ok", "accounts": len(self.manager.workers)}

        @app.post("/api/login")
        async def login(request: Request):
            key = self._ip(request)
            if self._rate_limited(key, 10, 300):
                return JSONResponse(
                    {"error": "嘗試次數過多，請 5 分鐘後再試"}, status_code=429
                )
            data = await request.json()
            user = str(data.get("username", "")).strip()
            pwd = str(data.get("password", ""))
            if user != self.config.dashboard_user or pwd != self.config.dashboard_pass:
                self._login_attempts.setdefault(key, []).append(time.time())
                return JSONResponse({"error": "帳號或密碼錯誤"}, status_code=401)
            sid = hashlib.sha256(
                f"{user}{time.time()}".encode()
            ).hexdigest()
            self._sessions[sid] = time.time() + 3600
            resp = JSONResponse({"ok": True, "user": user})
            resp.set_cookie(
                "sdf_session", sid,
                max_age=3600, httponly=True, samesite="strict",
            )
            return resp

        @app.post("/api/logout")
        async def logout(request: Request):
            sid = request.cookies.get("sdf_session", "")
            self._sessions.pop(sid, None)
            resp = JSONResponse({"ok": True})
            resp.delete_cookie("sdf_session")
            return resp

        # ---------- 需登入的 API ----------

        @app.get("/api/status")
        async def status(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            await self.login_service.prune_expired()
            return JSONResponse(await self.manager.status())

        @app.post("/api/accounts/{account_id}/start")
        async def start_account(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            err = await self.manager.start(account_id)
            if err:
                return JSONResponse({"ok": False, "error": err}, status_code=400)
            return JSONResponse({"ok": True})

        @app.post("/api/accounts/{account_id}/stop")
        async def stop_account(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            await self.manager.stop(account_id)
            return JSONResponse({"ok": True})

        @app.delete("/api/accounts/{account_id}")
        async def delete_account(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            await self.manager.delete(account_id)
            return JSONResponse({"ok": True})

        @app.get("/api/accounts/{account_id}/persona")
        async def get_persona(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            import json
            acc = await self.manager.db.get_account(account_id)
            if not acc:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            try:
                persona = json.loads(acc["persona"] or "{}")
            except Exception:
                persona = {}
            return JSONResponse({"persona": persona})

        @app.post("/api/accounts/{account_id}/persona/regenerate")
        async def regen_persona(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            persona = await self.manager.regen_persona(account_id)
            if persona is None:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            return JSONResponse({"ok": True, "persona": persona})

        @app.post("/api/accounts/{account_id}/persona")
        async def update_persona(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            persona = data.get("persona") if isinstance(data, dict) else None
            if not isinstance(persona, dict) or not str(persona.get("name", "")).strip():
                return JSONResponse({"error": "人設格式錯誤（缺少名字）"}, status_code=400)
            # 只保留允許的欄位，避免注入未知欄位
            allowed = {
                "name", "gender", "age", "city", "district", "industry",
                "university", "personality", "hobbies", "looking_for",
                "meetups_done", "schedule",
            }
            clean = {k: v for k, v in persona.items() if k in allowed}
            clean["name"] = str(clean["name"]).strip()
            if not isinstance(clean.get("hobbies"), list):
                clean["hobbies"] = []
            saved = await self.manager.update_persona(account_id, clean)
            if saved is None:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            return JSONResponse({"ok": True, "persona": saved})

        @app.post("/api/accounts/{account_id}/groups")
        async def save_groups(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            raw = data.get("groups") if isinstance(data, dict) else None
            if raw is None:
                raw = []
            if not isinstance(raw, list):
                return JSONResponse({"error": "群組格式錯誤"}, status_code=400)
            try:
                ids = [int(g) for g in raw]
            except (TypeError, ValueError):
                return JSONResponse({"error": "群組格式錯誤"}, status_code=400)
            err = await self.manager.save_groups(account_id, ids)
            if err:
                return JSONResponse({"error": err}, status_code=404)
            return JSONResponse({"ok": True, "groups": ids})

        @app.get("/api/accounts/{account_id}/privates")
        async def private_messages(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            msgs = await self.manager.db.get_private_messages(account_id, limit=50)
            return JSONResponse({"messages": msgs})

        @app.post("/api/accounts/{account_id}/privates/{msg_id}/read")
        async def mark_read(account_id: str, msg_id: int, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            await self.manager.db.mark_private_message_read(msg_id)
            return JSONResponse({"ok": True})

        # ---------- TG 登入流程（新增帳號） ----------

        @app.post("/api/tglogin/start")
        async def tglogin_start(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            try:
                return JSONResponse(
                    await self.login_service.start(data.get("phone"))
                )
            except LoginRateLimit as e:
                return JSONResponse({"error": str(e)}, status_code=429)
            except (LoginExpired, ValueError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        @app.post("/api/tglogin/code")
        async def tglogin_code(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            try:
                return JSONResponse(
                    await self.login_service.submit_code(
                        data.get("auth_id"), data.get("code")
                    )
                )
            except LoginRateLimit as e:
                return JSONResponse({"error": str(e)}, status_code=429)
            except (LoginExpired, LoginConflict, ValueError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        @app.post("/api/tglogin/password")
        async def tglogin_password(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            try:
                return JSONResponse(
                    await self.login_service.submit_password(
                        data.get("auth_id"), data.get("password")
                    )
                )
            except LoginRateLimit as e:
                return JSONResponse({"error": str(e)}, status_code=429)
            except (LoginExpired, LoginConflict, ValueError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        @app.post("/api/accounts/add")
        async def add_account(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            auth_id = str(data.get("auth_id", "")).strip()
            name = str(data.get("name", "")).strip() or "水軍帳號"
            try:
                verified = await self.login_service.claim(auth_id)
            except (LoginExpired, LoginConflict, ValueError) as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            account = await self.manager.add_account(
                name, verified.session_string, enable=False
            )
            await self.manager.db.update_account(
                account["id"],
                tg_user_id=verified.tg_user_id,
                tg_username="",
                enabled=0,
            )
            err = await self.manager.start(account["id"])
            return JSONResponse({"ok": not err, "account": account, "error": err})


PAGE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SDF 控制台</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #0f172a; color: #e2e8f0; min-height: 100vh;
}
.container { max-width: 1100px; margin: 0 auto; padding: 2rem 1.5rem; }
header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }
h1 { font-size: 1.3rem; color: #38bdf8; }
.login { max-width: 380px; margin: 120px auto; padding: 2rem; background: #1e293b; border-radius: 12px; }
.login input { width: 100%; padding: 0.7rem; margin: 0.5rem 0; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; }
.login button { width: 100%; padding: 0.7rem; margin-top: 1rem; background: #38bdf8; border: none; border-radius: 8px; color: #0f172a; font-weight: bold; cursor: pointer; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
.stat-card { background: #1e293b; padding: 1.2rem; border-radius: 12px; text-align: center; }
.stat-card .value { font-size: 1.8rem; font-weight: bold; color: #38bdf8; }
.stat-card .label { color: #94a3b8; margin-top: 0.3rem; font-size: 0.85rem; }
.card { background: #1e293b; padding: 1.2rem; border-radius: 12px; margin-bottom: 1rem; }
.card h3 { margin-bottom: 0.5rem; font-size: 1rem; }
.meta { color: #94a3b8; font-size: 0.85rem; line-height: 1.6; }
.row { display: flex; justify-content: space-between; align-items: center; gap: 1rem; flex-wrap: wrap; }
.status-badge { display: inline-block; padding: 0.2rem 0.7rem; border-radius: 999px; font-size: 0.75rem; font-weight: bold; }
.status-badge.running { background: #16a34a; color: #fff; }
.status-badge.stopped { background: #475569; color: #cbd5e1; }
.status-badge.error { background: #dc2626; color: #fff; }
.btn { padding: 0.45rem 0.9rem; border: none; border-radius: 6px; cursor: pointer; font-size: 0.85rem; margin-left: 0.4rem; }
.btn-primary { background: #38bdf8; color: #0f172a; }
.btn-danger { background: #dc2626; color: #fff; }
.btn-secondary { background: #475569; color: #e2e8f0; }
.modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10; align-items: center; justify-content: center; }
.modal.active { display: flex; }
.modal-box { background: #1e293b; border-radius: 12px; padding: 1.5rem; width: 90%; max-width: 460px; max-height: 80vh; overflow-y: auto; }
.modal-box h3 { margin-bottom: 1rem; }
.modal input { width: 100%; padding: 0.6rem; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; margin: 0.3rem 0; }
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #334155; padding: 0.6rem 1.2rem; border-radius: 8px; font-size: 0.85rem; display: none; z-index: 20; }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>💬 SDF 水軍控制台</h1>
        <button class="btn btn-secondary" id="logoutBtn" style="display:none" onclick="doLogout()">登出</button>
    </header>

    <div class="login" id="loginBox">
        <h3 style="margin-bottom:0.8rem">登入控制台</h3>
        <input type="text" id="username" placeholder="帳號">
        <input type="password" id="password" placeholder="密碼">
        <button onclick="doLogin()">登入</button>
    </div>

    <div id="mainBox" style="display:none">
        <div class="stats" id="stats"></div>
        <div style="margin-bottom:1rem">
            <button class="btn btn-primary" onclick="openAddModal()">＋ 新增水軍帳號</button>
        </div>
        <div id="accounts"></div>
    </div>
</div>

<!-- 新增帳號 -->
<div class="modal" id="addModal">
    <div class="modal-box">
        <h3>新增水軍帳號</h3>
        <div id="tgStep1">
            <p class="meta">輸入水軍帳號的手機號碼（含國碼），會傳驗證碼到該帳號</p>
            <input type="text" id="tgPhone" placeholder="+886912345678">
            <button class="btn btn-primary" onclick="tgStart()">傳送驗證碼</button>
        </div>
        <div id="tgStep2" style="display:none">
            <p class="meta">驗證碼已傳送至 <span id="tgHint"></span></p>
            <input type="text" id="tgCode" placeholder="驗證碼">
            <button class="btn btn-primary" onclick="tgSubmitCode()">確認驗證碼</button>
        </div>
        <div id="tgStep3" style="display:none">
            <p class="meta">該帳號啟用了兩步驗證，請輸入密碼</p>
            <input type="password" id="tgPassword" placeholder="兩步驗證密碼">
            <button class="btn btn-primary" onclick="tgSubmitPassword()">確認密碼</button>
        </div>
        <div id="tgStep4" style="display:none">
            <p class="meta">登入成功！設定帳號名稱（可留空用預設）</p>
            <input type="text" id="accName" placeholder="帳號名稱（例：台北-美玲）">
            <button class="btn btn-primary" onclick="tgAddAccount()">建立帳號並啟動</button>
        </div>
        <div id="tgErr" class="meta" style="color:#f87171"></div>
        <button class="btn btn-secondary" style="margin-top:1rem" onclick="closeModals()">關閉</button>
    </div>
</div>

<!-- 人設（可編輯，含性格） -->
<div class="modal" id="personaModal">
    <div class="modal-box" style="max-width:560px">
        <h3>人設設定（可直接修改）</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem 1rem">
            <label class="meta">名字<input id="pf_name" type="text"></label>
            <label class="meta">性別
                <select id="pf_gender"><option>女</option><option>男</option></select>
            </label>
            <label class="meta">年齡<input id="pf_age" type="number" min="18" max="60"></label>
            <label class="meta">城市<input id="pf_city" type="text" placeholder="台北"></label>
            <label class="meta">地區<input id="pf_district" type="text" placeholder="大安"></label>
            <label class="meta">行業<input id="pf_industry" type="text" placeholder="科技業"></label>
            <label class="meta">學歷<input id="pf_university" type="text" placeholder="政大"></label>
            <label class="meta">作息
                <select id="pf_schedule"><option>正常</option><option>夜貓</option><option>早起</option></select>
            </label>
            <label class="meta" style="grid-column:1 / span 2">性格（可自由輸入）<input id="pf_personality" type="text" placeholder="活潑開朗、愛交朋友"></label>
            <label class="meta" style="grid-column:1 / span 2">興趣愛好（用、分隔）<input id="pf_hobbies" type="text" placeholder="看電影、吃美食"></label>
            <label class="meta" style="grid-column:1 / span 2">想找什麼（求偶目標）<input id="pf_looking" type="text"></label>
            <label class="meta">約炮成約次數（社會證明）<input id="pf_meetups" type="number" min="0" max="99"></label>
        </div>
        <div style="margin-top:1rem">
            <button class="btn btn-primary" onclick="savePersona()">儲存人設</button>
            <button class="btn btn-secondary" onclick="regenPersona()">重新生成（換一個）</button>
            <button class="btn btn-secondary" onclick="closeModals()">關閉</button>
        </div>
    </div>
</div>

<!-- 指定群組 -->
<div class="modal" id="groupsModal">
    <div class="modal-box" style="max-width:520px">
        <h3>指定群組（只讓此水軍在勾選的群活動）</h3>
        <p class="meta" style="margin-bottom:0.8rem">不勾任何群 = 自動在所有群活動。需帳號已連線才會列出它所在的群。</p>
        <div id="groupsList" style="max-height:45vh;overflow-y:auto"></div>
        <div style="margin-top:1rem">
            <button class="btn btn-primary" onclick="saveGroups()">儲存指定群組</button>
            <button class="btn btn-secondary" onclick="closeModals()">關閉</button>
        </div>
    </div>
</div>

<!-- 私訊 -->
<div class="modal" id="privModal">
    <div class="modal-box">
        <h3>收到的私訊</h3>
        <div id="privList" style="max-height:50vh;overflow-y:auto"></div>
        <button class="btn btn-secondary" style="margin-top:1rem" onclick="closeModals()">關閉</button>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let tgAuthId = '';
let currentPersonaId = '';
let currentGroupsId = '';

function toast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 3000);
}

async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: 'same-origin', ...opts });
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (res.status === 401) { showLogin(); toast(data.error || '請重新登入'); throw new Error('401'); }
    return { ok: res.ok, data };
}

function showLogin() {
    document.getElementById('loginBox').style.display = 'block';
    document.getElementById('mainBox').style.display = 'none';
    document.getElementById('logoutBtn').style.display = 'none';
}

async function doLogin() {
    const { ok, data } = await api('/api/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            username: document.getElementById('username').value,
            password: document.getElementById('password').value,
        }),
    });
    if (ok) {
        document.getElementById('loginBox').style.display = 'none';
        document.getElementById('mainBox').style.display = 'block';
        document.getElementById('logoutBtn').style.display = 'block';
        loadStatus();
    } else { toast(data.error || '登入失敗'); }
}

async function doLogout() {
    await api('/api/logout', { method: 'POST' });
    showLogin();
}

async function loadStatus() {
    const { ok, data } = await api('/api/status');
    if (!ok) return;
    document.getElementById('stats').innerHTML = `
        <div class="stat-card"><div class="value">${data.total}</div><div class="label">總帳號數</div></div>
        <div class="stat-card"><div class="value">${data.running}</div><div class="label">運行中</div></div>
    `;
    document.getElementById('accounts').innerHTML = data.accounts.map(acc => {
        const persona = safeParse(acc.persona);
        const city = persona.city || '未設定';
        const stateCls = acc.is_running ? 'running' : (acc.state === 'disconnected' ? 'error' : 'stopped');
        const stateTxt = acc.is_running ? '運行中' : (acc.state === 'disconnected' ? '連線失敗' : '已停止');
        return `
        <div class="card">
            <div class="row">
                <div>
                    <h3>${esc(acc.name)} <span class="status-badge ${stateCls}">${stateTxt}</span></h3>
                    <div class="meta">
                        ${persona.name || ''}・${persona.gender || '?'}生・${persona.age || '?'}歲・${city}（${persona.district || ''}）・${persona.industry || ''}
                        <br>回覆 ${acc.stats.replies_sent}｜主動 ${acc.stats.proactive_sent}｜錯誤 ${acc.stats.errors}
                        ${acc.detail ? '<br style="color:#f87171">' + esc(acc.detail) : ''}
                    </div>
                </div>
                <div>
                    <button class="btn ${acc.is_running ? 'btn-danger' : 'btn-primary'}" data-act="${acc.is_running ? 'stop' : 'start'}" data-id="${esc(acc.id)}">${acc.is_running ? '停止' : '啟動'}</button>
                    <button class="btn btn-secondary" data-act="persona" data-id="${esc(acc.id)}">人設</button>
                    <button class="btn btn-secondary" data-act="groups" data-id="${esc(acc.id)}">群組${(acc.groups && acc.groups.length) ? '·' + acc.groups.length : ''}</button>
                    <button class="btn btn-secondary" data-act="privates" data-id="${esc(acc.id)}">私訊</button>
                    <button class="btn btn-danger" data-act="delete" data-id="${esc(acc.id)}">刪除</button>
                </div>
            </div>
        </div>`;
    }).join('') || '<div class="card meta">還沒有水軍帳號，先新增一個吧</div>';
}

function safeParse(s) { try { return JSON.parse(s) || {}; } catch (e) { return {}; } }
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function startAccount(id) {
    const r = await api('/api/accounts/' + id + '/start', { method: 'POST' });
    if (!r.ok) toast(r.data.error || '啟動失敗');
    loadStatus();
}
async function stopAccount(id) { await api('/api/accounts/' + id + '/stop', { method: 'POST' }); loadStatus(); }
async function deleteAccount(id) {
    if (!confirm('確定刪除此帳號？（會一併刪除記憶資料）')) return;
    await api('/api/accounts/' + id, { method: 'DELETE' });
    loadStatus();
}

function openAddModal() {
    document.getElementById('addModal').classList.add('active');
    ['tgStep1','tgStep2','tgStep3','tgStep4'].forEach(id => document.getElementById(id).style.display = 'none');
    document.getElementById('tgStep1').style.display = 'block';
    document.getElementById('tgErr').textContent = '';
}
function closeModals() { document.querySelectorAll('.modal').forEach(m => m.classList.remove('active')); }

async function tgStart() {
    const r = await api('/api/tglogin/start', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ phone: document.getElementById('tgPhone').value }),
    });
    if (!r.ok) { document.getElementById('tgErr').textContent = r.data.error; return; }
    tgAuthId = r.data.auth_id;
    document.getElementById('tgHint').textContent = r.data.phone_hint;
    ['tgStep1','tgStep4'].forEach(id => document.getElementById(id).style.display = 'none');
    document.getElementById('tgStep2').style.display = 'block';
}
async function tgSubmitCode() {
    const r = await api('/api/tglogin/code', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ auth_id: tgAuthId, code: document.getElementById('tgCode').value }),
    });
    if (!r.ok) { document.getElementById('tgErr').textContent = r.data.error; return; }
    if (r.data.status === 'password_required') {
        document.getElementById('tgStep2').style.display = 'none';
        document.getElementById('tgStep3').style.display = 'block';
    } else if (r.data.status === 'authorized') {
        ['tgStep2','tgStep3'].forEach(id => document.getElementById(id).style.display = 'none');
        document.getElementById('tgStep4').style.display = 'block';
    }
}
async function tgSubmitPassword() {
    const r = await api('/api/tglogin/password', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ auth_id: tgAuthId, password: document.getElementById('tgPassword').value }),
    });
    if (!r.ok) { document.getElementById('tgErr').textContent = r.data.error; return; }
    if (r.data.status === 'authorized') {
        ['tgStep2','tgStep3'].forEach(id => document.getElementById(id).style.display = 'none');
        document.getElementById('tgStep4').style.display = 'block';
    }
}
async function tgAddAccount() {
    const r = await api('/api/accounts/add', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ auth_id: tgAuthId, name: document.getElementById('accName').value }),
    });
    if (!r.ok) { document.getElementById('tgErr').textContent = r.data.error || '建立失敗'; return; }
    closeModals();
    loadStatus();
    toast('帳號已建立並啟動');
}

async function showPersona(id) {
    currentPersonaId = id;
    const r = await api('/api/accounts/' + id + '/persona');
    if (!r.ok) return;
    fillPersonaForm(r.data.persona || {});
    document.getElementById('personaModal').classList.add('active');
}
function fillPersonaForm(p) {
    const v = (id, key) => { const el = document.getElementById(id); if (el) el.value = (p[key] !== undefined && p[key] !== null) ? p[key] : ''; };
    v('pf_name', 'name'); v('pf_city', 'city'); v('pf_district', 'district');
    v('pf_industry', 'industry'); v('pf_university', 'university');
    v('pf_personality', 'personality'); v('pf_looking', 'looking_for');
    v('pf_age', 'age'); v('pf_meetups', 'meetups_done');
    const g = document.getElementById('pf_gender'); if (g) g.value = p.gender || '女';
    const s = document.getElementById('pf_schedule'); if (s) s.value = p.schedule || '正常';
    const h = document.getElementById('pf_hobbies'); if (h) h.value = (p.hobbies || []).join('、');
}
function readPersonaForm() {
    const g = id => document.getElementById(id).value.trim();
    return {
        name: g('pf_name'),
        gender: g('pf_gender'),
        age: parseInt(g('pf_age') || '0', 10),
        city: g('pf_city'),
        district: g('pf_district'),
        industry: g('pf_industry'),
        university: g('pf_university'),
        personality: g('pf_personality'),
        hobbies: g('pf_hobbies').split(/[,，、]/).map(x => x.trim()).filter(Boolean),
        looking_for: g('pf_looking'),
        meetups_done: parseInt(g('pf_meetups') || '0', 10),
        schedule: g('pf_schedule'),
    };
}
async function savePersona() {
    const persona = readPersonaForm();
    if (!persona.name) { toast('名字不能為空'); return; }
    const r = await api('/api/accounts/' + currentPersonaId + '/persona', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ persona }),
    });
    if (r.ok) { toast('人設已儲存'); closeModals(); loadStatus(); }
    else toast(r.data.error || '儲存失敗');
}
async function regenPersona() {
    const r = await api('/api/accounts/' + currentPersonaId + '/persona/regenerate', { method: 'POST' });
    if (r.ok) fillPersonaForm(r.data.persona);
}

async function showGroups(id) {
    currentGroupsId = id;
    const r = await api('/api/status');
    if (!r.ok) return;
    const acc = (r.data.accounts || []).find(a => a.id === id);
    if (!acc) return;
    const selected = new Set(acc.groups || []);
    const avail = acc.groups_available || [];
    document.getElementById('groupsList').innerHTML = avail.length
        ? avail.map(g => `
            <label style="display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0;font-size:0.9rem">
                <input type="checkbox" class="grp-cb" value="${g.id}" ${selected.has(g.id) ? 'checked' : ''}>
                <span>${esc(g.title)}</span> <span class="meta">（${g.id}）</span>
            </label>`).join('')
        : '<div class="meta">此帳號尚未連線，看不到它所在的群。先啟動帳號，再回來勾選。</div>';
    document.getElementById('groupsModal').classList.add('active');
}
async function saveGroups() {
    const ids = [...document.querySelectorAll('#groupsList .grp-cb:checked')].map(c => Number(c.value));
    const r = await api('/api/accounts/' + currentGroupsId + '/groups', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ groups: ids }),
    });
    if (r.ok) { closeModals(); toast('指定群組已儲存'); loadStatus(); }
    else toast(r.data.error || '儲存失敗');
}

async function showPrivates(id) {
    const r = await api('/api/accounts/' + id + '/privates');
    if (!r.ok) return;
    document.getElementById('privList').innerHTML = (r.data.messages || []).map(m =>
        `<div class="card" style="margin-bottom:0.5rem;padding:0.8rem">
            <div class="meta"><b>${esc(m.sender_name)}</b>（${new Date(m.timestamp * 1000).toLocaleString('zh-TW')}）${m.read ? '' : ' 🔴未讀'}</div>
            <div style="font-size:0.9rem">${esc(m.preview)}</div>
        </div>`
    ).join('') || '<div class="meta">沒有私訊紀錄</div>';
    document.getElementById('privModal').classList.add('active');
}

// 自動載入 + 自動刷新
(async () => {
    const r = await api('/api/status').catch(() => null);
    if (r && r.ok) {
        document.getElementById('loginBox').style.display = 'none';
        document.getElementById('mainBox').style.display = 'block';
        document.getElementById('logoutBtn').style.display = 'block';
        // 帳號卡片按鈕（事件委託，免手動綁定）
        document.getElementById('accounts').addEventListener('click', (e) => {
            const b = e.target.closest('button[data-act]');
            if (!b) return;
            const act = b.dataset.act, id = b.dataset.id;
            if (act === 'start') startAccount(id);
            else if (act === 'stop') stopAccount(id);
            else if (act === 'persona') showPersona(id);
            else if (act === 'groups') showGroups(id);
            else if (act === 'privates') showPrivates(id);
            else if (act === 'delete') deleteAccount(id);
        });
        loadStatus();
        setInterval(() => { if (document.getElementById('mainBox').style.display !== 'none') loadStatus(); }, 15000);
    }
})();
</script>
</body>
</html>
"""
