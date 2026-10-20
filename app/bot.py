"""
Telegram Bot 橋接 - 把控制台常用功能接進 Telegram Bot（遠端查看）。

用 BotFather 的 bot token（BOT_TOKEN 環境變數），長輪詢 getUpdates；
bot 和水軍帳號完全獨立（bot 不是 user account，不會進群、不占 session）。

指令：
  /status              運行狀態 + 24h KPI
  /groups              群組清單（依活動排序，前 8 個）
  /groups <id> [n]     某群最近 n 則訊息（預設 20）
  /privates <帳號> [n] 某水軍帳號收到的私訊（預設 15）
  /start /help         說明

權限：BOT_ADMIN_IDS（逗號分隔的 TG user id）。留空＝全部放行（會打警告）。
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass

from telethon import TelegramClient, events, types
from telethon.tl.functions.bots import SetBotCommandsRequest
from fastapi import Request

from .config import Settings
from .manager import AccountManager
from .persona import get_system_prompt

log = logging.getLogger("sdf.bot")

MSG_LIMIT = 4096

# 按鍵式菜單：(按鈕文字, 對應指令)；指令為空＝隱藏鍵盤
MENU: list[tuple[str, str]] = [
    ("📊 狀態", "status"),
    ("🗂 群組", "groups"),
    ("👤 帳號", "acct"),
    ("▶ 啟動", "startacc"),
    ("⏹ 停止", "stopacc"),
    ("🗑 刪除", "deleteacc"),
    ("🖼 媒體", "media"),
    ("📷 圖片生成", "imagegen"),
    ("🔊 語音", "voice"),
    ("➕ 新增", "addacc"),
    ("📖 說明", "help"),
    ("⌨ 隱藏", ""),
]
MENU_LABELS = {label: cmd for label, cmd in MENU}
MENU_ROWS = [MENU[0:3], MENU[3:6], MENU[6:9], MENU[9:12]]

# ---------- Mini App 單頁（新增帳號：手機→驗證碼→兩步密碼→名稱） ----------
# token 由 ?t=... 帶入；所有 API 呼叫都附上同一個 t。
_MINI_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>新增水軍帳號</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--fg:#e6e8ee;--mut:#8b93a3;--acc:#4f8cff;--ok:#39c07f;--err:#ff5c5c}
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;
       background:var(--bg);color:var(--fg);min-height:100vh;padding:16px 14px 40px}
  h1{font-size:18px;margin:6px 0 14px;font-weight:600}
  .card{background:var(--card);border-radius:14px;padding:16px;margin-bottom:14px}
  label{display:block;font-size:13px;color:var(--mut);margin:10px 0 6px}
  input{width:100%;background:#0c0e12;border:1px solid #2a2f3a;border-radius:10px;
        color:var(--fg);font-size:16px;padding:12px 12px;outline:none}
  input:focus{border-color:var(--acc)}
  button{width:100%;margin-top:14px;background:var(--acc);border:0;border-radius:10px;
         color:#fff;font-size:15px;font-weight:600;padding:13px;cursor:pointer}
  button:disabled{opacity:.45;cursor:default}
  .step{display:none}
  .step.on{display:block}
  .msg{margin-top:12px;font-size:14px;line-height:1.5;white-space:pre-wrap}
  .msg.ok{color:var(--ok)}
  .msg.err{color:var(--err)}
  .hint{font-size:12px;color:var(--mut);margin-top:8px;line-height:1.5}
</style>
</head>
<body>
<h1>➕ 新增水軍帳號</h1>

<div class="card step on" id="s1">
  <label>手機號碼（含國碼）</label>
  <input id="phone" type="tel" placeholder="+886912345678" autocomplete="off">
  <button id="b1" onclick="doStart()">傳送驗證碼</button>
  <div class="hint">驗證碼會傳到該手機的 Telegram（5 分鐘內有效）</div>
  <div class="msg" id="m1"></div>
</div>

<div class="card step" id="s2">
  <label>驗證碼</label>
  <input id="code" type="text" inputmode="numeric" placeholder="12345" autocomplete="off">
  <button id="b2" onclick="doCode()">送出驗證碼</button>
  <div class="msg" id="m2"></div>
</div>

<div class="card step" id="s3">
  <label>兩步驗證密碼（如需要）</label>
  <input id="pass" type="password" placeholder="cloud password" autocomplete="new-password">
  <button id="b3" onclick="doPass()">送出密碼</button>
  <div class="msg" id="m3"></div>
</div>

<div class="card step" id="s4">
  <label>帳號名稱（內部辨識用）</label>
  <input id="name" type="text" placeholder="台北-美玲" autocomplete="off">
  <button id="b4" onclick="doClaim()">建立帳號（暫不啟動）</button>
  <div class="msg" id="m4"></div>
</div>

<script>
const params = new URLSearchParams(location.search);
const T = params.get('t') || '';
const base = location.pathname.replace(/\\/tg\\/addacc.*/, '');
const API = base + '/tgapi/';

function show(n){
  for(let i=1;i<=4;i++){document.getElementById('s'+i).classList.toggle('on', i===n);}
}
function setMsg(id, txt, cls){
  const el = document.getElementById(id);
  el.textContent = txt || '';
  el.className = 'msg' + (cls ? ' ' + cls : '');
}
async function post(path, body){
  const r = await fetch(API + path + (T ? '?t='+encodeURIComponent(T) : ''), {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  let j = {};
  try{ j = await r.json(); }catch(e){}
  if(!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}
function busy(id, on){document.getElementById(id).disabled = on;}
// #2：身分優先用 Telegram initData（HMAC 簽章）；沒有才退回 token 綁定的 admin/chat
function ident(){
  const o = {};
  try{
    if(window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData){
      o.init_data = window.Telegram.WebApp.initData;
    }
  }catch(e){}
  return o;
}

async function doStart(){
  const phone = document.getElementById('phone').value.trim();
  if(!phone){setMsg('m1','請輸入手機號碼','err');return;}
  busy('b1', true); setMsg('m1','傳送中…');
  try{
    const r = await post('start', Object.assign({phone}, ident()));
    setMsg('m1', '✅ 驗證碼已傳到 '+(r.phone_hint||phone)+'，請輸入驗證碼', 'ok');
    show(2);
  }catch(e){ setMsg('m1', '⚠️ '+e.message, 'err'); }
  finally{ busy('b1', false); }
}
async function doCode(){
  const code = document.getElementById('code').value.trim();
  if(!code){setMsg('m2','請輸入驗證碼','err');return;}
  busy('b2', true); setMsg('m2','驗證中…');
  try{
    const r = await post('code', Object.assign({code}, ident()));
    if(r.need_password){
      setMsg('m2','需要兩步驗證密碼','ok'); show(3);
    }else{
      setMsg('m2','✅ 驗證成功！請輸入帳號名稱','ok'); show(4);
    }
  }catch(e){ setMsg('m2','⚠️ '+e.message, 'err'); }
  finally{ busy('b2', false); }
}
async function doPass(){
  const pwEl = document.getElementById('pass');
  const password = pwEl.value.trim();
  if(!password){setMsg('m3','請輸入密碼','err');return;}
  busy('b3', true); setMsg('m3','驗證中…');
  pwEl.value = ''; // #5：處理後清掉欄位（不留在頁面上）
  try{
    const r = await post('pass', Object.assign({password}, ident()));
    if(r.ok){
      setMsg('m3','✅ 驗證成功！請輸入帳號名稱','ok'); show(4);
    }else{ setMsg('m3','狀態：'+(r.error||'失敗'), 'err'); }
  }catch(e){ setMsg('m3', '⚠️ '+e.message, 'err'); }
  finally{ busy('b3', false); }
}
async function doClaim(){
  const name = document.getElementById('name').value.trim() || '水軍帳號';
  busy('b4', true); setMsg('m4','建立中…');
  try{
    const r = await post('claim', Object.assign({name}, ident()));
    setMsg('m4', '✅ 已建立 '+(r.account && r.account.name || name)
      + (r.tg_name ? '（'+r.tg_name+'）' : '') + '\\n先設人設與群組，再回 bot 啟動上線。', 'ok');
  }catch(e){ setMsg('m4','⚠️ '+e.message, 'err'); }
  finally{ busy('b4', false); }
}
// Telegram WebApp 主題色（若存在）
if(window.Telegram && window.Telegram.WebApp){
  window.Telegram.WebApp.ready();
  window.Telegram.WebApp.expand();
}
</script>
</body>
</html>
"""


def cb(data: str, text: str) -> "types.KeyboardButtonCallback":
    """內嵌按鈕：telethon 1.44 簽名 KeyboardButtonCallback(text, data:bytes)。"""
    return types.KeyboardButtonCallback(text, data.encode())


def kbrows(rows) -> list:
    """telethon 1.44 的 markup rows 必須是 KeyboardButtonRow 物件（純 list 會
    在序列化時炸 'list' object has no attribute '_bytes'）。"""
    return [types.KeyboardButtonRow(row) for row in rows]


async def notrack_balance(key: str, base_url: str = "https://api.notrack.ai/v1",
                         timeout: float = 20.0) -> float | None:
    """讀 NoTrack API 的剩餘額度（美元）。

    NoTrack 把餘額放在每個回應的 `x-notrack-balance-usd` header，沒有獨立的
    /balance 端點——所以發一個最小 chat 請求（1 token）讀 header。失敗回 None
    （不影響用量頁其餘內容）。key 是 NoTrack 的 sk-notrack-… 格式才有效。
    用 with_raw_response 拿到原始 httpx Response（openai 2.x 的 ChatCompletion
    物件本身不帶 headers）。
    """
    if not key:
        return None
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=base_url.rstrip("/"), api_key=key,
                             timeout=timeout)
        try:
            raw = await client.with_raw_response.chat.completions.create(
                model="notrack-uncensored",
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
            )
        finally:
            await client.close()
        headers = getattr(raw, "headers", None)
        if headers is None:
            return None
        for k, v in headers.items():
            if k.lower() == "x-notrack-balance-usd":
                return float(v)
        return None
    except Exception:
        return None

# 打 / 時的指令下拉（setMyCommands）
BOT_COMMANDS = [
    types.BotCommand("status", "運行狀態＋24h KPI"),
    types.BotCommand("groups", "群組清單／群訊息"),
    types.BotCommand("privates", "私訊"),
    types.BotCommand("acct", "帳號清單"),
    types.BotCommand("startacc", "啟動帳號"),
    types.BotCommand("stopacc", "停止帳號"),
    types.BotCommand("deleteacc", "刪除帳號"),
    types.BotCommand("media", "媒體識別開關（看懂圖/影片，on/off）"),
    types.BotCommand("imagegen", "圖片生成開關（被要求時發圖/影片，on/off）"),
    types.BotCommand("voice", "語音開關（on/off）"),
    types.BotCommand("send", "指定水軍發訊息：/send <角色/帳號> [群ID] <內容>"),
    types.BotCommand("addacc", "新增帳號（TG 驗證碼流程）"),
    types.BotCommand("help", "說明"),
]


def _fmt_ts(ts: float) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(float(ts), datetime.UTC).astimezone(
            datetime.timezone(datetime.timedelta(hours=8))
        )
    except Exception:
        dt = datetime.datetime.fromtimestamp(float(ts))
    return dt.strftime("%m-%d %H:%M")


@dataclass
class _MiniFlow:
    """Mini App（新增帳號）的臨時流程。

    綁定：簽發時的管理員 sender_id、聊天 chat_id、auth_id（start 後填入）、
    階段 stage（page/code/pass/claim）、期限 expires_at、是否已用 used。
    任一 HTTP 操作都要驗證 token 仍存在、未過期、未用，且 管理員／聊天 對得上，
    才准執行對應階段的 login_service 呼叫——禁止跨流程、跨 token 操作。
    """

    admin_id: str
    chat_id: int
    auth_id: str = ""
    stage: str = "page"
    expires_at: float = 0.0
    used: bool = False


def _tg_initdata_identity(
    init_data: str, bot_token: str, max_age: int = 600
) -> tuple[str | None, int | None]:
    """驗證 Telegram WebApp initData（HMAC-SHA256＋auth_date 新鮮度）。

    回傳 (user_id, chat_id)：
    - user_id＝發送者 user.id（可信身分）；
    - chat_id＝僅當 user JSON 自帶 chat_id 時才有（TG 標準 initData 通常沒有，
      此時 None＝不強求聊天綁定，改靠 token 簽發時的 callback 上下文）。
    缺失／簽章失敗／無 user／auth_date 過舊（>max_age 秒）都回 (None, None)。

    不能只相信客戶端傳的 user id：initData 是 key=value& 串，
    secret = HMAC_SHA256("WebAppData", bot_token)，再對
    HMAC_SHA256(secret, sorted_data_check_string) 驗簽。
    """
    if not init_data:
        return None, None
    try:
        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None, None
    if "hash" not in params:
        return None, None
    hash_v = params["hash"]
    check_items = {k: v for k, v in params.items() if k != "hash"}
    check_str = "\n".join(f"{k}={v}" for k, v in sorted(check_items.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, str(hash_v)):
        return None, None
    user_raw = check_items.get("user")
    if not user_raw:
        return None, None
    try:
        user = json.loads(user_raw)
    except Exception:
        return None, None
    uid = user.get("id")
    if uid is None:
        return None, None
    # auth_date 新鮮度：必須存在且不能過舊（防重放；缺少 auth_date 一律拒絕）
    auth_date = check_items.get("auth_date")
    if not auth_date:
        return None, None
    try:
        if abs(time.time() - int(auth_date)) > max_age:
            return None, None
    except (TypeError, ValueError):
        return None, None
    chat_id: int | None = None
    if isinstance(user.get("chat_id"), (int, str)):
        try:
            chat_id = int(user["chat_id"])
        except (TypeError, ValueError):
            chat_id = None
    return str(uid), chat_id


class TgControlBot:
    """輕量橋接：讀 DB 資料＋控制水軍帳號（啟動/停止/刪除/功能開關/新增）。"""

    def __init__(self, settings: Settings, manager: AccountManager,
                 login_service=None):
        self.settings = settings
        self.manager = manager
        self.login_service = login_service
        self.bot_token = settings.bot_token
        self.admin_ids = tuple(str(x) for x in settings.bot_admin_ids if str(x))
        self._client: TelegramClient | None = None
        self._bot_username = ""
        # 群組勾選的暫存選中狀態（callback 無狀態，用記憶體暫存）。
        # 鍵＝(管理員, 聊天, 帳號)：避免兩個管理員同時勾同一帳號互相覆蓋。
        self._group_selections: dict[tuple[str, int, str], set[int]] = {}
        # Mini App（新增帳號）用的臨時流程：{token: _MiniFlow}
        # 每個 token 綁定 管理員／聊天／auth_id／階段／期限／使用狀態，
        # 禁止跨流程、跨 token、跨管理員、跨聊天操作。
        self._mini_flows: dict[str, _MiniFlow] = {}
        # 刪除確認：{token: (account_id, admin_id, chat_id, exp)}
        # 一次性、有期限、綁管理員／聊天／帳號；重複點不會二次刪除。
        self._del_confirms: dict[str, tuple[str, str, int, float]] = {}
        # #11：run() 重試／可觀測（/health 含 bot 狀態，防「health 3/3 但 bot 死」）
        self._stop_event: asyncio.Event | None = None
        self._last_error = ""
        if not self.admin_ids:
            log.warning("BOT_ADMIN_IDS 為空：fail-closed，任何 TG 使用者都會被擋")
        self.app = self._build_mini_app()

    # ---------- 生命週期 ----------

    async def run(self) -> None:
        # #11：斷線/啟動失敗自動重連（指數退避 3→60 秒）；stop() 或 task cancel 才結束。
        self._stop_event = asyncio.Event()
        backoff = 3
        while True:
            try:
                await self._run_once()
                self._last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                log.exception("bot 連線失敗（%ss 後重試）", backoff)
                print(f"⚠️ bot 連線失敗：{e}", flush=True)
            if self._stop_event.is_set():
                return
            print(f"bot：{backoff}s 後重連…", flush=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)

    async def _run_once(self) -> None:
        # 舊 session 檔可能帶上一代的 auth key／api_id，bot 用 token 重新授權即可，
        # 先清掉避免 client.start 卡住或炸 ApiIdInvalid。
        try:
            import os as _os

            if _os.path.exists("sdf_control_bot.session"):
                _os.remove("sdf_control_bot.session")
                print("bot：清掉舊 session 檔", flush=True)
        except Exception:
            pass
        try:
            client = TelegramClient(
                "sdf_control_bot", self.settings.tg_api_id, self.settings.tg_api_hash
            )
            self._client = client
            print("bot：client.start 連線中…", flush=True)
            await client.start(bot_token=self.bot_token)
            print("bot：start 完成", flush=True)
            me = await client.get_me()
            self._bot_username = str(getattr(me, "username", "") or "")
            # 打 / 時的指令下拉（telethon 1.44：SetBotCommandsRequest，scope 必填）
            try:
                await client(
                    SetBotCommandsRequest(
                        types.BotCommandScopeDefault(),
                        "",
                        [c for c in BOT_COMMANDS],
                    )
                )
            except Exception:
                log.exception("setBotCommands 失敗")
            self._reply_menu = types.ReplyKeyboardMarkup(
                [
                    types.KeyboardButtonRow([types.KeyboardButton(l) for l, _ in row])
                    for row in MENU_ROWS
                ],
                resize=True,
            )
            client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
            client.add_event_handler(self._on_callback, events.CallbackQuery)
            print(f"Telegram 控制台 bot 上線（@{self._bot_username}）", flush=True)
            log.info("Telegram 控制台 bot 上線（@%s）", self._bot_username)
            await client.run_until_disconnected()
        except Exception as e:
            # 丟回 run() 的重試迴圈（#11：斷線恢復＋last_error 可觀測）
            raise
        finally:
            try:
                if self._client:
                    await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def stop(self) -> None:
        """正常停止 run() 的重試迴圈（main shutdown 用）。"""
        if self._stop_event is not None:
            self._stop_event.set()

    def state(self) -> dict:
        """/health 用的 bot 健康狀態（#11：防 health 3/3 但 bot 無法操作）。"""
        client = self._client
        connected = False
        if client is not None:
            try:
                connected = bool(client.is_connected)
            except Exception:
                connected = False
        return {
            "connected": connected,
            "username": self._bot_username,
            "last_error": self._last_error,
        }

    # ---------- Mini App（新增帳號，免在聊天傳驗證碼/密碼） ----------

    _MINI_TOKEN_TTL = 1800  # 30 分鐘

    # claim 回應欄位白名單：禁止 session／密鑰／hash 等內部欄位外洩
    _ACCOUNT_PUBLIC_FIELDS = (
        "id", "name", "tg_username", "tg_name", "avatar", "status",
        "enabled", "persona", "reply_enabled", "proactive_enabled",
        "created_at", "updated_at",
    )

    def _mini_mint(self, admin_id: str, chat_id: int) -> str:
        """簽發一個綁定 管理員＋聊天 的新流程令牌。"""
        token = secrets.token_urlsafe(16)
        self._mini_flows[token] = _MiniFlow(
            admin_id=str(admin_id), chat_id=int(chat_id),
            expires_at=time.time() + self._MINI_TOKEN_TTL,
        )
        return token

    def _mini_flow(self, token: str, *, admin_id: str | None = None,
                   chat_id: int | None = None,
                   stage: str | None = None) -> _MiniFlow | None:
        """驗證並取出 token 對應的流程。

        條件：存在、未過期、未用、（若提供）管理員/聊天/階段都對得上。
        """
        flow = self._mini_flows.get(token)
        if flow is None:
            return None
        if not self.admin_ids or str(flow.admin_id) not in self.admin_ids:
            self._mini_flows.pop(token, None)
            return None
        if time.time() > flow.expires_at:
            self._mini_flows.pop(token, None)
            return None
        if flow.used:
            return None
        if admin_id is not None and str(flow.admin_id) != str(admin_id):
            return None
        if chat_id is not None and flow.chat_id != int(chat_id):
            return None
        if stage is not None and flow.stage != stage:
            return None
        return flow

    @staticmethod
    def _public_account(account: dict) -> dict:
        """只回白名單欄位，其餘（session_key 等）擋在外。"""
        return {k: account.get(k) for k in TgControlBot._ACCOUNT_PUBLIC_FIELDS
                if k in account}

    def _build_mini_app(self):
        """一組 HTTP 路由：/tg/addacc 開 Mini App，/tgapi/* 是它後端。

        回 APIRouter，由 main.py 掛到 dashboard.app（同一個 uvicorn 對外）。
        令牌走 query string（?t=...），綁定 管理員/聊天/auth_id/階段/期限/使用狀態；
        start 後另要求 initData（HMAC 驗證）或 admin_id+chat_id 綁定，防跨流程操作。
        """
        from fastapi import APIRouter, Request
        from fastapi.responses import HTMLResponse, JSONResponse

        router = APIRouter()

        def _tok(req: Request) -> str:
            return str(req.query_params.get("t", ""))

        async def _flow_for(req: Request, payload: dict,
                            stage: str) -> _MiniFlow | None:
            """fail-closed 身分＋流程驗證。

            可信身分只認 initData（HMAC 簽章＋auth_date 新鮮度）：
            - 缺 initData／簽章錯／user 缺／auth_date 過舊 → 直接拒（不回落
              客戶端自報 admin_id/chat_id 或 URL 的 admin/chat）。
            - initData 的 user.id 必須＝token 簽發的管理員；initData 若自帶
              chat_id 也要對得上（沒有 chat_id 不強求，靠 token 綁定）。
            - 再驗 stage／期限／一次性（_mini_flow）。
            """
            token = _tok(req)
            init_data = str(payload.get("init_data") or "")
            uid, id_chat = _tg_initdata_identity(init_data, self.bot_token)
            if uid is None:
                return None  # 無可信身分（含缺 initData、簽章錯、過舊）
            # initData 若自帶 chat_id：必須對得上 token 綁定的聊天
            if id_chat is not None:
                return self._mini_flow(
                    token, admin_id=uid, chat_id=id_chat, stage=stage
                )
            # 沒有 chat_id（TG 標準 initData 通常沒有）：只綁管理員＋階段
            return self._mini_flow(token, admin_id=uid, stage=stage)

        @router.get("/tg/addacc", response_class=HTMLResponse)
        async def mini_page(req: Request):
            token = _tok(req)
            if self._mini_flow(token) is None:
                return HTMLResponse("<h3>令牌過期，請回到 bot 再按「➕ 新增帳號」</h3>")
            return HTMLResponse(_MINI_HTML)

        @router.post("/tgapi/start")
        async def start(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            data = await req.json()
            phone = str(data.get("phone", "")).strip()
            if not phone:
                return JSONResponse({"error": "請輸入手機號碼"}, status_code=400)
            flow = await _flow_for(req, data, stage="page")
            if flow is None:
                return JSONResponse({"error": "令牌無效或與管理員/聊天不符"},
                                     status_code=400)
            try:
                r = await self.login_service.start(phone)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            if not isinstance(r, dict) or not r.get("auth_id"):
                return JSONResponse({"error": "驗證碼發送失敗"}, status_code=400)
            # 綁定 auth_id 與 code 階段：之後 code/pass/claim 都用這個流程
            flow.auth_id = str(r["auth_id"])
            flow.stage = "code"
            return JSONResponse({"ok": True, "auth_id": r["auth_id"]})

        @router.post("/tgapi/code")
        async def code(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            data = await req.json()
            code_v = str(data.get("code", "")).strip()
            if not code_v:
                return JSONResponse({"error": "請輸入驗證碼"}, status_code=400)
            flow = await _flow_for(req, data, stage="code")
            if flow is None or not flow.auth_id:
                return JSONResponse({"error": "先發驗證碼"}, status_code=400)
            try:
                r = await self.login_service.submit_code(flow.auth_id, code_v)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            # 真正服務回 status（code_required/password_required/authorized），
            # 沒有 need_password——用 status 推進階段，未知狀態明確拒絕。
            status = r.get("status") if isinstance(r, dict) else None
            if status == "password_required":
                flow.stage = "pass"
                return JSONResponse(
                    {"ok": True, "need_password": True, "status": status}
                )
            if status == "authorized":
                flow.stage = "claim"
                return JSONResponse(
                    {"ok": True, "need_password": False, "status": status}
                )
            return JSONResponse(
                {"error": "未知驗證狀態", "status": status}, status_code=400
            )

        @router.post("/tgapi/pass")
        async def pas(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            data = await req.json()
            pw = str(data.get("password", "")).strip()
            if not pw:
                return JSONResponse({"error": "請輸入密碼"}, status_code=400)
            flow = await _flow_for(req, data, stage="pass")
            if flow is None or not flow.auth_id:
                return JSONResponse({"error": "流程不存在"}, status_code=400)
            try:
                r = await self.login_service.submit_password(flow.auth_id, pw)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            # 只有真正 authorized 才進 claim；未知狀態明確拒絕（不冒稱成功）。
            status = r.get("status") if isinstance(r, dict) else None
            if status == "authorized":
                flow.stage = "claim"
                return JSONResponse({"ok": True, "status": status})
            return JSONResponse(
                {"error": "未知驗證狀態", "status": status}, status_code=400
            )

        @router.post("/tgapi/claim")
        async def claim(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            data = await req.json()
            name = str(data.get("name", "")).strip() or "水軍帳號"
            flow = await _flow_for(req, data, stage="claim")
            if flow is None or not flow.auth_id:
                return JSONResponse({"error": "流程不存在"}, status_code=400)
            try:
                verified = await self.login_service.claim(flow.auth_id)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            # 成功 claim → 流程用完（used）：之後重放同 token 會被擋
            flow.used = True
            account = await self.manager.add_account(
                name, verified.session_string, enable=False,
                display_name=str(getattr(verified, "tg_name", "") or ""),
            )
            await self.manager.db.update_account(
                account["id"],
                tg_user_id=verified.tg_user_id,
                tg_username=str(getattr(verified, "tg_name", "") or ""),
                avatar=str(getattr(verified, "avatar", "") or ""),
                enabled=0,
            )
            self._mini_flows.pop(_tok(req), None)
            return JSONResponse({"ok": True,
                                 "account": self._public_account(account),
                                 "tg_name": str(getattr(verified, "tg_name", ""))})

        return router

    # ---------- 權限 ----------

    def _allowed(self, event) -> bool:
        # 預設拒絕（fail-closed）：admin_ids 空＝沒設定管理者＝不給任何人操作。
        # 以前空時 return True（＝任何人可操作），安全失敗路徑要改成 fail-closed。
        if not self.admin_ids:
            return False
        # 優先用同步的 sender_id（CallbackQuery/NewMessage 都有，且 always set）；
        # .sender 是 property，entity 未快取時可能是 None，不可靠。
        sid = getattr(event, "sender_id", None)
        if sid is None:
            sender = event.sender
            sid = getattr(sender, "id", None) if sender is not None else None
        if sid is None:
            return False
        return str(sid) in self.admin_ids

    # ---------- 指令處理 ----------

    async def _on_message(self, event) -> None:
        if not self._allowed(event):
            await event.reply("⚠️ 不是管理者（BOT_ADMIN_IDS 未包含你）")
            return
        text = (event.raw_text or "").strip()
        # 按鍵式菜單：按鈕回傳的是標籤（如「📊 狀態」），先映射成指令
        if text in MENU_LABELS:
            if not MENU_LABELS[text]:  # 「⌨ 隱藏」→ 收掉下方固定鍵盤
                await event.reply(
                    "⌨ 鍵盤已隱藏（訊息內按鈕仍可用，再按「📊 狀態」重建）",
                    buttons=types.ReplyKeyboardHide(),
                )
                return
            text = f"/{MENU_LABELS[text]}"
        cmd = text.split()
        name = cmd[0].lower().lstrip("/").split("@")[0] if cmd else ""
        rest: list[str] = list(cmd[1:])
        try:
            # 回 (文字, 鍵盤)；鍵盤＝下方固定鍵盤 / 內嵌主菜單 / 無
            if name in {"start", "help", "", "acct"}:
                body, kb = (
                    self._help() if name in {"start", "help", ""}
                    else await self._home_text(),
                    await self._home_keyboard(),
                )
            elif name == "status":
                body, kb = await self._status(), self._reply_menu
            elif name == "groups":
                body, kb = await self._groups(rest), self._reply_menu
            elif name in {"privates", "priv"}:
                body, kb = await self._privates(rest), self._reply_menu
            elif name in {"startacc", "stopacc", "run"}:
                body, kb = await self._acc_toggle(name, rest), self._reply_menu
            elif name in {"deleteacc", "del"}:
                body, kb = await self._acc_delete(rest, event)
            elif name in {"media", "voice", "imagegen"}:
                body, kb = await self._feature_toggle(name, rest), self._reply_menu
            elif name in {"check", "selfcheck"}:
                body, kb = await self._system_self_check()
            elif name in {"panic", "stopall", "quiet"}:
                body, kb = await self._acc_panic_stop()
            elif name in {"test", "preview"}:
                body, kb = await self._test_reply_preview(rest), self._reply_menu
            elif name in {"send", "say", "msg"}:
                body, kb = await self._send_as_account(rest), self._reply_menu
            elif name == "addacc":
                body, kb = await self._add_start(rest, event)
            elif name == "code":
                body, kb = await self._add_code(rest, event), self._reply_menu
            elif name == "pass":
                body, kb = await self._add_pass(rest, event), self._reply_menu
            elif name == "name":
                body, kb = await self._add_name(rest, event), self._reply_menu
            elif name == "gsearch":
                body, kb = await self._gsearch(rest, event)
            elif name == "personasave":
                body, kb = await self._persona_save_cmd(rest), self._reply_menu
            elif name in {"logs", "log"}:
                body, kb = (
                    await self._acc_logs(rest[0]) if rest
                    else "用法：/logs <帳號> [則數]",
                    self._reply_menu,
                )
            else:
                body, kb = f"不認識的指令「/{name}」\n" + self._help(), self._reply_menu
            await event.reply(body[:MSG_LIMIT], buttons=kb)
        except Exception as e:
            log.exception("bot 指令 %r 失敗", text)
            await event.reply(f"⚠️ {type(e).__name__}: {e}",
                              buttons=self._reply_menu)

    # ---------- 內嵌按鈕（callback） ----------

    async def _on_callback(self, event) -> None:
        raw = getattr(event, "data", None)
        if isinstance(raw, bytes):
            data = raw.decode()
        else:
            data = str(raw or "")
        # 權限：callback 的 sender 是按下按鈕的人；非管理者按按鈕給提示
        if not self._allowed(event):
            try:
                await event.answer("⚠️ 不是管理者（BOT_ADMIN_IDS 未包含你）", show_alert=True)
            except Exception:
                pass
            return
        try:
            await self._dispatch_callback(event, data)
        except Exception as e:
            log.exception("callback %r 失敗", data)
            try:
                await event.answer(f"⚠️ {type(e).__name__}: {e}")
            except Exception:
                pass

    async def _dispatch_callback(self, event, data: str) -> None:
        """內嵌按鈕路由：data 格式「action[:arg]」。"""
        if not data:
            await event.answer()
            return
        action, _, arg = data.partition(":")

        if action == "home":
            await event.edit(
                await self._home_text(), buttons=await self._home_keyboard()
            )
        elif action == "status":
            await event.edit(
                (await self._status())[:MSG_LIMIT],
                buttons=await self._home_keyboard(),
            )
        elif action == "groups":
            await event.edit(
                (await self._groups([]))[:MSG_LIMIT],
                buttons=await self._home_keyboard(),
            )
        elif action == "help":
            await event.edit(
                self._help()[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc":
            # 選了某帳號 → 該帳號的「可操作功能」內嵌菜單
            body, kb = await self._account_actions_keyboard(arg)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.start":
            body = await self._acc_toggle("startacc", [arg])
            await event.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc.stop":
            body = await self._acc_toggle("stopacc", [arg])
            await event.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc.del":
            # 先給確認頁（避免誤觸直接刪），簽發一次性 token
            admin = str(getattr(event, "sender_id", "") or "")
            chat = int(getattr(event, "chat_id", 0) or 0)
            body, kb = await self._acc_delete_confirm(arg, admin, chat)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.del.yes":
            # arg＝一次性 token：驗 期限/管理員/聊天 綁定，按第二次會被擋。
            # pop 先拿走再驗證（atomic）：驗證不過也消耗掉，防並行雙擊二次刪。
            info = self._del_confirms.pop(arg, None)
            if info is None:
                await event.answer("確認已過期，請重新按「🗑 刪除」",
                                   show_alert=True)
                return
            acc_id, adm, cht, exp = info
            ev_sid = str(getattr(event, "sender_id", "") or "")
            ev_chat = int(getattr(event, "chat_id", 0) or 0)
            if (
                time.time() > exp
                or str(adm) != ev_sid
                or int(cht) != ev_chat
            ):
                # 錯管理員/錯聊天/過期：確認已消耗（防重放），提示重來
                await event.answer("確認已過期或不是原按者", show_alert=True)
                return
            accounts = await self.manager.db.list_accounts()
            acc = next((a for a in accounts if a["id"] == acc_id), None)
            err = await self.manager.delete(acc_id)
            body = (
                f"🗑️ 已刪除 {acc.get('name') if acc else acc_id}（含記憶）"
                if not err else f"⚠️ 刪除失敗：{err}"
            )
            await event.edit(body[:MSG_LIMIT],
                             buttons=await self._home_keyboard())
        elif action == "acc.del.no":
            # 取消：arg＝該確認的 token（綁定特定確認，不會誤刪別份）。
            # pop 掉指定 token；同帳號的其他確認保留（一致策略：只取消這份）。
            info = self._del_confirms.pop(arg, None)
            acc_id = info[0] if info else arg
            body, kb = await self._account_actions_keyboard(acc_id)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.groups":
            admin = str(getattr(event, "sender_id", "") or "")
            chat = int(getattr(event, "chat_id", 0) or 0)
            body, kb = await self._groups_menu(arg, admin, chat)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif data.startswith("g.save:"):
            # g.save:<acc_id> 保存勾選（要先於一般 g: 分支）
            _, acc_id = data.split(":", 1)
            admin = str(getattr(event, "sender_id", "") or "")
            chat = int(getattr(event, "chat_id", 0) or 0)
            body = await self._groups_save(acc_id, admin, chat)
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif data.startswith("gp:"):
            # gp:<page>:<acc_id> 群組分頁（要先於 g:，gp: 不會被 g: 吃到）
            _, page_s, acc_id = data.split(":", 2)
            admin = str(getattr(event, "sender_id", "") or "")
            chat = int(getattr(event, "chat_id", 0) or 0)
            st = self._group_state((str(admin), int(chat), str(acc_id)))
            try:
                st["page"] = max(0, int(page_s))
            except ValueError:
                st["page"] = 0
            body, kb = await self._groups_menu(acc_id, admin, chat)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif data.startswith("g:"):
            # g:<gid>:<acc_id> 切換某群組勾選
            _, gid_s, acc_id = data.split(":", 2)
            admin = str(getattr(event, "sender_id", "") or "")
            chat = int(getattr(event, "chat_id", 0) or 0)
            try:
                gid = int(gid_s)
            except ValueError:
                await event.answer("群組 id 格式錯誤", show_alert=True)
                return
            st = self._group_state((str(admin), int(chat), str(acc_id)))
            if gid in st["sel"]:
                st["sel"].discard(gid)
            else:
                st["sel"].add(gid)
            body, kb = await self._groups_menu(acc_id, admin, chat)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "model":
            body = self._model_info()
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "usage":
            body = await self._usage_info()
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "acc.persona":
            body, kb = await self._persona_actions_keyboard(arg)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.master":
            body, kb = await self._acc_master_toggle(arg)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.master.all":
            body, kb = await self._acc_master_all_toggle()
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.panic.stop":
            body, kb = await self._acc_panic_stop()
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "check":
            body, kb = await self._system_self_check()
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "testroom":
            body = (
                "💬 試聊預覽室（測試不發群）：\n"
                "──────────────\n"
                "用法：/test <角色名> <模擬群聊訊息>\n"
                "例如：\n"
                "`/test 小天後 我只有13公分 我退群好了`\n"
                "`/test 小小 每天被老闆壓榨好想放鬆`\n\n"
                "回覆只在 Bot 私聊中預覽展示，絕不會發到真實 Telegram 群組！"
            )
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "acc.reply":
            body = await self._acc_flag_toggle(arg, "reply_enabled")
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "acc.proactive":
            body = await self._acc_flag_toggle(arg, "proactive_enabled")
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "acc.logs":
            body = await self._acc_logs(arg)
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "acc.send":
            body = (
                f"✉️ 指定發言｜{arg}\n"
                "──────────────\n"
                "用法：/send " + arg + " <想發送的訊息內容>\n"
                "若要指定群組：/send " + arg + " <群組ID> <訊息內容>\n\n"
                "例如：\n"
                f"`/send {arg} 大家晚上好呀`\n"
                f"`/send {arg} -1002229799107 今晚好冷有人要吃宵夜嗎`\n\n"
                "（此指令由該水軍 Telegram 帳號直接發送到該群）"
            )
            await event.edit(
                body[:MSG_LIMIT],
                buttons=types.ReplyInlineMarkup(kbrows([[
                    cb("back.acc:" + arg, "🔙 回帳號操作"),
                    cb("back.home", "🏠 主菜單"),
                ]])),
            )
        elif action == "persona.edit":
            body = await self._persona_edit(arg)
            await event.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "priv":
            body = await self._privates([arg])
            await event.edit(
                body[:MSG_LIMIT],
                buttons=types.ReplyInlineMarkup(kbrows([[
                    cb(
                        "back.acc:" + arg, "🔙 回帳號操作"
                    ),
                    cb("back.home", "🏠 主菜單"),
                ]])),
            )
        elif action in {"media", "imagegen", "voice"}:
            body = await self._feature_toggle(action, [])
            await event.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "addacc":
            # #7：inline 新增也走 Mini App（跟文字 /addacc 同一閉環）
            admin = str(getattr(event, "sender_id", "") or "")
            chat = int(getattr(event, "chat_id", 0) or 0)
            token = self._mini_mint(admin, chat)
            await event.edit(
                "➕ 新增帳號：按「📱 開 Mini App」填手機號＋驗證碼＋名稱\n"
                "（完成後回這裡設群組/人設，再「▶ 啟動」上線）",
                buttons=self._mini_keyboard_token(token, admin, chat),
            )
        elif action == "hidekb":
            await event.edit(
                "⌨ 已隱藏下方快速鍵盤（訊息內按鈕仍可用，再按「📊 狀態」重建）",
                buttons=types.ReplyKeyboardHide(),
            )
            await event.answer("下方鍵盤已隱藏")
        elif action == "persona.show":
            body = await self._persona_show(arg)
            await event.edit(
                body[:MSG_LIMIT], buttons=self._persona_keyboard(arg)
            )
        elif action == "persona.regen":
            body = await self._persona_regen(arg)
            await event.edit(
                body[:MSG_LIMIT], buttons=self._persona_keyboard(arg)
            )
        elif action == "back.home":
            await event.edit(
                await self._home_text(), buttons=await self._home_keyboard()
            )
        elif action == "back.acc":
            body, kb = await self._account_actions_keyboard(arg)
            await event.edit(body[:MSG_LIMIT], buttons=kb)
        else:
            await event.answer("未處理的按鈕", show_alert=True)

    # ---------- 內嵌主菜單（按鍵式交互，類圖示） ----------

    @staticmethod
    def _acc_label(a: dict) -> str:
        """帳號顯示名：優先 TG 顯示名（tg_username），沒有才用內部帳號名。"""
        return str(a.get("tg_username") or "").strip() or str(a.get("name") or "")

    async def _account_rows(self) -> list[str]:
        """帳號行（文字）：狀態＋名字＋所在群，_status/_home_text 共用。"""
        data = await self.manager.status()
        rows = []
        for a in sorted(
            data.get("accounts", []),
            key=lambda x: not x.get("is_running"),
        ):
            on = bool(a.get("is_running"))
            state = a.get("state", "")
            groups = a.get("groups") or []
            rows.append(
                f"{'🟢 運行中' if on else '⚪ ' + (state or '待機中')}｜{self._acc_label(a)}（{a['id']}）"
                + (f"｜群 {', '.join(str(g) for g in groups)}" if groups else "")
            )
        return rows

    async def _home_text(self) -> str:
        rows = await self._account_rows()
        n = len(rows)
        return (
            "🎛️ SDF 控制台\n"
            "──────────────\n"
            + ("（尚無帳號）" if not rows else "账号:")
            + "\n"
            + "\n".join(rows)
            + f"\n\n選擇账号（{n} 個）→ 展開該账号可操作功能"
        )

    async def _home_keyboard(self) -> types.ReplyInlineMarkup:
        accs = sorted(
            (await self.manager.status()).get("accounts", []),
            key=lambda x: not x.get("is_running"),
        )
        rows = [
            [
                cb("status", "📊 狀態"),
                cb("groups", "🗂 群組"),
            ],
            [
                cb("help", "📖 說明"),
                cb("addacc", "➕ 新增帳號"),
            ],
        ]
        # 帳號清單：2 欄排列（類參考圖 provider 清單），運行中加 ✓ 前綴
        for i in range(0, len(accs), 2):
            row = []
            for a in accs[i:i + 2]:
                on = bool(a.get("is_running"))
                mark = "✓ " if on else ""
                row.append(cb(
                    "acc:" + a["id"],
                    f"{mark}{self._acc_label(a)}（{'運行' if on else '待機'}）",
                ))
            rows.append(row)
        rows.append([
            cb("media", "🖼 媒體識別"),
            cb("imagegen", "📷 圖片生成"),
            cb("voice", "🔊 語音"),
        ])
        # 全局總開關：所有帳號 reply+proactive 都開→全靜音；否則→全開啟
        all_both_on = bool(accs) and all(
            int(a.get("reply_enabled") or 0) and int(a.get("proactive_enabled") or 0)
            for a in accs
        )
        master_txt = "🔕 全部靜音" if all_both_on else "🔔 全部開啟"
        rows.append([
            cb("acc.master.all", master_txt),
            cb("acc.panic.stop", "🚨 緊急全停"),
        ])
        rows.append([
            cb("check", "🩺 一鍵自檢"),
            cb("testroom", "💬 試聊預覽"),
        ])
        rows.append([
            cb("model", "🧩 模型"),
            cb("usage", "📈 用量"),
        ])
        rows.append([
            cb("hidekb", "⌨ 隱藏鍵盤"),
        ])
        return types.ReplyInlineMarkup(kbrows(rows))

    async def _account_actions_keyboard(self, account_id: str):
        """選了某帳號 → 該帳號「可操作功能」內嵌菜單。回 (文字, 鍵盤)。"""
        data = await self.manager.status()
        acc = next(
            (a for a in data.get("accounts", []) if a["id"] == account_id), None
        )
        name = self._acc_label(acc) if acc else account_id
        on = bool(acc and acc.get("is_running"))
        toggle_txt = "⏹ 停止" if on else "▶ 啟動"
        toggle_action = "acc.stop" if on else "acc.start"
        state = acc.get("state", "待機中") if acc else "待機中"
        body = (
            f"👤 帳號操作｜{name}（{account_id}）\n"
            "──────────────\n"
            f"狀態：{'🟢 運行中' if on else '⚪ ' + state}\n"
            "可操作功能:"
        )
        rep_on = bool(acc and acc.get("reply_enabled", 1))
        pro_on = bool(acc and acc.get("proactive_enabled", 1))
        both_on = rep_on and pro_on
        master_txt = "🔕 全部靜音" if both_on else "🔔 全部開啟"
        rows = [
            [
                cb(
                    "acc.master:" + account_id, master_txt
                ),
                cb(
                    toggle_action + ":" + account_id, toggle_txt
                ),
            ],
            [
                cb(
                    "acc.persona:" + account_id, "🧬 人設"
                ),
                cb(
                    "acc.groups:" + account_id, "📍 群組"
                ),
                cb(
                    "acc.send:" + account_id, "✉️ 發言"
                ),
            ],
            [
                cb(
                    "priv:" + account_id, "📩 私訊"
                ),
                cb("acc.reply:" + account_id,
                   f"💬 回覆：{'開' if rep_on else '關'}"),
                cb("acc.proactive:" + account_id,
                   f"📣 主動：{'開' if pro_on else '關'}"),
            ],
            [
                cb("acc.logs:" + account_id, "📜 日誌"),
                cb(
                    "acc.del:" + account_id, "🗑 刪除"
                ),
                cb("back.home", "🔙 回主菜單"),
            ],
        ]
        return body, types.ReplyInlineMarkup(kbrows(rows))

    async def _persona_actions_keyboard(self, account_id: str):
        acc = next(
            (
                a
                for a in (await self.manager.status()).get("accounts", [])
                if a["id"] == account_id
            ),
            None,
        )
        name = self._acc_label(acc) if acc else account_id
        body = f"人設操作｜{name}（{account_id}）"
        rows = [
            [
                cb(
                    "persona.show:" + account_id, "👀 查看人設"
                ),
                cb(
                    "persona.regen:" + account_id, "♻️ 重新生成"
                ),
            ],
            [
                cb(
                    "back.acc:" + account_id, "🔙 回帳號操作"
                ),
            ],
        ]
        return body, types.ReplyInlineMarkup(kbrows(rows))

    async def _persona_show(self, account_id: str) -> str:
        acc = await self.manager.db.get_account(account_id)
        if not acc:
            return f"找不到帳號 {account_id}"
        persona = acc.get("persona") or {}
        if isinstance(persona, str):
            try:
                persona = json.loads(persona)
            except Exception:
                persona = {}
        keys = (
            "name", "nickname", "gender", "age", "city", "job",
            "occupation", "personality", "speaking_style", "catchphrase",
            "likes", "dislikes", "bio", "intro", "mbti", "education",
        )
        lines = [f"人設｜{account_id}"]
        for k in keys:
            v = persona.get(k)
            if v:
                lines.append(f"· {k}: {str(v)[:80]}")
        return "\n".join(lines)[:MSG_LIMIT]

    async def _persona_regen(self, account_id: str) -> str:
        p = await self.manager.regen_persona(account_id)
        return (
            f"♻️ 已重新生成 {account_id} 的人設"
            + (f"（{p.get('name', '')}）" if p else "（被保護中，未改動）")
        )

    def _persona_keyboard(self, account_id: str) -> types.ReplyInlineMarkup:
        rows = [
            [
                cb(
                    "persona.show:" + account_id, "👀 再看一下"
                ),
                cb(
                    "persona.regen:" + account_id, "♻️ 重新生成"
                ),
            ],
            [
                cb(
                    "persona.edit:" + account_id, "✏️ 編輯人設"
                ),
            ],
            [
                cb(
                    "back.acc:" + account_id, "🔙 回帳號操作"
                ),
            ],
        ]
        return types.ReplyInlineMarkup(kbrows(rows))

    # ---------- 各指令內容 ----------

    def _help(self) -> str:
        return (
            "SDF 控制台 bot\n"
            "──────────────\n"
            "📊 查看\n"
            "/status 運行狀態＋24h KPI\n"
            "/groups 群組清單（活動排序）\n"
            "/groups <群id> [則數] 看該群最近訊息\n"
            "/privates <帳號> [則數] 看私訊\n"
            "/acct 帳號清單\n"
            "🎛️ 控制\n"
            "/startacc <帳號> 啟動（登入/上線）\n"
            "/stopacc <帳號> 停止（下線/登出）\n"
            "/deleteacc <帳號> 刪除（含記憶）\n"
            "/media on|off 媒體識別開關（看懂別人發的圖/影片）\n"
            "/imagegen on|off 圖片生成開關（被要求時自動發圖/影片）\n"
            "/voice on|off 語音開關\n"
            "➕ 新增帳號（TG 驗證碼流程）\n"
            "/addacc 開 Mini App 填手機號/驗證碼/名稱（密碼不進聊天）\n"
            "/code 12345 輸入驗證碼（Mini App 外備用）\n"
            "/pass 兩步密碼請在 Mini App 內輸入\n"
            "/gsearch <帳號> [關鍵字] 群組勾選頁搜尋\n"
            "/personasave <帳號> {JSON} 編輯人設（白名單欄位）\n"
            "/logs <帳號> 最近收發紀錄\n"
            "/start 本說明"
        )

    async def _status(self) -> str:
        data = await self.manager.status()
        lines = ["SDF 控制台", "──────────────"]
        running = data.get("running", 0)
        total = data.get("total", 0)
        audit = data.get("reply_audit", {}) or {}
        sent = int(((audit.get("sent") or {}).get("ok") or 0))
        policy = audit.get("policy") or {}
        blocked = sum(int(v) or 0 for v in policy.values())
        top_reason, top_n = "", 0
        for r, n in policy.items():
            if int(n or 0) > top_n:
                top_reason, top_n = r, int(n)
        held = sum(
            int((a.get("stats") or {}).get("gate_held") or 0)
            for a in data.get("accounts", [])
        )
        lines.append(f"運行 {running}/{total}｜待處置 {held}")
        line = f"24h 送出 {sent}｜攔截 {blocked}"
        if top_reason:
            line += f"（最多：{top_reason} {top_n}）"
        lines.append(line)
        for acc in data.get("accounts", []):
            persona = {}
            raw = acc.get("persona")
            if isinstance(raw, str):
                try:
                    import json

                    persona = json.loads(raw)
                except Exception:
                    persona = {}
            st = acc.get("stats") or {}
            state = "🟢" if acc.get("is_running") else "⚪"
            lines.append(
                f"{state} {acc.get('name')}（{persona.get('name', '')}）"
                f"｜回覆 {st.get('replies_sent', 0)}"
                f"｜主動 {st.get('proactive_sent', 0)}"
            )
        return "\n".join(lines)

    async def _groups(self, args: list[str]) -> str:
        # 有群 id 參數：看該群訊息
        if args:
            try:
                gid = int(args[0])
            except ValueError:
                return f"群 id 要數字，收到「{args[0]}」"
            try:
                n = max(1, min(60, int(args[1])))
            except (ValueError, IndexError):
                n = 20
            rows = await self.manager.db.get_group_messages(gid, n)
            rows = list(reversed(rows))  # 舊→新
            lines = [f"群 {gid} 最近 {len(rows)} 則", "──────────────"]
            for row in rows:
                role = "水軍" if str(row.get("role")) == "assistant" else "真人"
                who = str(row.get("sender_name") or "?")[:10]
                ts = _fmt_ts(row.get("timestamp") or 0)
                content = str(row.get("content") or "")[:80]
                lines.append(f"[{ts}] {role}·{who}：{content}")
            return "\n".join(lines)

        # 無參數：清單（活動排序前 8）
        db = self.manager.db
        overview_rows = await db.group_overview(exclude_senders=())
        rows = sorted(
            overview_rows,
            key=lambda r: (-float(r.get("last_ts") or 0), r.get("group_id")),
        )[:8]
        if not rows:
            return "還沒有群組紀錄（先讓水軍上線收訊息）"
        labels = await db.get_group_labels()
        lines = ["群組清單（按最近活動）", "──────────────"]
        for r in rows:
            gid = r["group_id"]
            label = labels.get(gid, "")
            name = label or f"群組 {gid}"
            lines.append(
                f"▸ {name}（{gid}）\n"
                f"  訊息 {int(r.get('msg_count') or 0)}"
                f"｜真人 {int(r.get('human_senders') or 0)}"
                f"｜最近 {_fmt_ts(float(r.get('last_ts') or 0))}"
            )
        lines.append("")
        lines.append("看訊息：/groups <群id> [則數]")
        return "\n".join(lines)

    async def _privates(self, args: list[str]) -> str:
        if not args:
            return "用法：/privates <帳號名或id> [則數]"
        target = args[0]
        try:
            n = max(1, min(40, int(args[1])))
        except (ValueError, IndexError):
            n = 15
        accounts = await self.manager.db.list_accounts()
        acc = next(
            (
                a
                for a in accounts
                if str(a.get("id")) == target or str(a.get("name") or "") == target
            ),
            None,
        )
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}"
        msgs = await self.manager.db.get_private_messages(acc["id"], limit=n)
        if not msgs:
            return f"{acc.get('name')}：{n} 則內沒有私訊"
        lines = [f"{acc.get('name')} 收到的私訊", "──────────────"]
        for m in msgs:
            ts = _fmt_ts(m.get("timestamp") or 0)
            who = str(m.get("sender_name") or m.get("sender_id") or "?")[:10]
            mark = "🔵未讀 " if not m.get("read") else "     "
            lines.append(f"{mark}[{ts}] {who}：{str(m.get('content') or '')[:70]}")
        return "\n".join(lines)

    # ---------- 帳號控制 ----------

    async def _acc_delete_confirm(self, account_id: str, admin_id: str,
                                  chat_id: int):
        """刪除確認頁：簽發一次性 token（綁 管理員/聊天/帳號/期限 5 分鐘）。"""
        acc = next(
            (
                a
                for a in (await self.manager.status()).get("accounts", [])
                if a["id"] == account_id
            ),
            None,
        )
        name = self._acc_label(acc) if acc else account_id
        token = secrets.token_hex(8)
        self._del_confirms[token] = (
            account_id, str(admin_id), int(chat_id), time.time() + 300,
        )
        body = (
            f"🗑 確認刪除｜{name}（{account_id}）\n"
            "──────────────\n"
            "刪除後人設、記憶、收發紀錄一併清掉（session 也要重新登入）。\n"
            "5 分鐘內無動作自動失效；「✅ 確認刪除」按第二次會提示已過期。"
        )
        rows = [
            [
                cb("acc.del.yes:" + token, "✅ 確認刪除"),
                cb("acc.del.no:" + token, "↩️ 再想想"),
            ],
        ]
        return body, types.ReplyInlineMarkup(kbrows(rows))

    def _group_state(self, key: tuple) -> dict:
        """群組勾選狀態（按 管理員/聊天/帳號 隔離，10 分鐘期限，觸即續期）。"""
        st = self._group_selections.get(key)
        now = time.time()
        if st is None or now > st.get("exp", 0):
            st = {"sel": set(), "page": 0, "q": "", "exp": now + 600,
                  "primed": False}
            self._group_selections[key] = st
        else:
            st["exp"] = now + 600
        return st

    async def _groups_menu(self, account_id: str, admin_id: str,
                           chat_id: int):
        """群組勾選頁：分頁＋搜尋，勾選狀態按 管理員/聊天/帳號 隔離。"""
        key = (str(admin_id), int(chat_id), str(account_id))
        st = self._group_state(key)
        data = await self.manager.status()
        acc = next(
            (a for a in data.get("accounts", []) if a["id"] == account_id), None
        )
        if acc is None:
            return f"找不到帳號 {account_id}", types.ReplyInlineMarkup(
                kbrows([[cb("back.home", "🔙 回主菜單")]])
            )
        name = self._acc_label(acc)
        if not st["primed"]:
            # 首次進入：預設＝目前已設定的群組
            st["sel"] = set(acc.get("groups") or [])
            st["primed"] = True
        # 群組清單：優先用 worker 的（運行中），沒有才臨時抓
        available = acc.get("groups_available") or []
        if not available:
            available, err = await self.manager.list_available_groups(account_id)
            if err:
                return f"⚠️ 抓群組清單失敗：{err}", types.ReplyInlineMarkup(
                    kbrows([[cb("back.acc:" + account_id, "🔙 回帳號操作")]])
                )
        q = str(st.get("q", "")).lower()
        filtered = available
        if q:
            filtered = [
                g for g in available
                if q in str(g.get("title") or "").lower() or q in str(g.get("id"))
            ]
        total = len(filtered)
        page_size = 8
        n_pages = max(1, (total + page_size - 1) // page_size)
        st["page"] = max(0, min(int(st.get("page", 0)), n_pages - 1))
        page = st["page"]
        lines = [
            f"📍 群組勾選｜{name}（{account_id}）",
            "──────────────",
            f"已選 {len(st['sel'])} 個｜第 {page + 1}/{n_pages} 頁（共 {total} 個）",
        ]
        if q:
            lines.append(f"搜尋：{st['q']}（/gsearch {account_id} 新關鍵字，"
                         f"/gsearch {account_id} 清除）")
        if not filtered:
            lines.append("（沒有符合的群組。先讓帳號上線收過訊息，或換關鍵字）")
        rows = []
        for g in filtered[page * page_size:(page + 1) * page_size]:
            gid = int(g["id"])
            title = str(g.get("title") or f"群組 {gid}")[:24]
            mark = "✅" if gid in st["sel"] else "▫️"
            rows.append([cb(f"g:{gid}:{account_id}", f"{mark} {title}")])
        nav = []
        if page > 0:
            nav.append(cb(f"gp:{page - 1}:{account_id}", "⬅️ 上頁"))
        if page < n_pages - 1:
            nav.append(cb(f"gp:{page + 1}:{account_id}", "➡️ 下頁"))
        if nav:
            rows.append(nav)
        rows.append([
            cb("g.save:" + account_id, "💾 保存群組"),
            cb("back.acc:" + account_id, "🔙 回帳號操作"),
        ])
        return "\n".join(lines), types.ReplyInlineMarkup(kbrows(rows))

    async def _groups_save(self, account_id: str, admin_id: str,
                           chat_id: int) -> str:
        key = (str(admin_id), int(chat_id), str(account_id))
        st = self._group_selections.get(key)
        now = time.time()
        if st is None or now > st.get("exp", 0):
            return "⚠️ 群組勾選階段已逾期或失效，未保存任何變更（已保護原群組不被清空）。請重新點進群組選單操作。"
        sel = st.get("sel", set())
        err = await self.manager.save_groups(account_id, sorted(sel))
        if err:
            return f"⚠️ 保存失敗：{err}"
        self._group_selections.pop(key, None)
        # save_groups 會即時更新運行中 worker 的群組＋標 setup_complete，
        # 不再誤報「重啟後生效」（#9 真實生效回報）。
        running = any(
            a.get("id") == account_id and a.get("is_running")
            for a in (await self.manager.status()).get("accounts", [])
        )
        eff = ("運行中帳號已即時切換群組（立即生效，不需重啟）"
               if running else "已保存，帳號啟動時按此清單進群")
        return (
            f"💾 已保存 {len(sel)} 個群組（{account_id}）。\n{eff}。\n"
            "setup_complete 已標記，可 /startacc 上線。"
        )

    def _model_info(self) -> str:
        cfg = self.manager.config
        lines = [
            "🧩 目前模型設定（只讀顯示）",
            "──────────────",
            f"主聊天：{cfg.ai_model}",
            f"  端點：{cfg.chat_base_url}",
            f"備援模型：{', '.join(cfg.ai_fallback_models) or '（無）'}",
            f"視覺識別：{cfg.vision_model}",
            f"  兜底：{cfg.vision_fallback_model}",
            f"圖片生成：{cfg.image_model}",
            f"影片生成：{cfg.video_model}",
            f"語音：{cfg.speech_model}",
            "",
            "作用範圍：環境變數級，所有帳號共用；重部署後才生效。",
            "Bot 內安全編輯尚未完成（此頁＝只讀，不是改動）。",
            "目前安全改法：Railway 改 AI_MODEL 等環境變數＋重部署"
            "（勿把 API key 傳到聊天）。",
            "（未來 Bot 內編輯＝表單＋服務層寫入，禁止聊天輸入密鑰）",
        ]
        return "\n".join(lines)

    async def _usage_info(self) -> str:
        data = await self.manager.status()
        summary = await self.manager.db.ai_call_summary(hours=24)
        lines = [
            "📈 24 小時用量",
            "──────────────",
            f"模型呼叫：{summary['totals']['calls']} 次"
            f"（有帳 {summary['totals']['measured']}）",
            f"token：{summary['totals']['prompt_tokens']}"
            f" 入 / {summary['totals']['completion_tokens']} 出",
            f"重試：{summary['totals']['retries']}",
            "",
            "依用途：",
        ]
        for purpose, v in sorted(
            summary["by_purpose"].items(), key=lambda x: -x[1]["calls"]
        ):
            lines.append(
                f"· {purpose}：{v['calls']} 次｜"
                f"延遲均值 {v['avg_latency_ms']}ms"
            )
        spend = data.get("media_spend_usd", 0)
        budget = data.get("media_budget_usd", 0)
        lines.append("")
        lines.append(
            f"媒體花費：${spend} / ${budget}（每日預算）"
        )
        held = sum(
            int((a.get("stats") or {}).get("gate_held") or 0)
            for a in data.get("accounts", [])
        )
        lines.append(f"門控暫緩累計：{held}")
        res = "\n".join(lines).strip()
        return res if res else "暫無用量數據"
        cfg = self.manager.config
        if "notrack" in str(getattr(cfg, "ai_base_url", "")).lower():
            bal = await notrack_balance(
                getattr(cfg, "ai_api_key", "") or "",
                str(getattr(cfg, "ai_base_url", "")),
            )
            if bal is None:
                lines.append("\n💰 NoTrack 額度：讀取失敗（key 或網路）")
            else:
                warn = "⚠️ 快用完了，記得充值" if bal < 0.05 else ""
                lines.append(
                    f"\n💰 NoTrack 額度：${bal:.4f}"
                    + (f"　{warn}" if warn else "")
                )
        return "\n".join(lines)

    # ---------- 每帳號開關／人設編輯／日誌（#10） ----------

    # 人設白名單：聊天輸入的 JSON 只收這些欄位，key/secret/hash 進不來
    _PERSONA_FIELDS = (
        "name", "nickname", "gender", "age", "city", "job", "occupation",
        "personality", "speaking_style", "catchphrase", "likes", "dislikes",
        "bio", "intro", "mbti", "education", "hobbies", "chat_style",
    )

    async def _acc_flag_toggle(self, account_id: str, flag: str) -> str:
        """每帳號 回覆/主動 開關：db＋運行中 worker 即時更新（不需重啟）。"""
        acc = await self.manager.db.get_account(account_id)
        if not acc:
            return f"找不到帳號 {account_id}"
        new = 0 if int(acc.get(flag) or 0) else 1
        await self.manager.db.update_account(account_id, **{flag: new})
        worker = (getattr(self.manager, "workers", {}) or {}).get(str(account_id))
        if worker is not None:
            setattr(worker, flag, bool(new))  # worker 每則訊息都重讀此值
        label = "回覆" if flag == "reply_enabled" else "主動"
        return (
            f"{'✅' if new else '⛔'} {self._acc_label(acc)} {label}開關："
            f"{'開' if new else '關'}（運行中 worker 已即時生效）"
        )

    async def _acc_master_toggle(self, account_id: str) -> tuple[str, object]:
        """帳號總開關：一鍵把 回覆＋主動 設成同值（全靜音／全開啟），
        運行中 worker 即時生效、不需重啟。回 (狀態文字, 該帳號操作鍵盤)。"""
        acc = await self.manager.db.get_account(account_id)
        if not acc:
            return f"找不到帳號 {account_id}", await self._home_keyboard()
        rep = bool(int(acc.get("reply_enabled") or 0))
        pro = bool(int(acc.get("proactive_enabled") or 0))
        both_on = rep and pro
        # 全開（兩者都開）→ 一鍵全靜音；否則 → 一鍵全開啟
        target = 0 if both_on else 1
        await self.manager.db.update_account(
            account_id,
            reply_enabled=target,
            proactive_enabled=target,
        )
        worker = (getattr(self.manager, "workers", {}) or {}).get(str(account_id))
        if worker is not None:
            worker.reply_enabled = bool(target)
            worker.proactive_enabled = bool(target)
        on = bool(target)
        icon = "🔔" if on else "🔕"
        line = (
            f"{icon} {self._acc_label(acc)} 總開關：{'全開啟（回覆＋主動）' if on else '全靜音（回覆＋主動都關）'}"
            "（運行中 worker 已即時生效）"
        )
        body, kb = await self._account_actions_keyboard(account_id)
        return line + "\n──────────────\n" + body, kb

    async def _acc_master_all_toggle(self) -> tuple[str, object]:
        """全局總開關（主選單）：一鍵把 所有帳號 的 回覆＋主動 設成同值。
        所有帳號都開→全靜音；否則→全開啟。回 (狀態文字, 主選單鍵盤)。"""
        accounts = await self.manager.db.list_accounts()
        if not accounts:
            return "還沒有水軍帳號", await self._home_keyboard()
        # 判斷：所有帳號 reply+proactive 都開？
        all_both_on = all(
            int(a.get("reply_enabled") or 0) and int(a.get("proactive_enabled") or 0)
            for a in accounts
        )
        target = 0 if all_both_on else 1
        on = bool(target)
        icon = "🔔" if on else "🔕"
        names = []
        for a in accounts:
            await self.manager.db.update_account(
                a["id"],
                reply_enabled=target,
                proactive_enabled=target,
            )
            worker = (getattr(self.manager, "workers", {}) or {}).get(str(a["id"]))
            if worker is not None:
                worker.reply_enabled = bool(target)
                worker.proactive_enabled = bool(target)
            names.append(self._acc_label(a))
        line = (
            f"{icon} 全部帳號總開關：{'全開啟（回覆＋主動）' if on else '全靜音（回覆＋主動都關）'}\n"
            f"{'、'.join(names)}（運行中 worker 已即時生效）"
        )
        return line, await self._home_keyboard()

    async def _acc_panic_stop(self) -> tuple[str, object]:
        """🚨 緊急全停：確定性強制靜音全部帳號＋取消排隊任務，絕不反彈。"""
        accounts = await self.manager.db.list_accounts()
        if not accounts:
            return "還沒有水軍帳號", await self._home_keyboard()
        names = []
        cancelled_total = 0
        still_running_total = 0
        for a in accounts:
            await self.manager.db.update_account(
                a["id"],
                reply_enabled=0,
                proactive_enabled=0,
            )
            worker = (getattr(self.manager, "workers", {}) or {}).get(str(a["id"]))
            if worker is not None:
                worker.reply_enabled = False
                worker.proactive_enabled = False
                # 取消在排隊的回覆與主動發送任務
                tasks = list(getattr(worker, "_reply_tasks", set()))
                cancelled_total += len(tasks)
                for t in tasks:
                    t.cancel()
                if worker.is_running:
                    still_running_total += 1
            names.append(self._acc_label(a))
        msg = (
            "🚨 【緊急全停已生效】\n"
            "──────────────\n"
            f"受影響帳號：{'、'.join(names)}\n"
            f"🚫 已取消排隊中任務：{cancelled_total} 個\n"
            f"🔄 正在收尾監聽帳號：{still_running_total} 個\n"
            "狀態：回覆(reply)=0，主動(proactive)=0（已持久化寫入 DB，重啟後依然保持靜音）！\n\n"
            "如需恢復發言，請至主選單點擊「🔔 全部開啟」或進入個別帳號開啟。"
        )
        return msg, await self._home_keyboard()

    async def _system_self_check(self) -> tuple[str, object]:
        """🩺 一鍵自檢：真實檢測 Telegram 連線、資料庫連通、群組權限、模型狀態與發言診斷。"""
        lines = [
            "🩺 系統健康真實自檢報告",
            "──────────────",
        ]
        # 1. 檢測資料庫實體連通性
        db_ok = False
        try:
            db_res = await self.manager.db._c.execute("SELECT COUNT(*) FROM accounts")
            acc_count = (await db_res.fetchone())[0]
            db_ok = True
            lines.append(f"🗄 資料庫連通：🟢 正常（WAL模式，收錄帳號: {acc_count}）")
        except Exception as e:
            lines.append(f"🗄 資料庫連通：🔴 異常（{e}）")

        # 2. 檢測配置與模型端點
        cfg = self.manager.config
        lines.append(f"🧩 主模型：{cfg.ai_model}")
        lines.append(f"🎯 決策模型：{cfg.decision_model} (端點: {cfg.decision_base_url})")

        # 3. 檢測各帳號 Telegram Client 與群組權限
        data = await self.manager.status()
        accounts = data.get("accounts", [])
        if not accounts:
            lines.append("\n⚠️ 目前無水軍帳號")
            return "\n".join(lines), await self._home_keyboard()

        for a in accounts:
            name = self._acc_label(a)
            state = a.get("state", "unknown")
            is_run = a.get("is_running", False)
            rep = "開" if a.get("reply_enabled") else "關"
            pro = "開" if a.get("proactive_enabled") else "關"
            worker = (getattr(self.manager, "workers", {}) or {}).get(str(a["id"]))
            
            # 真實連線與群組權限核查
            tg_connected = False
            groups_count = 0
            if worker and worker.tg_client:
                tg_connected = bool(getattr(worker.tg_client, "is_connected", lambda: False)())
                groups_count = len(getattr(worker, "selected_groups", set()))

            status_icon = "🟢" if tg_connected and is_run else "🔴"
            lines.append(
                f"\n{status_icon} 【{name}】\n"
                f"  - Telegram: {'連線正常' if tg_connected else '未連線/離線'}｜群組範圍: {groups_count} 個\n"
                f"  - 開關狀態: 回覆[{rep}] 主動[{pro}]"
            )

            stats = a.get("stats", {})
            rep_sent = stats.get("replies_sent", 0)
            pro_sent = stats.get("proactive_sent", 0)
            claimed = stats.get("human_claimed", 0)
            seen = stats.get("images_seen", 0)
            understood = stats.get("images_understood", 0)
            lines.append(
                f"  - 發言統計: 搶答送出 {rep_sent} / 主動 {pro_sent} (命中真人 {claimed})\n"
                f"  - 識圖統計: 見圖 {seen} / 成功理解 {understood}"
            )
            # 診斷報告
            if is_run and rep == "開" and rep_sent == 0:
                lines.append("  💡 診斷: 帳號正常監聽中，尚未觸發回覆或正處於防撞冷卻")

        lines.append("\n📌 備註：付費模型實際對話與額度消耗請至「📈 用量」或使用「💬 試聊預覽」測試。")
        return "\n".join(lines), await self._home_keyboard()

    async def _test_reply_preview(self, args: list[str]) -> str:
        """💬 試聊預覽室：模擬群聊對話，私聊預覽回覆，絕不外發到真實 Telegram 群。"""
        if len(args) < 2:
            return (
                "⚠️ 用法錯誤！請使用：/test <角色名> <模擬訊息>\n"
                "例如：\n"
                "/test 小天後 我只有13公分 我退群好了\n"
                "/test 小小 每天被老闆壓榨好想放鬆"
            )
        bot_target = args[0]
        sim_text = " ".join(args[1:])

        accounts = await self.manager.db.list_accounts()
        target_acc = None
        for a in accounts:
            label = self._acc_label(a)
            if bot_target in label or bot_target == a["id"]:
                target_acc = a
                break
        if not target_acc:
            names = "、".join(self._acc_label(a) for a in accounts)
            return f"⚠️ 找不到角色「{bot_target}」，現有角色：{names}"

        worker = (getattr(self.manager, "workers", {}) or {}).get(str(target_acc["id"]))
        if not worker:
            return f"⚠️ 角色「{self._acc_label(target_acc)}」尚未啟動，請先啟動帳號"

        # 構建沙盒上下文並呼叫模型
        system_prompt = get_system_prompt(worker.persona)
        sim_context = (
            f"最近對話：\n"
            f"[真人測試群友 sender_id=99999] {sim_text}\n"
            f"最新消息：[真人測試群友 sender_id=99999]（身分未驗證） {sim_text}\n"
            f"回覆指引：台灣繁體口語短訊，自然接話或成人調情，40字以內，結尾不帶句號。"
        )
        try:
            raw = await worker._call_ai(system_prompt, sim_context, purpose="test_preview")
            reply = (raw or "").strip()
            return (
                f"💬 【試聊預覽室】\n"
                f"──────────────\n"
                f"👤 測試角色：{self._acc_label(target_acc)}\n"
                f"📥 模擬輸入：{sim_text}\n"
                f"📤 角色擬真回覆：\n\n"
                f"👉 **「{reply}」**\n\n"
                f"🔒 此為沙盒預覽測試，未發送至任何群組。"
            )
        except Exception as e:
            return f"⚠️ 測試回覆生成失敗：{e}"

    async def _send_as_account(self, args: list[str]) -> str:
        """✉️ 控制指定水軍帳號向其所在群發送一條訊息。"""
        if len(args) < 2:
            return (
                "⚠️ 用法：/send <角色名/帳號ID> [群組ID] <訊息內容>\n"
                "例如：\n"
                "/send 小天後 哥哥們晚上好呀\n"
                "/send 小小 -1002229799107 今晚好冷有人要吃火鍋嗎"
            )
        target_name = args[0]
        accounts = await self.manager.db.list_accounts()
        target_acc = None
        for a in accounts:
            label = self._acc_label(a)
            if target_name in label or target_name == a["id"]:
                target_acc = a
                break
        if not target_acc:
            names = "、".join(self._acc_label(a) for a in accounts)
            return f"⚠️ 找不到帳號「{target_name}」，現有帳號：{names}"

        worker = (getattr(self.manager, "workers", {}) or {}).get(str(target_acc["id"]))
        if not worker or not worker.is_running or not worker.tg_client:
            return f"⚠️ 帳號「{self._acc_label(target_acc)}」未在運行中，請先啟動上線"

        # 判斷是否有給群組 ID
        chat_id = None
        content_tokens = args[1:]
        try:
            potential_gid = int(content_tokens[0])
            if potential_gid < 0:
                chat_id = potential_gid
                content_tokens = content_tokens[1:]
        except (ValueError, IndexError):
            pass

        if not chat_id:
            # 沒指定群組，預設取該帳號的第一個群組
            if not worker.selected_groups:
                return f"⚠️ 帳號「{self._acc_label(target_acc)}」尚未綁定任何群組"
            chat_id = next(iter(worker.selected_groups))

        msg_text = " ".join(content_tokens).strip()
        if not msg_text:
            return "⚠️ 請輸入要發送的訊息內容"

        # 統一走底層安全門（時段、急停、活動開關、長度、群組）
        if not worker._is_allowed_active_hour():
            return "⚠️ 發送被拒絕：目前處於夜間禁言時段（22:00～10:00）"
        if not worker.is_running:
            return "⚠️ 發送被拒絕：帳號已處於停止狀態"
        if int(chat_id) not in worker.selected_groups:
            return f"⚠️ 發送被拒絕：目標群組 {chat_id} 已不在該帳號允許範圍內"
        if len(msg_text) > 55:
            return f"⚠️ 發送被拒絕：訊息長度（{len(msg_text)}字）超過安全上限 55 字"

        # 同群原子鎖排隊（解決手動與自動發言並發撞車）
        send_lock = worker.group_send_locks.setdefault(int(chat_id), asyncio.Lock())
        async with send_lock:
            try:
                sent_msg = await worker.tg_client.send_message(int(chat_id), msg_text)
                mid = getattr(sent_msg, "id", "ok")
                worker._record_sent_text(int(chat_id), msg_text)
                return (
                    f"✅ 【發送成功】\n"
                    f"──────────────\n"
                    f"發送帳號：{self._acc_label(target_acc)}（{target_acc['id']}）\n"
                    f"目標群組：{chat_id}\n"
                    f"訊息 ID：{mid}\n"
                    f"內容：{msg_text}"
                )
            except Exception as e:
                return f"⚠️ 發送失敗：{e}"

    async def _persona_edit(self, account_id: str) -> str:
        """人設安全編輯說明：白名單欄位，密鑰類不會被存進來。"""
        acc = await self.manager.db.get_account(account_id)
        if not acc:
            return f"找不到帳號 {account_id}"
        return (
            f"✏️ 人設編輯｜{self._acc_label(acc)}（{account_id}）\n"
            "──────────────\n"
            "回：/personasave " + account_id + " {JSON}\n"
            "只收白名單欄位（其餘忽略）：\n"
            + ", ".join(self._PERSONA_FIELDS) + "\n"
            "（API key/密碼不在此列，傳錯也不會被存）"
        )

    async def _persona_save(self, account_id: str, raw: str) -> str:
        """/personasave 的核心：JSON 解析→白名單→（優先走 manager.update_persona
        的 fixed-persona 保護）→存 db→運行中 worker 即時刷。"""
        try:
            data = json.loads(raw)
        except Exception as e:
            return f"⚠️ 不是 JSON：{e}"
        if not isinstance(data, dict):
            return "⚠️ 人設要 JSON 物件（{...}）"
        allowed = {k: v for k, v in data.items() if k in self._PERSONA_FIELDS}
        if not allowed:
            return "⚠️ 沒有有效欄位（白名單：" + ", ".join(self._PERSONA_FIELDS[:6]) + "…）"
        acc = await self.manager.db.get_account(account_id)
        if not acc:
            return f"找不到帳號 {account_id}"
        persona = acc.get("persona") or {}
        if isinstance(persona, str):
            try:
                persona = json.loads(persona)
            except Exception:
                persona = {}
        persona = dict(persona)
        persona.update(allowed)
        # 優先走 manager.update_persona：複用 fixed live-test / reconciliation
        # 的「受保護帳號不得改人設」＋雙重檢查＋lifecycle lock（#5）。
        # 受保護時回 None＝拒絕（不回傳成功）；一般帳號回 dict＝成功。
        up = getattr(self.manager, "update_persona", None)
        if callable(up):
            result: dict | None = await up(account_id, persona)  # type: ignore[union-attr]
            if result is None:
                return (
                    f"⚠️ {account_id} 人設受保護（live test／reconciliation 進行中），"
                    "未改動"
                )
        else:
            # manager 沒有 update_persona（舊 fake／簡化服務）：直接 db 寫＋刷 worker
            await self.manager.db.update_account(
                account_id, persona=json.dumps(persona, ensure_ascii=False)
            )
            worker = (
                (getattr(self.manager, "workers", {}) or {}).get(str(account_id))
            )
            if worker is not None and hasattr(worker, "persona"):
                worker.persona = persona
            result = persona
        dropped = sorted(set(data) - set(self._PERSONA_FIELDS))
        extra = (f"\n忽略非白名單欄位：{', '.join(dropped)}") if dropped else ""
        worker = (
            (getattr(self.manager, "workers", {}) or {}).get(str(account_id))
        )
        return (
            f"✅ 已更新 {account_id} 人設（{len(allowed)} 欄位）{extra}\n"
            + (
                "運行中 worker 已即時生效"
                if worker is not None
                else "啟動時生效"
            )
        )

    async def _persona_save_cmd(self, args: list[str]) -> str:
        if not args:
            return "用法：/personasave <帳號> {JSON}"
        return await self._persona_save(args[0], " ".join(args[1:]))

    async def _gsearch(self, args: list[str], event):
        """/gsearch <帳號> [關鍵字]：群組勾選頁搜尋（空關鍵字＝清除）。回 (文字,鍵盤)。"""
        if not args:
            return "用法：/gsearch <帳號> [關鍵字]", self._reply_menu
        admin = str(getattr(event, "sender_id", "") or "")
        chat = int(getattr(event, "chat_id", 0) or 0)
        key = (admin, chat, args[0])
        st = self._group_state(key)
        st["q"] = " ".join(args[1:]).strip()
        st["page"] = 0
        return await self._groups_menu(args[0], admin, chat)

    async def _acc_logs(self, account_id: str, n: int = 15) -> str:
        """帳號最近收發紀錄（#10 日誌頁）。"""
        acc = await self.manager.db.get_account(account_id)
        name = self._acc_label(acc) if acc else account_id
        try:
            cur = await self.manager.db._c.execute(
                "SELECT group_id, sender_name, role, content, timestamp "
                "FROM messages WHERE account_id = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (str(account_id), n),
            )
            rows = list(await cur.fetchall())
        except Exception as e:
            return f"⚠️ 讀日誌失敗：{e}"
        rows = list(reversed(rows))  # 舊→新
        lines = [f"📜 {name} 最近 {len(rows)} 則", "──────────────"]
        for r in rows:
            ts = _fmt_ts(float(r[4] or 0))
            role = "水軍" if str(r[2]) == "assistant" else "真人"
            lines.append(
                f"[{ts}] {role}·{str(r[1] or '?')[:10]}@{r[0]}："
                f"{str(r[3] or '')[:60]}"
            )
        if not rows:
            lines.append("（還沒有紀錄）")
        return "\n".join(lines)

    def _find_account_sync(self, accounts: list[dict], target: str) -> dict | None:
        return next(
            (
                a
                for a in accounts
                if str(a.get("id")) == target or str(a.get("name") or "") == target
            ),
            None,
        )

    async def _acct_list(self) -> str:
        accounts = await self.manager.db.list_accounts()
        if not accounts:
            return "還沒有水軍帳號"
        workers = getattr(self.manager, "workers", {}) or {}
        lines = ["水軍帳號", "──────────────"]
        for a in accounts:
            persona = {}
            if isinstance(a.get("persona"), str):
                try:
                    import json

                    persona = json.loads(a["persona"])
                except Exception:
                    persona = {}
            worker = workers.get(str(a.get("id")))
            running = bool(worker.is_running) if worker else False
            st = "🟢" if running else "⚪"
            on = "啟用" if a.get("enabled") else "停用"
            lines.append(
                f"{st} {self._acc_label(a)}（{persona.get('name', '')}）[{on}]"
            )
        lines.append("")
        lines.append("控制：/startacc、/stopacc、/deleteacc <帳號>")
        return "\n".join(lines)

    async def _acc_toggle(self, name: str, args: list[str]) -> str:
        if not args:
            # 按鍵點「▶ 啟動」「⏹ 停止」不帶帳號：回帳號清單讓主人挑
            return await self._acct_list()
        target = args[0]
        accounts = await self.manager.db.list_accounts()
        acc = self._find_account_sync(accounts, target)
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}"
        if name in {"startacc", "run"}:
            err = await self.manager.start(acc["id"])
            verb = "啟動"
        else:
            err = await self.manager.stop(acc["id"])
            verb = "停止"
        if err:
            return f"⚠️ {verb}失敗：{err}"
        mark = "🟢" if verb == "啟動" else "⚪"
        return f"{mark} {acc.get('name')} 已{verb}"

    async def _acc_delete(self, args: list[str], event):
        # #3：所有入口（/deleteacc 文字、inline 按鈕）共用一次性、有期限、
        # 綁 管理員/聊天/帳號 的確認，取消立即失效、重複點不會二次刪。
        if not args:
            return (await self._acct_list()) + "\n\n刪除：/deleteacc <帳號>", self._reply_menu
        target = args[0]
        accounts = await self.manager.db.list_accounts()
        acc = self._find_account_sync(accounts, target)
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}", self._reply_menu
        admin = str(getattr(event, "sender_id", "") or "")
        chat = int(getattr(event, "chat_id", 0) or 0)
        return await self._acc_delete_confirm(acc["id"], admin, chat)

    async def _feature_toggle(self, name: str, args: list[str]) -> str:
        # 三個獨立開關：
        #   media   ＝ 媒體識別（看懂別人發的圖／影片）
        #   imagegen＝ 圖片生成（水軍被要求時生成圖片/影片發出去）
        #   voice   ＝ 語音
        # 按鍵不帶參數＝切換；帶 on/off＝設成該值
        current = self.manager.feature_status()
        key = {"media": "media_enabled", "imagegen": "media_generation_enabled",
               "voice": "voice_enabled"}[name]
        if args:
            on = args[0].lower() in {"on", "1", "true", "開", "開啟"}
            off = args[0].lower() in {"off", "0", "false", "關", "關閉"}
            if not (on or off):
                return "開關值用 on / off"
        else:
            on = not bool(current[key])
        media_enabled = on if name == "media" else current["media_enabled"]
        voice_enabled = on if name == "voice" else current["voice_enabled"]
        imagegen = on if name == "imagegen" else current["media_generation_enabled"]
        err = await self.manager.update_feature_flags(
            media_enabled=media_enabled,
            voice_enabled=voice_enabled,
            media_generation_enabled=imagegen,
        )
        if err:
            return f"⚠️ {err}"
        st = self.manager.feature_status()
        return (
            f"🖼 媒體識別：{'開啟' if st['media_enabled'] else '關閉'}｜"
            f"📷 圖片生成：{'開啟' if st['media_generation_enabled'] else '關閉'}｜"
            f"🔊 語音：{'開啟' if st['voice_enabled'] else '關閉'}"
        )

    # ---------- 新增帳號（TG 驗證碼流程） ----------

    async def _add_start(self, args: list[str], event):
        if self.login_service is None:
            return "⚠️ 登入服務未初始化", self._reply_menu
        # 有 Mini App：優先讓主人按按鈕在 Mini App 填（免在聊天傳驗證碼/密碼）。
        # token 綁定 管理員＋聊天：別人拿到同一連結也對不上流程。
        admin = str(getattr(event, "sender_id", "") or "")
        chat = int(getattr(event, "chat_id", 0) or 0)
        if args:
            # 帶手機號：先發驗證碼並把 auth_id 綁進此 token 的 flow
            token = self._mini_mint(admin, chat)
            try:
                r = await self.login_service.start(args[0])
            except Exception as e:
                self._mini_flows.pop(token, None)
                return f"⚠️ {e}", self._reply_menu
            flow = self._mini_flows.get(token)
            if flow and isinstance(r, dict) and r.get("auth_id"):
                flow.auth_id = str(r["auth_id"])
                flow.stage = "code"
            return (
                f"驗證碼已傳送至 {r.get('phone_hint', args[0])}（5 分鐘內有效）\n"
                "按下面「📱 開 Mini App」輸入驗證碼繼續",
                self._mini_keyboard_token(token, admin, chat),
            )
        token = self._mini_mint(admin, chat)
        return (
            "➕ 新增帳號：按「📱 開 Mini App」填手機號＋驗證碼＋名稱\n"
            "（全程在 Mini App 內，不會把驗證碼/密碼傳到聊天）",
            self._mini_keyboard_token(token, admin, chat),
        )

    def _mini_keyboard_token(self, token: str, admin_id: str = "",
                             chat_id: int = 0):
        """Mini App 入口按鈕：url 帶 token（?t=...，30 分鐘有效）。

        #1 入口選擇說明（fail-closed）：
        - 現行用 KeyboardButtonUrl（普通連結，telethon 1.44 無 WebAppInfo 頂層
          型別、KeyboardButton 不吃 web_app 參數）。TG 客戶端「內」開啟時
          telegram-web-app.js 仍會注入 initData（HMAC 簽章＋auth_date），
          _flow_for 認 initData 為唯一可信身分；瀏覽器裸開（無 initData）
          會被 fail-closed 拒掉。
        - URL 的 admin/chat 只作「簽發時」綁進 token 的上下文（_mini_mint），
          _flow_for 不再拿 URL 的 admin/chat 當身分（防偽造）；它只是
          讓 initData 缺失時仍能綁定到正確流程的輔助欄位。
        - 要進一步強化可改用「受支持且能取得可信 initData 的入口」
          （Telegram 正式 Web App / 受保護 web 登入），此處先用
          initData 簽章驗證達成等價安全，不新增持續權限或密鑰。
        """
        base = getattr(self.settings, "dashboard_public_url", "") or (
            "http://localhost:" + str(self.settings.dashboard_port)
        )
        url = f"{base}/tg/addacc?t={token}"
        if admin_id:
            url += f"&admin={admin_id}&chat={chat_id}"
        return types.ReplyInlineMarkup(
            kbrows([[types.KeyboardButtonUrl("📱 開 Mini App 新增帳號", url)]])
        )

    async def _add_code(self, args: list[str], event) -> str:
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        if not args:
            return "用法：/code 12345"
        auth_id = await self._last_auth_id()
        if not auth_id:
            return "⚠️ 先 /addacc 發驗證碼"
        try:
            r = await self.login_service.submit_code(auth_id, args[0])
        except Exception as e:
            return f"⚠️ {e}"
        if r.get("status") == "password_required":
            return "需要兩步驗證密碼：/pass xxx"
        if r.get("status") == "authorized":
            return "✅ 驗證成功！回：/name 台北-美玲（帳號名稱）"
        return f"狀態：{r.get('status')}"

    async def _add_pass(self, args: list[str], event) -> str:
        # #5：兩步密碼不再進聊天（會留在 TG 訊息紀錄），改在 Mini App 密碼頁輸入
        return ("🔑 兩步驗證密碼請在 Mini App 內輸入（別傳到聊天）\n"
                "按上面「📱 開 Mini App」→ 密碼頁輸入。")

    async def _add_name(self, args: list[str], event) -> str:
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        auth_id = await self._last_auth_id()
        if not auth_id:
            return "⚠️ 先完成 /addacc → /code 流程"
        name = args[0] if args else "水軍帳號"
        try:
            verified = await self.login_service.claim(auth_id)
        except Exception as e:
            return f"⚠️ {e}"
        account = await self.manager.add_account(
            name, verified.session_string, enable=False,
            display_name=str(getattr(verified, "tg_name", "") or ""),
        )
        await self.manager.db.update_account(
            account["id"],
            tg_user_id=verified.tg_user_id,
            tg_username=str(getattr(verified, "tg_name", "") or ""),
            avatar=str(getattr(verified, "avatar", "") or ""),
            enabled=0,
        )
        return (
            f"✅ 已建立 {account['name']}（{verified.tg_name}），暫不啟動。\n"
            f"先設人設與群組，再 /startacc {account['name']} 上線。"
        )

    async def _last_auth_id(self) -> str:
        """新增流程是單使用者（管理者）串行的，用最近一次 start 的 auth_id。"""
        pending = getattr(self.login_service, "pending", {}) or {}
        if not pending:
            return ""
        latest = max(pending.values(), key=lambda p: p.created_at)
        return latest.auth_id


def make_bot(settings: Settings, manager: AccountManager,
             login_service=None) -> TgControlBot | None:
    if settings.bot_token:
        return TgControlBot(settings, manager, login_service)
    return None
