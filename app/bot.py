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
import json
import logging
import re
import secrets
import time

from telethon import TelegramClient, events, types
from telethon.tl.functions.bots import SetBotCommandsRequest
from fastapi import Request

from .config import Settings
from .manager import AccountManager

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
  <input id="pass" type="text" placeholder="cloud password" autocomplete="off">
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

async function doStart(){
  const phone = document.getElementById('phone').value.trim();
  if(!phone){setMsg('m1','請輸入手機號碼','err');return;}
  busy('b1', true); setMsg('m1','傳送中…');
  try{
    const r = await post('start', {phone});
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
    const r = await post('code', {code});
    if(r.status === 'password_required'){
      setMsg('m2','需要兩步驗證密碼','ok'); show(3);
    }else if(r.status === 'authorized'){
      setMsg('m2','✅ 驗證成功！請輸入帳號名稱','ok'); show(4);
    }else{
      setMsg('m2','狀態：'+r.status, 'err');
    }
  }catch(e){ setMsg('m2','⚠️ '+e.message, 'err'); }
  finally{ busy('b2', false); }
}
async function doPass(){
  const password = document.getElementById('pass').value.trim();
  if(!password){setMsg('m3','請輸入密碼','err');return;}
  busy('b3', true); setMsg('m3','驗證中…');
  try{
    const r = await post('pass', {password});
    if(r.status === 'authorized'){
      setMsg('m3','✅ 驗證成功！請輸入帳號名稱','ok'); show(4);
    }else{ setMsg('m3','狀態：'+r.status, 'err'); }
  }catch(e){ setMsg('m3','⚠️ '+e.message, 'err'); }
  finally{ busy('b3', false); }
}
async function doClaim(){
  const name = document.getElementById('name').value.trim() || '水軍帳號';
  busy('b4', true); setMsg('m4','建立中…');
  try{
    const r = await post('claim', {name});
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
        # 群組勾選的暫存選中狀態（callback 無狀態，用記憶體暫存）
        self._group_selections: dict[str, set[int]] = {}
        # Mini App（新增帳號）用的臨時令牌：{token: exp_ts}
        self._mini_tokens: dict[str, float] = {}
        if not self.admin_ids:
            log.warning("BOT_ADMIN_IDS 為空：任何 TG 使用者都能操作 bot")
        self.app = self._build_mini_app()

    # ---------- 生命週期 ----------

    async def run(self) -> None:
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
            # 不打到 stderr 就沉底（main 只在 shutdown 才 await 這個 task）
            import traceback

            traceback.print_exc()
            print(f"⚠️ bot 啟動失敗：{type(e).__name__}: {e}", flush=True)
        finally:
            try:
                if self._client:
                    await self._client.disconnect()
            except Exception:
                pass

    # ---------- Mini App（新增帳號，免在聊天傳驗證碼/密碼） ----------

    _MINI_TOKEN_TTL = 1800  # 30 分鐘

    def _build_mini_app(self):
        """一組 HTTP 路由：/tg/addacc 開 Mini App，/tgapi/* 是它後端。

        回 APIRouter，由 main.py 掛到 dashboard.app（同一個 uvicorn 對外）。
        令牌走 query string（?t=...），由 bot 按「➕ 新增帳號」時簽發；
        同一個 token 綁定一筆新增流程，避免多人同時加帳號互相搶 pending。
        """
        from fastapi import APIRouter, Request
        from fastapi.responses import HTMLResponse, JSONResponse

        router = APIRouter()

        def _tok(req: Request) -> str:
            return str(req.query_params.get("t", ""))

        def _tok_valid(req: Request) -> bool:
            t = _tok(req)
            exp = self._mini_tokens.get(t)
            if not exp:
                return False
            import time as _t

            if _t.time() > exp:
                self._mini_tokens.pop(t, None)
                return False
            return True

        @router.get("/tg/addacc", response_class=HTMLResponse)
        async def mini_page(req: Request):
            if not _tok_valid(req):
                return HTMLResponse("<h3>令牌過期，請回到 bot 再按「➕ 新增帳號」</h3>")
            return HTMLResponse(_MINI_HTML)

        @router.post("/tgapi/start")
        async def start(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            if not _tok_valid(req):
                return JSONResponse({"error": "令牌無效"}, status_code=400)
            data = await req.json()
            phone = str(data.get("phone", "")).strip()
            if not phone:
                return JSONResponse({"error": "請輸入手機號碼"}, status_code=400)
            try:
                r = await self.login_service.start(phone)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse(r)

        @router.post("/tgapi/code")
        async def code(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            if not _tok_valid(req):
                return JSONResponse({"error": "令牌無效"}, status_code=400)
            data = await req.json()
            code_v = str(data.get("code", "")).strip()
            aid = await self._mini_auth_id()
            if not aid:
                return JSONResponse({"error": "先發驗證碼"}, status_code=400)
            try:
                r = await self.login_service.submit_code(aid, code_v)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse(r)

        @router.post("/tgapi/pass")
        async def pas(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            if not _tok_valid(req):
                return JSONResponse({"error": "令牌無效"}, status_code=400)
            data = await req.json()
            pw = str(data.get("password", "")).strip()
            aid = await self._mini_auth_id()
            if not aid:
                return JSONResponse({"error": "流程不存在"}, status_code=400)
            try:
                r = await self.login_service.submit_password(aid, pw)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse(r)

        @router.post("/tgapi/claim")
        async def claim(req: Request):
            if self.login_service is None:
                return JSONResponse({"error": "登入服務未初始化"}, status_code=500)
            if not _tok_valid(req):
                return JSONResponse({"error": "令牌無效"}, status_code=400)
            data = await req.json()
            name = str(data.get("name", "")).strip() or "水軍帳號"
            aid = await self._mini_auth_id()
            if not aid:
                return JSONResponse({"error": "流程不存在"}, status_code=400)
            try:
                verified = await self.login_service.claim(aid)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)
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
            self._mini_tokens.pop(_tok(req), None)
            return JSONResponse({"ok": True, "account": account,
                                 "tg_name": str(getattr(verified, "tg_name", ""))})

        return router

    async def _mini_auth_id(self) -> str:
        """Mini App 新增流程用同一個 auth_id（與 /addacc 串行的最近一次 start）。"""
        return await self._last_auth_id()

    # ---------- 權限 ----------

    def _allowed(self, event) -> bool:
        if not self.admin_ids:
            return True
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
                body, kb = await self._acc_delete(rest), self._reply_menu
            elif name in {"media", "voice", "imagegen"}:
                body, kb = await self._feature_toggle(name, rest), self._reply_menu
            elif name == "addacc":
                body, kb = await self._add_start(rest, event)
            elif name == "code":
                body, kb = await self._add_code(rest, event), self._reply_menu
            elif name == "pass":
                body, kb = await self._add_pass(rest, event), self._reply_menu
            elif name == "name":
                body, kb = await self._add_name(rest, event), self._reply_menu
            else:
                body, kb = f"不認識的指令「/{name}」\n" + self._help(), self._reply_menu
            await event.reply(body[:MSG_LIMIT], buttons=kb)
        except Exception as e:
            log.exception("bot 指令 %r 失敗", text)
            await event.reply(f"⚠️ {type(e).__name__}: {e}",
                              buttons=self._reply_menu)

    # ---------- 內嵌按鈕（callback） ----------

    async def _on_callback(self, cb) -> None:
        raw = getattr(cb, "data", None)
        if isinstance(raw, bytes):
            data = raw.decode()
        else:
            data = str(raw or "")
        # 權限：callback 的 sender 是按下按鈕的人；非管理者按按鈕給提示
        if not self._allowed(cb):
            try:
                await cb.answer("⚠️ 不是管理者（BOT_ADMIN_IDS 未包含你）", show_alert=True)
            except Exception:
                pass
            return
        try:
            await self._dispatch_callback(cb, data)
        except Exception as e:
            log.exception("callback %r 失敗", data)
            try:
                await cb.answer(f"⚠️ {type(e).__name__}: {e}")
            except Exception:
                pass

    async def _dispatch_callback(self, cb, data: str) -> None:
        """內嵌按鈕路由：data 格式「action[:arg]」。"""
        if not data:
            await cb.answer()
            return
        action, _, arg = data.partition(":")

        if action == "home":
            await cb.edit(
                await self._home_text(), buttons=await self._home_keyboard()
            )
        elif action == "status":
            await cb.edit(
                (await self._status())[:MSG_LIMIT],
                buttons=await self._home_keyboard(),
            )
        elif action == "groups":
            await cb.edit(
                (await self._groups([]))[:MSG_LIMIT],
                buttons=await self._home_keyboard(),
            )
        elif action == "help":
            await cb.edit(
                self._help()[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc":
            # 選了某帳號 → 該帳號的「可操作功能」內嵌菜單
            body, kb = await self._account_actions_keyboard(arg)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.start":
            body = await self._acc_toggle("startacc", [arg])
            await cb.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc.stop":
            body = await self._acc_toggle("stopacc", [arg])
            await cb.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc.del":
            # 先給確認頁（避免誤觸直接刪），按「確認」才真的刪
            body, kb = await self._acc_delete_confirm(arg)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.del.yes":
            body = await self._acc_delete([arg])
            await cb.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "acc.del.no":
            body, kb = await self._account_actions_keyboard(arg)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "acc.groups":
            body, kb = await self._groups_menu(arg)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        elif data.startswith("g.save:"):
            # g.save:<acc_id> 保存勾選（要先於一般 g: 分支）
            _, acc_id = data.split(":", 1)
            body = await self._groups_save(acc_id)
            await cb.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif data.startswith("g:"):
            # g:<gid>:<acc_id> 切換某群組勾選
            _, gid_s, acc_id = data.split(":", 2)
            try:
                gid = int(gid_s)
            except ValueError:
                await cb.answer("群組 id 格式錯誤", show_alert=True)
                return
            sel = self._group_selections.setdefault(acc_id, set())
            if gid in sel:
                sel.discard(gid)
            else:
                sel.add(gid)
            body, kb = await self._groups_menu(acc_id)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "model":
            body = self._model_info()
            await cb.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "usage":
            body = await self._usage_info()
            await cb.edit(body[:MSG_LIMIT], buttons=await self._home_keyboard())
        elif action == "acc.persona":
            body, kb = await self._persona_actions_keyboard(arg)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        elif action == "priv":
            body = await self._privates([arg])
            await cb.edit(
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
            await cb.edit(
                body[:MSG_LIMIT], buttons=await self._home_keyboard()
            )
        elif action == "addacc":
            await cb.edit(
                "➕ 新增帳號\n把 Telegram 手機號傳給我，例如 +886912345678",
                buttons=await self._home_keyboard(),
            )
        elif action == "hidekb":
            await cb.edit(
                "⌨ 已隱藏下方快速鍵盤（訊息內按鈕仍可用，再按「📊 狀態」重建）",
                buttons=types.ReplyKeyboardHide(),
            )
            await cb.answer("下方鍵盤已隱藏")
        elif action == "persona.show":
            body = await self._persona_show(arg)
            await cb.edit(
                body[:MSG_LIMIT], buttons=self._persona_keyboard(arg)
            )
        elif action == "persona.regen":
            body = await self._persona_regen(arg)
            await cb.edit(
                body[:MSG_LIMIT], buttons=self._persona_keyboard(arg)
            )
        elif action == "back.home":
            await cb.edit(
                await self._home_text(), buttons=await self._home_keyboard()
            )
        elif action == "back.acc":
            body, kb = await self._account_actions_keyboard(arg)
            await cb.edit(body[:MSG_LIMIT], buttons=kb)
        else:
            await cb.answer("未處理的按鈕", show_alert=True)

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
        rows = [
            [
                cb(
                    toggle_action + ":" + account_id, toggle_txt
                ),
                cb(
                    "acc.persona:" + account_id, "🧬 人設"
                ),
            ],
            [
                cb(
                    "acc.groups:" + account_id, "📍 群組"
                ),
                cb(
                    "priv:" + account_id, "📩 私訊"
                ),
            ],
            [
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
            "/addacc +886…5678 傳驗證碼\n"
            "/code 12345 輸入驗證碼\n"
            "/pass xxx 兩步驗證密碼（若需要）\n"
            "/name 台北-美玲 建立帳號（暫不啟動）\n"
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

    async def _acc_delete_confirm(self, account_id: str):
        """刪除確認頁：回 (文字, 鍵盤)。"""
        acc = next(
            (
                a
                for a in (await self.manager.status()).get("accounts", [])
                if a["id"] == account_id
            ),
            None,
        )
        name = self._acc_label(acc) if acc else account_id
        body = (
            f"🗑 確認刪除｜{name}（{account_id}）\n"
            "──────────────\n"
            "刪除後人設、記憶、收發紀錄一併清掉（session 也要重新登入）。"
        )
        rows = [
            [
                cb("acc.del.yes:" + account_id, "✅ 確認刪除"),
                cb("acc.del.no:" + account_id, "↩️ 再想想"),
            ],
        ]
        return body, types.ReplyInlineMarkup(kbrows(rows))

    async def _groups_menu(self, account_id: str):
        """群組勾選頁：回 (文字, 鍵盤)。勾選狀態暫存 self._group_selections。"""
        data = await self.manager.status()
        acc = next(
            (a for a in data.get("accounts", []) if a["id"] == account_id), None
        )
        if acc is None:
            return f"找不到帳號 {account_id}", types.ReplyInlineMarkup(
                kbrows([[cb("back.home", "🔙 回主菜單")]])
            )
        name = self._acc_label(acc)
        selected_now = set(acc.get("groups") or [])
        if account_id not in self._group_selections:
            # 首次進入：預設＝目前已設定的群組
            self._group_selections[account_id] = set(selected_now)
        sel = self._group_selections[account_id]
        # 群組清單：優先用 worker 的（運行中），沒有才臨時抓
        available = acc.get("groups_available") or []
        if not available:
            available, err = await self.manager.list_available_groups(account_id)
        lines = [
            f"📍 群組勾選｜{name}（{account_id}）",
            "──────────────",
            f"勾選要讓這個帳號駐留的群組（已選 {len(sel)} 個）",
        ]
        if not available:
            lines.append("（還沒有任何群組資料，先讓帳號上線收過訊息）")
        rows = []
        for g in available[:30]:
            gid = int(g["id"])
            title = str(g.get("title") or f"群組 {gid}")[:24]
            mark = "✅" if gid in sel else "▫️"
            rows.append([cb(f"g:{gid}:{account_id}", f"{mark} {title}")])
        rows.append([
            cb("g.save:" + account_id, "💾 保存群組"),
            cb("back.acc:" + account_id, "🔙 回帳號操作"),
        ])
        return "\n".join(lines), types.ReplyInlineMarkup(kbrows(rows))

    async def _groups_save(self, account_id: str) -> str:
        sel = self._group_selections.get(account_id)
        if sel is None:
            return "⚠️ 勾選狀態不存在，請重新進入群組頁"
        err = await self.manager.save_groups(account_id, sorted(sel))
        if err:
            return f"⚠️ 保存失敗：{err}"
        self._group_selections.pop(account_id, None)
        return (
            f"💾 已保存 {len(sel)} 個群組（{account_id}）。\n"
            "運行中的帳號重啟後生效。"
        )

    def _model_info(self) -> str:
        cfg = self.manager.config
        lines = [
            "🧩 目前模型設定",
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
            "改模型＝改 Railway 環境變數（AI_MODEL 等）＋重新部署。",
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

    async def _acc_delete(self, args: list[str]) -> str:
        if not args:
            return (await self._acct_list()) + "\n\n刪除：/deleteacc <帳號>"
        target = args[0]
        accounts = await self.manager.db.list_accounts()
        acc = self._find_account_sync(accounts, target)
        if acc is None:
            names = "、".join(str(a.get("name")) for a in accounts)
            return f"找不到帳號「{target}」；現有：{names}"
        err = await self.manager.delete(acc["id"])
        if err:
            return f"⚠️ 刪除失敗：{err}"
        return f"🗑️ 已刪除 {acc.get('name')}（含記憶）"

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
        # 有 Mini App：優先讓主人按按鈕在 Mini App 填（免在聊天傳驗證碼/密碼）
        if args:
            # 直接帶手機號：照舊走 start 發驗證碼
            try:
                r = await self.login_service.start(args[0])
            except Exception as e:
                return f"⚠️ {e}", self._reply_menu
            return (
                f"驗證碼已傳送至 {r.get('phone_hint')}（5 分鐘內有效）\n"
                f"按下面「📱 開 Mini App 輸入驗證碼」繼續",
                self._mini_app_keyboard(),
            )
        token = secrets.token_urlsafe(16)
        self._mini_tokens[token] = time.time() + self._MINI_TOKEN_TTL
        return (
            "➕ 新增帳號：按「📱 開 Mini App」填手機號＋驗證碼＋名稱\n"
            "（全程在 Mini App 內，不會把驗證碼/密碼傳到聊天）",
            self._mini_app_keyboard(token),
        )

    def _mini_app_keyboard(self, token: str | None = None):
        """Mini App 入口按鈕：url 帶 token（?t=...），30 分鐘有效。"""
        tok = token or secrets.token_urlsafe(16)
        if token is None:
            self._mini_tokens[tok] = time.time() + self._MINI_TOKEN_TTL
        base = getattr(self.settings, "dashboard_public_url", "") or (
            "http://localhost:" + str(self.settings.dashboard_port)
        )
        url = f"{base}/tg/addacc?t={tok}"
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
        if self.login_service is None:
            return "⚠️ 登入服務未初始化"
        if not args:
            return "用法：/pass xxx"
        auth_id = await self._last_auth_id()
        if not auth_id:
            return "⚠️ 流程不存在或已過期"
        try:
            r = await self.login_service.submit_password(auth_id, args[0])
        except Exception as e:
            return f"⚠️ {e}"
        if r.get("status") == "authorized":
            return "✅ 驗證成功！回：/name 台北-美玲（帳號名稱）"
        return f"狀態：{r.get('status')}"

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
