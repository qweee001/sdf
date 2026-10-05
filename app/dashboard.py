"""
控制台 - FastAPI Web UI（深色、繁體中文、單文件前端）
功能：登入/登出、帳號狀態、啟動/停止/刪除、新增帳號（TG 登入流程）、
      人設檢視/重新生成、私訊查看、統計
安全：session cookie（HttpOnly + SameSite=Strict + Secure（HTTPS 時））、登入限流、登出路由
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .live_test import LiveTestError
from .manager import AccountManager
from .telegram_login import (
    LoginConflict,
    LoginExpired,
    LoginRateLimit,
    TelegramLoginService,
)

SESSION_COOKIE = "sdf_session"
_SESSION_TTL = 3600
_LOGIN_MAX_ATTEMPTS = 10
_LOGIN_WINDOW = 300.0
# 追蹤的來源位址上限：超過就清掉已過期／最舊的鍵，避免記憶體被灌爆
_MAX_TRACKED_LOGIN_KEYS = 4096


def _trusted_proxy_hops() -> int:
    """前面有幾層可信反向代理；預設 0 ＝不信任任何客戶端可自行填入的前綴標頭。"""
    try:
        hops = int(os.getenv("TRUSTED_PROXY_HOPS", "0").strip() or "0")
    except ValueError:
        return 0
    return hops if hops > 0 else 0


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _cookie_secure(request: Request) -> bool:
    """session cookie 是否加 Secure。

    只有確認連線是 HTTPS 才加：直接是 HTTPS、或運維已聲明 TRUSTED_PROXY_HOPS>0
    且可信反代回報 X-Forwarded-Proto: https、或以 DASHBOARD_COOKIE_SECURE=1 強制
    （例如 Railway 邊緣終結 TLS 時）。純 HTTP 下硬加 Secure 會讓瀏覽器直接丟棄
    整顆 cookie，等於完全無法登入，所以預設跟隨實際協定。
    """
    if _env_flag("DASHBOARD_COOKIE_SECURE"):
        return True
    if request.url.scheme == "https":
        return True
    if _trusted_proxy_hops() > 0:
        proto = request.headers.get("x-forwarded-proto", "")
        # 反代可能串接多層，取最右邊由可信反代寫入的那一段
        if proto.split(",")[-1].strip().lower() == "https":
            return True
    return False


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
        sid = request.cookies.get(SESSION_COOKIE, "")
        if not sid or sid not in self._sessions:
            return False
        if time.time() > self._sessions[sid]:
            self._sessions.pop(sid, None)
            return False
        self._sessions[sid] = time.time() + _SESSION_TTL
        return True

    def _ip(self, request: Request) -> str:
        """登入限流用的來源識別：預設只用 TCP 連線位址。

        絕不無條件相信 X-Forwarded-For／X-Real-IP：這些標頭客戶端可自填，
        攻擊者只要每支請求換一段 XFF 首段就能讓 `_login_attempts` 每次都是
        新鍵，等於完全繞過限流。只有在運維明確聲明 TRUSTED_PROXY_HOPS>0
        （前面有幾層可信反代）時，才往左取過那些可信跳數後的位址。
        """
        peer = request.client.host if request.client else "unknown"
        hops = _trusted_proxy_hops()
        if hops <= 0:
            return peer
        forwarded = [
            p.strip()
            for p in request.headers.get("x-forwarded-for", "").split(",")
            if p.strip()
        ]
        if not forwarded:
            return peer
        # 最右邊 hops 段是可信反代自己寫入的，其左邊那一段才是真實客戶端
        return forwarded[max(0, len(forwarded) - hops)]

    def _rate_limited(self, key: str, max_n: int = _LOGIN_MAX_ATTEMPTS,
                      window: float = _LOGIN_WINDOW) -> bool:
        now = time.time()
        attempts = [t for t in self._login_attempts.get(key, []) if now - t < window]
        if attempts:
            self._login_attempts[key] = attempts
        else:
            # 全數過期就移除鍵，避免只增不刪
            self._login_attempts.pop(key, None)
        if len(self._login_attempts) > _MAX_TRACKED_LOGIN_KEYS:
            self._prune_login_attempts(now, window)
        return len(attempts) >= max_n

    def _prune_login_attempts(self, now: float, window: float) -> None:
        """鍵數量超上限時的清理：先清過期鍵，仍超量就清最舊的鍵。"""
        for stale in [
            k for k, ts in self._login_attempts.items()
            if not ts or now - ts[-1] >= window
        ]:
            self._login_attempts.pop(stale, None)
        overflow = len(self._login_attempts) - _MAX_TRACKED_LOGIN_KEYS
        if overflow > 0:
            oldest = sorted(
                self._login_attempts, key=lambda k: self._login_attempts[k][-1]
            )
            for stale in oldest[:overflow]:
                self._login_attempts.pop(stale, None)

    # ---------- 路由 ----------

    def _setup_routes(self):
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse(PAGE)

        @app.get("/health")
        async def health():
            # Railway healthcheckPath：只回最小公開狀態，不洩漏帳號數等內部資訊
            return {"status": "ok"}

        @app.post("/api/login")
        async def login(request: Request):
            key = self._ip(request)
            if self._rate_limited(key):
                return JSONResponse(
                    {"error": "嘗試次數過多，請 5 分鐘後再試"}, status_code=429
                )
            data = await request.json()
            user = str(data.get("username", "")).strip()
            pwd = str(data.get("password", ""))
            # 常數時間比較，避免字串比較的短路行為洩漏憑證前綴
            user_ok = hmac.compare_digest(
                user.encode("utf-8"),
                str(self.config.dashboard_user).encode("utf-8"),
            )
            pwd_ok = hmac.compare_digest(
                pwd.encode("utf-8"),
                str(self.config.dashboard_pass).encode("utf-8"),
            )
            if not (user_ok and pwd_ok):
                self._login_attempts.setdefault(key, []).append(time.time())
                return JSONResponse({"error": "帳號或密碼錯誤"}, status_code=401)
            # 登入成功即清空此來源的失敗計數
            self._login_attempts.pop(key, None)
            # 會話令牌用 CSPRNG 產生，只存在服務端記憶體，不含任何可預測輸入
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = time.time() + _SESSION_TTL
            resp = JSONResponse({"ok": True, "user": user})
            resp.set_cookie(
                SESSION_COOKIE, sid,
                max_age=_SESSION_TTL, httponly=True, samesite="strict",
                secure=_cookie_secure(request),
            )
            return resp

        @app.post("/api/logout")
        async def logout(request: Request):
            sid = request.cookies.get(SESSION_COOKIE, "")
            self._sessions.pop(sid, None)
            resp = JSONResponse({"ok": True})
            resp.delete_cookie(SESSION_COOKIE)
            return resp

        # ---------- 需登入的 API ----------

        @app.get("/api/status")
        async def status(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            await self.login_service.prune_expired()
            return JSONResponse(await self.manager.status())

        @app.get("/api/groups/{group_id}/messages")
        async def group_messages(group_id: int, request: Request):
            """讀取某群跨所有帳號的實際訊息串（含人類與水軍），供互動分析。"""
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            limit = request.query_params.get("limit", "100")
            try:
                limit = max(1, min(5000, int(limit)))
            except ValueError:
                limit = 100
            rows = await self.manager.db.get_group_messages(group_id, limit)
            return JSONResponse({"group_id": group_id, "count": len(rows), "messages": rows})

        def _persona_name(acc: dict) -> str:
            persona = acc.get("persona")
            if isinstance(persona, str):
                try:
                    persona = json.loads(persona)
                except Exception:
                    persona = None
            if isinstance(persona, dict) and persona.get("name"):
                return str(persona["name"])
            return str(acc.get("name") or acc.get("id") or "")

        @app.get("/api/groups/directory")
        async def groups_directory(request: Request):
            """群組總管：把三個帳號的群組清單、活動量、備註名合成一張表。"""
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            refresh = request.query_params.get("refresh", "").lower() in {"1", "true", "yes"}
            accounts = await self.manager.db.list_accounts()
            account_meta = []
            per_account: dict[str, dict] = {}
            for acc in accounts:
                acc_id = str(acc["id"])
                selected = []
                if acc.get("groups"):
                    try:
                        selected = [int(g) for g in json.loads(acc["groups"])]
                    except Exception:
                        selected = []
                worker = self.manager.workers.get(acc_id)
                if refresh:
                    # 停機帳號也要能探索：list_available_groups 會開唯讀連線
                    try:
                        await self.manager.list_available_groups(acc_id)
                    except Exception:
                        pass
                    worker = self.manager.workers.get(acc_id)
                available = worker.group_list() if worker else []
                per_account[acc_id] = {
                    "selected": set(selected),
                    "available": available,
                }
                account_meta.append(
                    {
                        "id": acc_id,
                        "name": str(acc.get("name") or acc_id),
                        "persona_name": _persona_name(acc),
                        "is_running": bool(worker.is_running) if worker else False,
                    }
                )

            labels = await self.manager.db.get_group_labels()
            overview = {
                row["group_id"]: row for row in await self.manager.db.group_overview()
            }

            table: dict[int, dict] = {}
            for acc_id, info in per_account.items():
                for item in info["available"]:
                    if not isinstance(item, dict) or isinstance(item.get("id"), bool):
                        continue
                    try:
                        gid = int(item.get("id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if gid >= 0:
                        continue
                    entry = table.setdefault(
                        gid, {"id": gid, "title": "", "members": 0, "accounts": []}
                    )
                    title = str(item.get("title") or "")
                    if title and (not entry["title"] or entry["title"].startswith("群組 ")):
                        entry["title"] = title
                    try:
                        members = int(item.get("members") or 0)
                    except (TypeError, ValueError):
                        members = 0
                    entry["members"] = max(entry["members"], members)
                    if gid in info["selected"]:
                        entry["accounts"].append(acc_id)
            # 只有歷史訊息、目前不在任何帳號清單裡的群也要看得到（不然找不到舊群）
            for gid in overview:
                table.setdefault(gid, {"id": gid, "title": "", "members": 0, "accounts": []})

            groups = []
            for gid, entry in table.items():
                row = overview.get(gid, {})
                label = labels.get(gid, "")
                groups.append(
                    {
                        "id": gid,
                        "title": entry["title"] or f"群組 {gid}",
                        "label": label,
                        "display": label or entry["title"] or f"群組 {gid}",
                        "members": entry["members"],
                        "accounts": sorted(entry["accounts"]),
                        "selected_count": len(entry["accounts"]),
                        "msg_count": int(row.get("msg_count") or 0),
                        "last_ts": float(row.get("last_ts") or 0.0),
                        "last_human_ts": float(row.get("last_human_ts") or 0.0),
                        "human_senders": int(row.get("human_senders") or 0),
                    }
                )
            groups.sort(key=lambda g: (-g["last_ts"], g["id"]))
            return JSONResponse({"groups": groups, "accounts": account_meta})

        @app.post("/api/groups/label")
        async def set_group_label(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"error": "格式錯誤"}, status_code=400)
            if not isinstance(data, dict):
                return JSONResponse({"error": "格式錯誤"}, status_code=400)
            try:
                group_id = int(data.get("group_id"))
            except (TypeError, ValueError):
                return JSONResponse({"error": "群組 ID 格式錯誤"}, status_code=400)
            label = data.get("label")
            if label is None:
                label = ""
            if not isinstance(label, str):
                return JSONResponse({"error": "備註名稱格式錯誤"}, status_code=400)
            ok = await self.manager.db.upsert_group_label(group_id, label)
            if not ok:
                return JSONResponse({"error": "備註名稱儲存失敗"}, status_code=400)
            return JSONResponse({"ok": True, "group_id": group_id, "label": label.strip()})

        @app.post("/api/groups/membership")
        async def update_group_membership(request: Request):
            """一次把某個群加入／移出多個帳號（群組總管的一鍵三號）。"""
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"error": "格式錯誤"}, status_code=400)
            if not isinstance(data, dict):
                return JSONResponse({"error": "格式錯誤"}, status_code=400)
            try:
                group_id = int(data.get("group_id"))
            except (TypeError, ValueError):
                return JSONResponse({"error": "群組 ID 格式錯誤"}, status_code=400)
            raw_accounts = data.get("account_ids")
            if not isinstance(raw_accounts, list) or not raw_accounts:
                return JSONResponse({"error": "請選擇帳號"}, status_code=400)
            selected = bool(data.get("selected"))
            results: dict[str, str] = {}
            for acc_id in [str(a) for a in raw_accounts]:
                acc = await self.manager.db.get_account(acc_id)
                if not acc:
                    results[acc_id] = "帳號不存在"
                    continue
                current: list[int] = []
                if acc.get("groups"):
                    try:
                        current = [int(g) for g in json.loads(acc["groups"])]
                    except Exception:
                        current = []
                target = [g for g in current if g != group_id]
                if selected:
                    target.append(group_id)
                if not target:
                    err = await self.manager.save_groups(acc_id, [])
                    results[acc_id] = err or "已停用（沒有任何群組）"
                    continue
                err = await self.manager.save_groups(acc_id, target)
                results[acc_id] = err or "ok"
            return JSONResponse(
                {
                    "ok": all(v == "ok" for v in results.values()),
                    "group_id": group_id,
                    "selected": selected,
                    "results": results,
                }
            )

        @app.post("/api/features")
        async def update_features(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            data = await request.json()
            if not isinstance(data, dict):
                return JSONResponse({"error": "功能設定格式錯誤"}, status_code=400)
            media_enabled = data.get("media_enabled")
            voice_enabled = data.get("voice_enabled")
            if type(media_enabled) is not bool or type(voice_enabled) is not bool:
                return JSONResponse({"error": "功能開關必須是布林值"}, status_code=400)
            error = await self.manager.update_feature_flags(
                media_enabled=media_enabled,
                voice_enabled=voice_enabled,
            )
            if error:
                return JSONResponse({"error": error}, status_code=409)
            return JSONResponse({
                "ok": True,
                "features": self.manager.feature_status(),
            })

        @app.post("/api/live-test/start")
        async def start_live_test(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"error": "測試設定格式錯誤"}, status_code=400)
            if not isinstance(data, dict):
                return JSONResponse({"error": "測試設定格式錯誤"}, status_code=400)
            try:
                result = await self.manager.start_live_test(data)
            except LiveTestError as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            return JSONResponse({"ok": True, "live_test": result})

        @app.get("/api/live-test/status")
        async def live_test_status(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            return JSONResponse({
                "ok": True,
                "live_test": await self.manager.live_test_status(),
            })

        @app.post("/api/live-test/stop")
        async def stop_live_test(request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            result = await self.manager.stop_live_test()
            return JSONResponse({"ok": True, "live_test": result})

        @app.post("/api/accounts/{account_id}/start")
        async def start_account(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            err = await self.manager.start(account_id)
            if err:
                if err == "帳號不存在":
                    return JSONResponse({"ok": False, "error": err}, status_code=404)
                return JSONResponse({"ok": False, "error": err}, status_code=400)
            return JSONResponse({"ok": True})

        @app.post("/api/accounts/{account_id}/stop")
        async def stop_account(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            err = await self.manager.stop(account_id)
            if err == "帳號不存在":
                return JSONResponse({"ok": False, "error": err}, status_code=404)
            return JSONResponse({"ok": True})

        @app.delete("/api/accounts/{account_id}")
        async def delete_account(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            err = await self.manager.delete(account_id)
            if err == "帳號不存在":
                return JSONResponse({"ok": False, "error": err}, status_code=404)
            return JSONResponse({"ok": True})

        @app.post("/api/accounts/{account_id}/toggle")
        async def toggle_account(account_id: str, request: Request):
            """啟用/停用帳號：只改 DB 的 enabled 旗標，不影響目前運行中的 worker。"""
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            acc = await self.manager.db.get_account(account_id)
            if not acc:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            now_enabled = not bool(acc.get("enabled"))
            await self.manager.db.update_account(account_id, enabled=1 if now_enabled else 0)
            return JSONResponse({"ok": True, "enabled": now_enabled})

        @app.post("/api/accounts/{account_id}/features")
        async def update_account_features(account_id: str, request: Request):
            """帳號級別功能開關：reply_enabled（回覆）/ proactive_enabled（主動發言）。"""
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            acc = await self.manager.db.get_account(account_id)
            if not acc:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"error": "格式錯誤"}, status_code=400)
            fields = {}
            for key in ("reply_enabled", "proactive_enabled"):
                if key in data:
                    v = data[key]
                    if type(v) is not bool:
                        return JSONResponse({"error": f"{key} 必須是布林值"}, status_code=400)
                    fields[key] = 1 if v else 0
            if not fields:
                return JSONResponse({"error": "沒有可更新的開關"}, status_code=400)
            await self.manager.db.update_account(account_id, **fields)
            acc2 = await self.manager.db.get_account(account_id)
            return JSONResponse({
                "ok": True,
                "reply_enabled": bool(acc2.get("reply_enabled", 1)),
                "proactive_enabled": bool(acc2.get("proactive_enabled", 1)),
            })

        @app.get("/api/accounts/{account_id}/persona")
        async def get_persona(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
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
                "meetups_done", "schedule", "chat_style",
            }
            clean = {k: v for k, v in persona.items() if k in allowed}
            clean["name"] = str(clean["name"]).strip()
            if not isinstance(clean.get("hobbies"), list):
                clean["hobbies"] = []
            saved = await self.manager.update_persona(account_id, clean)
            if saved is None:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            return JSONResponse({"ok": True, "persona": saved})

        @app.get("/api/accounts/{account_id}/groups/available")
        async def available_groups(account_id: str, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            groups, err = await self.manager.list_available_groups(account_id)
            if err:
                status_code = 404 if err == "帳號不存在" else 400
                return JSONResponse({"error": err}, status_code=status_code)
            return JSONResponse({"groups": groups})

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
            acc = await self.manager.db.get_account(account_id)
            if not acc:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            msgs = await self.manager.db.get_private_messages(account_id, limit=50)
            return JSONResponse({"messages": msgs})

        @app.post("/api/accounts/{account_id}/privates/{msg_id}/read")
        async def mark_read(account_id: str, msg_id: int, request: Request):
            if not self._check_session(request):
                return JSONResponse({"error": "未登入"}, status_code=401)
            acc = await self.manager.db.get_account(account_id)
            if not acc:
                return JSONResponse({"error": "帳號不存在"}, status_code=404)
            await self.manager.db.mark_private_message_read(msg_id, account_id)
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
            return JSONResponse({
                "ok": True,
                "account": {
                    "id": account["id"],
                    "name": account["name"],
                    "tg_user_id": verified.tg_user_id,
                    "enabled": False,
                    "setup_complete": False,
                },
                "started": False,
            })


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
.status-badge.connecting { background: #0ea5e9; color: #fff; }
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
.feature-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.8rem; }
.feature-item { display: flex; justify-content: space-between; align-items: center; gap: 1rem; padding: 0.8rem; background: #0f172a; border-radius: 9px; }
.switch { width: 44px; height: 24px; accent-color: #38bdf8; cursor: pointer; }
.switch:disabled { cursor: not-allowed; opacity: 0.45; }
/* 群組監控 */
.feed { max-height: 420px; overflow-y: auto; display: flex; flex-direction: column; gap: 0.6rem; padding: 0.3rem; }
.feed-item { display: flex; gap: 0.6rem; align-items: flex-start; }
.feed-item .who { width: 130px; flex-shrink: 0; font-size: 0.78rem; color: #94a3b8; line-height: 1.35; }
.feed-item .body { flex: 1; min-width: 0; }
.bubble { max-width: 80%; padding: 0.5rem 0.8rem; border-radius: 10px; font-size: 0.9rem; line-height: 1.4; word-break: break-word; }
.bubble-human { background: #24344d; }
.bubble-bot { background: #1c3a5c; border-left: 3px solid #38bdf8; }
.feed-item .ts { font-size: 0.7rem; color: #64748b; margin-top: 0.15rem; }
.badge { display: inline-block; padding: 0.05rem 0.4rem; border-radius: 4px; font-size: 0.65rem; font-weight: bold; }
.badge-human { background: #16a34a; color: #fff; }
.badge-bot { background: #38bdf8; color: #0f172a; }
.monitor-select { background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; padding: 0.45rem 0.7rem; }
.acc-tag { display: inline-block; padding: 0.05rem 0.4rem; border-radius: 4px; font-size: 0.7rem; font-weight: bold; }
.acc-tag-on { background: #16a34a; color: #fff; }
.acc-tag-off { background: #475569; color: #cbd5e1; }
/* 群組總管 */
.hub-toolbar { display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: center; margin-bottom: 0.8rem; }
.hub-toolbar input[type=text], .hub-toolbar select {
    background: #0f172a; border: 1px solid #334155; border-radius: 8px;
    color: #e2e8f0; padding: 0.45rem 0.7rem; font-size: 0.85rem;
}
.hub-toolbar input[type=text] { flex: 1; min-width: 180px; }
.hub-filter { display: flex; align-items: center; gap: 0.35rem; font-size: 0.8rem; color: #94a3b8; white-space: nowrap; }
.hub-summary { font-size: 0.8rem; color: #94a3b8; margin-bottom: 0.6rem; }
.hub-list { max-height: 56vh; overflow-y: auto; display: flex; flex-direction: column; gap: 0.5rem; padding-right: 0.2rem; }
.hub-row { background: #0f172a; border: 1px solid #24334d; border-radius: 10px; padding: 0.7rem 0.8rem; }
.hub-row.hub-row-on { border-color: #16a34a; }
.hub-row.hub-row-focus { box-shadow: 0 0 0 1px #38bdf8; }
.hub-row-top { display: flex; justify-content: space-between; gap: 0.7rem; align-items: flex-start; flex-wrap: wrap; }
.hub-name { font-size: 0.95rem; font-weight: bold; color: #e2e8f0; word-break: break-all; }
.hub-id { font-size: 0.72rem; color: #64748b; cursor: pointer; }
.hub-id:hover { color: #38bdf8; }
.hub-info { font-size: 0.76rem; color: #94a3b8; line-height: 1.6; margin-top: 0.2rem; }
.hub-actions { display: flex; gap: 0.35rem; flex-wrap: wrap; align-items: center; }
.hub-chip {
    border: 1px solid #334155; background: #1e293b; color: #94a3b8; cursor: pointer;
    border-radius: 999px; padding: 0.18rem 0.6rem; font-size: 0.74rem; white-space: nowrap;
}
.hub-chip.hub-chip-on { background: #16a34a; border-color: #16a34a; color: #fff; font-weight: bold; }
.hub-chip-static { cursor: default; }
.hub-label-input {
    background: #0f172a; border: 1px dashed #334155; border-radius: 8px;
    color: #e2e8f0; padding: 0.25rem 0.5rem; font-size: 0.78rem; width: 150px;
}
.hub-empty { color: #94a3b8; font-size: 0.85rem; padding: 1rem; text-align: center; }
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
        <div class="card">
            <h3>功能開關</h3>
            <div class="feature-grid">
                <div class="feature-item">
                    <div><strong>媒體功能</strong><div class="meta">圖片理解預設開啟；控制圖片理解、圖片與影片生成</div></div>
                    <input class="switch" id="mediaToggle" type="checkbox" onchange="saveFeatures()">
                </div>
                <div class="feature-item">
                    <div><strong>語音功能</strong><div class="meta" id="voiceHint">本地克隆台灣腔尚未就緒</div></div>
                    <input class="switch" id="voiceToggle" type="checkbox" onchange="saveFeatures()">
                </div>
            </div>
        </div>
        <div style="margin-bottom:1rem">
            <button class="btn btn-primary" onclick="openAddModal()">＋ 新增水軍帳號</button>
        </div>
        <div class="card" id="monitorCard" style="margin-bottom:1rem">
            <div class="row" style="margin-bottom:0.8rem">
                <h3 style="margin:0">📊 群組監控（即時收集）</h3>
                <div>
                    <select id="monitorGroup" class="monitor-select" onchange="loadMonitor()"></select>
                    <button class="btn btn-secondary" onclick="showGroups('')">🗂️ 群組總管</button>
                </div>
            </div>
            <div class="stats" id="monitorStats" style="margin-bottom:0.8rem"></div>
            <div class="feed" id="monitorFeed"><div class="meta">尚無資料，請先選擇群組</div></div>
        </div>
        <div class="card" style="margin-bottom:1rem">
            <h3>🛡️ 回覆審計（近 24h）</h3>
            <div id="replyAudit"><div class="meta">載入中…</div></div>
        </div>
        <div class="card" style="margin-bottom:1rem">
            <h3>🧪 媒體實測（live test）</h3>
            <div class="row" style="margin-bottom:0.6rem">
                <span class="meta" id="liveTestState">無進行中實測</span>
                <div>
                    <button class="btn btn-primary" id="liveTestStartBtn" onclick="startLiveTest()">啟動實測</button>
                    <button class="btn btn-danger" id="liveTestStopBtn" onclick="stopLiveTest()" style="display:none">停止</button>
                </div>
            </div>
            <div class="meta" id="liveTestDetail"></div>
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
            <p class="meta">登入成功！設定帳號名稱（可留空用預設）。建立後會保持停止，請先設定人設與群組再手動啟動。</p>
            <input type="text" id="accName" placeholder="帳號名稱（例：台北-美玲）">
            <button class="btn btn-primary" onclick="tgAddAccount()">建立帳號（暫不啟動）</button>
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
            <label class="meta">聊天風格
                <select id="pf_chat_style">
                    <option>俏皮少量表情</option>
                    <option>直球務實</option>
                    <option>內斂反問</option>
                    <option>冷淡短句</option>
                    <option>溫柔慢熱</option>
                    <option>生活碎念</option>
                </select>
            </label>
        </div>
        <div style="margin-top:1rem">
            <button class="btn btn-primary" onclick="savePersona()">儲存人設</button>
            <button class="btn btn-secondary" onclick="regenPersona()">重新生成（換一個）</button>
            <button class="btn btn-secondary" onclick="closeModals()">關閉</button>
        </div>
    </div>
</div>

<!-- 群組總管 -->
<div class="modal" id="groupsModal">
    <div class="modal-box" style="max-width:760px">
        <div class="row" style="margin-bottom:0.8rem">
            <h3 style="margin:0">🗂️ 群組總管</h3>
            <div class="meta" id="hubFocusHint"></div>
        </div>
        <div class="hub-toolbar">
            <input type="text" id="hubSearch" placeholder="搜尋群名、備註、群 ID…（打幾個字就篩選）" oninput="renderGroupsHub()">
            <select id="hubSort" onchange="renderGroupsHub()">
                <option value="activity">最近有訊息</option>
                <option value="name">名稱</option>
                <option value="id">群 ID</option>
                <option value="selected">已選帳號數</option>
            </select>
            <label class="hub-filter"><input type="checkbox" id="hubOnlySelected" onchange="renderGroupsHub()">只看已選</label>
            <button class="btn btn-secondary" onclick="loadGroupDirectory({refresh:true})">重新取得群組</button>
        </div>
        <div class="hub-summary" id="hubSummary">載入中…</div>
        <div class="hub-list" id="groupsList"><div class="hub-empty">正在取得群組清單…</div></div>
        <div style="margin-top:1rem" class="row">
            <div class="meta">點名字旁的帳號膠囊＝把那隻水軍加入／移出這個群；「三號全選」＝一次勾好三隻。<br>
                不勾任何群 = 帳號無法啟動；清空既有選擇會停用帳號。</div>
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

<!-- 帳號功能開關 -->
<div class="modal" id="featuresModal">
    <div class="modal-box" style="max-width:420px">
        <h3>功能開關（帳號級別）</h3>
        <p class="meta" style="margin-bottom:0.8rem">此開關只影響這個水軍帳號；與全域環境變數（REPLY_ENABLED / PROACTIVE_ENABLED）取「且」關係——兩者都開才會生效。</p>
        <label class="meta" style="display:block;margin-bottom:0.5rem">
            <input type="checkbox" id="feat_reply">
            <b>回覆功能</b>（看到群組訊息後回覆）
        </label>
        <label class="meta" style="display:block;margin-bottom:0.5rem">
            <input type="checkbox" id="feat_proactive">
            <b>主動發言</b>（自己開話題、接話）
        </label>
        <p class="meta">⚠️ 修改後需重啟該帳號才生效。</p>
        <div style="margin-top:1rem">
            <button class="btn btn-primary" onclick="saveAccountFeatures()">儲存開關</button>
            <button class="btn btn-secondary" onclick="closeModals()">關閉</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let tgAuthId = '';
let currentPersonaId = '';
let currentPrivatesId = '';
let currentFeaturesId = '';
let latestStatusData = null;

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
    latestStatusData = data;
    document.getElementById('stats').innerHTML = `
        <div class="stat-card"><div class="value">${data.total}</div><div class="label">總帳號數</div></div>
        <div class="stat-card"><div class="value">${data.running}</div><div class="label">運行中</div></div>
    `;
    const features = data.features || {};
    const mediaToggle = document.getElementById('mediaToggle');
    const voiceToggle = document.getElementById('voiceToggle');
    mediaToggle.checked = !!features.media_enabled;
    mediaToggle.disabled = false;
    voiceToggle.checked = !!features.voice_enabled;
    voiceToggle.disabled = !features.voice_available;
    document.getElementById('voiceHint').textContent = features.voice_available
        ? '開啟後使用本地克隆台灣腔'
        : '本地克隆台灣腔尚未就緒，已鎖定關閉';
    document.getElementById('accounts').innerHTML = data.accounts.map(acc => {
        const persona = safeParse(acc.persona);
        const city = persona.city || '未設定';
        const st = statusDisplay(acc.state, acc.is_running, acc.setup_complete);
        const stateCls = st.className;
        const stateTxt = st.text;
        return `
        <div class="card">
            <div class="row">
                <div style="display:flex;align-items:center;gap:0.75rem">
                    ${acc.avatar ? `<img src="${acc.avatar}" alt="頭像" style="width:48px;height:48px;border-radius:50%;object-fit:cover;flex-shrink:0">` : '<div style="width:48px;height:48px;border-radius:50%;background:#2a3a52;display:flex;align-items:center;justify-content:center;color:#7f93b0;font-size:1.1rem;flex-shrink:0">${esc(acc.name).charAt(0)}</div>'}
                    <div>
                    <h3>${esc(acc.name)} <span class="status-badge ${stateCls}">${stateTxt}</span></h3>
                    <div class="meta">
                        ${persona.name || ''}・${persona.gender || '?'}生・${persona.age || '?'}歲・${city}（${persona.district || ''}）・${persona.industry || ''}
                        ${acc.tg_username ? `<br>顯示名：${esc(acc.tg_username)}` : ''}
                        <br>
                        <span class="acc-tag ${acc.enabled ? 'acc-tag-on' : 'acc-tag-off'}">${acc.enabled ? '已啟用' : '已停用'}</span>
                        <br>回覆 ${acc.stats.replies_sent}｜主動 ${acc.stats.proactive_sent}｜語音 ${acc.stats.voice_realtime_sent}｜圖片 ${acc.stats.images_understood}/${acc.stats.images_seen}｜錯誤 ${acc.stats.errors}
                        ${acc.detail ? '<br style="color:#f87171">' + esc(acc.detail) : ''}
                        ${!acc.setup_complete ? '<br>請先檢查人設並設定群組範圍，之後才能啟動。' : ''}
                    </div>
                    </div>
                </div>
                <div>
                    <button class="btn ${acc.is_running ? 'btn-danger' : 'btn-primary'}" data-act="${acc.is_running ? 'stop' : 'start'}" data-id="${esc(acc.id)}" data-state="${acc.state || ''}" ${!acc.is_running && !acc.setup_complete ? 'disabled title="請先設定群組範圍"' : ''}>${acc.is_running ? '停止' : '啟動'}</button>
                    <button class="btn btn-secondary" data-act="toggle" data-id="${esc(acc.id)}">${acc.enabled ? '停用' : '啟用'}</button>
                    <button class="btn btn-secondary" data-act="persona" data-id="${esc(acc.id)}">人設</button>
                    <button class="btn btn-secondary" data-act="groups" data-id="${esc(acc.id)}">群組管理${(acc.groups && acc.groups.length) ? '·' + acc.groups.length : ''}</button>
                    <button class="btn btn-secondary" data-act="features" data-id="${esc(acc.id)}">功能</button>
                    <button class="btn btn-secondary" data-act="privates" data-id="${esc(acc.id)}">私訊</button>
                    <button class="btn btn-danger" data-act="delete" data-id="${esc(acc.id)}">刪除</button>
                </div>
            </div>
        </div>`;
    }).join('') || '<div class="card meta">還沒有水軍帳號，先新增一個吧</div>';
    // 回覆審計（近24h，stage × reason）
    const audit = data.reply_audit || {};
    const stageLabel = {
        'claimed': '已聲明',
        'sent': '已送出',
        'policy': '策略攔截',
        'vision': '視覺理解',
        'media': '媒體額度',
        'generation': '生成',
    };
    const reasonLabel = {
        'group_meta': '群組 meta',
        'blocked_video': '影片阻擋',
        'too_long': '過長',
        'near_duplicate': '近似重複',
        'refusal': '拒絕',
        'image_unavailable': '圖片不可用',
        'image_understanding_empty': '圖片理解為空',
        'image_understanding_error': '圖片理解錯誤',
        'media_disabled': '媒體已關閉',
        'ok': '成功',
        'rate_limited': '限流',
        'error': '錯誤',
    };
    const auditEl = document.getElementById('replyAudit');
    const stages = Object.keys(audit);
    if (!stages.length) {
        auditEl.innerHTML = '<div class="meta">近 24h 無回覆審計紀錄</div>';
    } else {
        auditEl.innerHTML = stages.map(stage => {
            const reasons = audit[stage] || {};
            const rows = Object.keys(reasons).map(r =>
                `<div class="row" style="padding:0.35rem 0;border-bottom:1px solid #2a3a52">
                    <span class="meta">${reasonLabel[r] || r} <span class="meta">（${r}）</span></span>
                    <b style="color:#38bdf8">${reasons[r]}</b>
                </div>`
            ).join('');
            return `<div style="margin-bottom:0.8rem">
                <div style="font-size:0.85rem;font-weight:bold;color:#e2e8f0;margin-bottom:0.3rem">${stageLabel[stage] || stage}（${stage}）</div>
                ${rows}
            </div>`;
        }).join('');
    }
}

async function saveFeatures() {
    const mediaToggle = document.getElementById('mediaToggle');
    const voiceToggle = document.getElementById('voiceToggle');
    mediaToggle.disabled = true;
    voiceToggle.disabled = true;
    const r = await api('/api/features', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            media_enabled: mediaToggle.checked,
            voice_enabled: voiceToggle.checked,
        }),
    });
    if (!r.ok) toast(r.data.error || '功能設定失敗');
    else toast('功能設定已立即生效');
    await loadStatus();
}

function safeParse(s) { try { return JSON.parse(s) || {}; } catch (e) { return {}; } }
function statusDisplay(state, isRunning, setupComplete) {
    if (!setupComplete && !isRunning) {
        return { text: '待設定', className: 'connecting' };
    }
    if (isRunning) {
        if (state === 'connecting' || state === 'stopping') {
            return { text: state === 'connecting' ? '連線中' : '關閉中', className: 'connecting' };
        }
        return { text: '運行中', className: 'running' };
    }
    if (state === 'disconnected') {
        return { text: '連線失敗', className: 'error' };
    }
    if (state === 'connecting') {
        return { text: '連線中', className: 'connecting' };
    }
    if (state === 'stopping') {
        return { text: '關閉中', className: 'connecting' };
    }
    return { text: '已停止', className: 'stopped' };
}
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function startAccount(button) {
    if (!button) return;
    const id = button.dataset.id;
    const old = button.textContent;
    button.disabled = true;
    button.textContent = '啟動中';
    const r = await api('/api/accounts/' + id + '/start', { method: 'POST' });
    if (!r.ok) {
        toast(r.data.error || '啟動失敗');
    }
    setTimeout(() => {
        loadStatus();
        button.disabled = false;
        button.textContent = old;
    }, 500);
}
async function stopAccount(button) {
    if (!button) return;
    const id = button.dataset.id;
    const old = button.textContent;
    button.disabled = true;
    button.textContent = '停止中';
    const r = await api('/api/accounts/' + id + '/stop', { method: 'POST' });
    if (!r.ok) {
        toast(r.data.error || '停止失敗');
    }
    setTimeout(() => {
        loadStatus();
        button.disabled = false;
        button.textContent = old;
    }, 500);
}
async function toggleAccount(button) {
    if (!button) return;
    const id = button.dataset.id;
    const old = button.textContent;
    button.disabled = true;
    button.textContent = '切換中';
    const r = await api('/api/accounts/' + id + '/toggle', { method: 'POST' });
    if (r.ok) toast(r.data.enabled ? '已啟用' : '已停用');
    else toast(r.data.error || '切換失敗');
    setTimeout(() => {
        loadStatus();
        button.disabled = false;
        button.textContent = old;
    }, 500);
}
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
    toast('帳號已建立，請先設定人設與群組再啟動');
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
    const cs = document.getElementById('pf_chat_style');
    if (cs && p.chat_style) cs.value = p.chat_style;
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
        chat_style: g('pf_chat_style'),
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

// ---------- 群組總管 ----------
let hubData = null;
let hubFocusId = '';

async function showGroups(accId) {
    hubFocusId = accId || '';
    document.getElementById('groupsModal').classList.add('active');
    await loadGroupDirectory();
    document.getElementById('hubFocusHint').innerHTML = hubFocusId
        ? `聚焦帳號：<b>${esc(hubAccountLabel(hubFocusId))}</b>`
        : '';
    const search = document.getElementById('hubSearch');
    if (search) search.focus();
}

function hubAccountLabel(accId) {
    const list = (hubData && hubData.accounts) || [];
    const acc = list.find(a => a.id === accId);
    if (!acc) return accId;
    return acc.persona_name || acc.name || accId;
}

function hubSearchText() {
    const el = document.getElementById('hubSearch');
    return (el && el.value ? el.value : '').trim().toLowerCase();
}

async function loadGroupDirectory(opts = {}) {
    document.getElementById('groupsList').innerHTML = '<div class="hub-empty">正在取得群組清單…</div>';
    const r = await api('/api/groups/directory' + (opts.refresh ? '?refresh=1' : '')).catch(() => null);
    if (!r || !r.ok) {
        document.getElementById('groupsList').innerHTML = '<div class="hub-empty">取得群組失敗，稍後再試</div>';
        document.getElementById('hubSummary').textContent = '';
        return;
    }
    hubData = r.data;
    // 清單空的（例如帳號剛登入、還沒連線過）就自動做一次唯讀探索，免得要自己按重新取得
    if (!opts.refresh && !(hubData.groups || []).length) {
        return loadGroupDirectory({refresh: true});
    }
    renderGroupsHub();
    if (opts.refresh) toast('已重新取得群組清單');
}

function hubFmtTime(ts) {
    if (!ts) return '—';
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return '剛剛';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分鐘前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小時前';
    return new Date(ts * 1000).toLocaleString('zh-TW', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
}

function renderGroupsHub() {
    if (!hubData) return;
    const accs = hubData.accounts || [];
    const kw = hubSearchText();
    const onlySelected = document.getElementById('hubOnlySelected').checked;
    const sort = document.getElementById('hubSort').value;
    let rows = (hubData.groups || []).filter(g => {
        if (onlySelected && !g.selected_count) return false;
        if (!kw) return true;
        return [g.display, g.title, g.label, String(g.id)]
            .some(v => String(v || '').toLowerCase().includes(kw));
    });
    rows = rows.slice().sort((a, b) => {
        if (sort === 'name') return String(a.display).localeCompare(String(b.display), 'zh-Hant');
        if (sort === 'id') return a.id - b.id;
        if (sort === 'selected') return (b.selected_count - a.selected_count) || (b.last_ts - a.last_ts);
        return (b.last_ts - a.last_ts) || (a.id - b.id);
    });

    const total = (hubData.groups || []).length;
    const selectedGroups = (hubData.groups || []).filter(g => g.selected_count).length;
    document.getElementById('hubSummary').innerHTML =
        `共 <b>${total}</b> 個群｜水軍已指定 <b>${selectedGroups}</b> 個｜篩選後 <b>${rows.length}</b> 個`
        + `｜帳號：${accs.map(a => esc(a.persona_name || a.name) + (a.is_running ? '' : '（停）')).join('、')}`;

    document.getElementById('groupsList').innerHTML = rows.length ? rows.map(g => {
        const chips = accs.map(a => {
            const on = (g.accounts || []).includes(a.id);
            const focus = hubFocusId === a.id;
            return `<button class="hub-chip ${on ? 'hub-chip-on' : ''}"
                title="${esc(a.persona_name || a.name)}：${on ? '已加入，點一下移出' : '未加入，點一下加入'}"
                onclick="toggleGroupAccount(${g.id}, '${esc(a.id)}', ${on ? 'false' : 'true'})"
                ${focus ? 'style="outline:2px solid #38bdf8"' : ''}>${esc(a.persona_name || a.name)}${on ? ' ✓' : ''}</button>`;
        }).join('');
        const human = g.last_human_ts ? hubFmtTime(g.last_human_ts) : '無紀錄';
        return `
        <div class="hub-row ${g.selected_count ? 'hub-row-on' : ''}">
            <div class="hub-row-top">
                <div style="min-width:0;flex:1">
                    <span class="hub-name">${esc(g.display)}</span>
                    ${g.label ? '' : `<span class="meta">（Telegram 名稱：${esc(g.title)}）</span>`}
                    <div class="hub-info">
                        <span class="hub-id" onclick="hubCopyId(${g.id})" title="點一下複製群 ID">${g.id}</span>
                        ${g.members ? `・成員 ${g.members}` : ''}
                        ｜訊息 ${g.msg_count} 則｜真人 ${g.human_senders} 人
                        ｜最後訊息 ${hubFmtTime(g.last_ts)}（真人 ${human}）
                    </div>
                </div>
                <div class="hub-actions">
                    ${chips}
                    <button class="hub-chip hub-chip-static" style="border-style:dashed" onclick="allAccountsGroup(${g.id}, true)">三號全選</button>
                    <button class="hub-chip hub-chip-static" style="border-style:dashed" onclick="allAccountsGroup(${g.id}, false)">全部移出</button>
                    <button class="hub-chip hub-chip-static" onclick="jumpToMonitor(${g.id})">看訊息</button>
                    <input class="hub-label-input" placeholder="＋備註名" value="${esc(g.label)}"
                        onkeydown="if(event.key==='Enter'){this.blur();}"
                        onblur="saveGroupLabel(${g.id}, this.value)">
                </div>
            </div>
        </div>`;
    }).join('') : '<div class="hub-empty">沒有符合條件的群組。若是剛被拉進去的群，按「重新取得群組」。</div>';
}

function hubCopyId(gid) {
    const text = String(gid);
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => toast('已複製群 ID：' + text)).catch(() => toast(text));
    } else {
        toast(text);
    }
}

async function toggleGroupAccount(groupId, accId, selected) {
    const r = await api('/api/groups/membership', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({group_id: groupId, account_ids: [accId], selected}),
    }).catch(() => null);
    if (!r) { toast('設定失敗'); return; }
    const msg = r.data && r.data.results ? r.data.results[accId] : '';
    if (!r.ok && !msg) { toast(r.data.error || '設定失敗'); return; }
    toast(msg && msg !== 'ok' ? msg : (selected ? '已加入' : '已移出'));
    await loadGroupDirectory();
    loadStatus();
    loadMonitorGroups();
}

async function allAccountsGroup(groupId, selected) {
    const ids = ((hubData && hubData.accounts) || []).map(a => a.id);
    if (!ids.length) return;
    const r = await api('/api/groups/membership', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({group_id: groupId, account_ids: ids, selected}),
    }).catch(() => null);
    if (!r) { toast('設定失敗'); return; }
    const results = (r.data && r.data.results) || {};
    const bad = Object.values(results).filter(v => v !== 'ok');
    toast(bad.length ? bad.join('；') : (selected ? '三隻水軍都加入這個群了' : '三隻水軍都移出這個群了'));
    await loadGroupDirectory();
    loadStatus();
    loadMonitorGroups();
}

async function saveGroupLabel(groupId, value) {
    const label = (value || '').trim();
    const current = ((hubData && hubData.groups) || []).find(g => g.id === groupId);
    if (current && (current.label || '') === label) return;
    const r = await api('/api/groups/label', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({group_id: groupId, label}),
    }).catch(() => null);
    if (!r || !r.ok) { toast((r && r.data.error) || '備註儲存失敗'); return; }
    toast(label ? '備註名已儲存：' + label : '已清除備註名');
    await loadGroupDirectory();
}

function jumpToMonitor(gid) {
    closeModals();
    const sel = document.getElementById('monitorGroup');
    if (![...sel.options].some(o => Number(o.value) === Number(gid))) {
        sel.insertAdjacentHTML('afterbegin', `<option value="${gid}">群組 ${gid}</option>`);
    }
    sel.value = String(gid);
    monitorGroupId = sel.value;
    try { localStorage.setItem('sdf_monitor_group', sel.value); } catch (e) {}
    loadMonitor();
    const card = document.getElementById('monitorCard');
    if (card && card.scrollIntoView) card.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function showPrivates(id) {
    const r = await api('/api/accounts/' + id + '/privates');
    if (!r.ok) return;
    currentPrivatesId = id;
    document.getElementById('privList').innerHTML = (r.data.messages || []).map(m =>
        `<div class="card" style="margin-bottom:0.5rem;padding:0.8rem">
            <div class="meta"><b>${esc(m.sender_name)}</b>（${new Date(m.timestamp * 1000).toLocaleString('zh-TW')}）${m.read ? '' : ' 🔴未讀'}</div>
            <div style="font-size:0.9rem">${esc(m.preview)}</div>
            ${!m.read ? `<button class="btn btn-secondary" style="margin-top:0.5rem" onclick="markPrivateRead(${m.id})">標記已讀</button>` : ''}
        </div>`
    ).join('') || '<div class="meta">沒有私訊紀錄</div>';
    document.getElementById('privModal').classList.add('active');
}

async function markPrivateRead(msgId) {
    if (!currentPrivatesId) return;
    const r = await api(`/api/accounts/${currentPrivatesId}/privates/${msgId}/read`, { method: 'POST' });
    if (r.ok) { toast('已標記已讀'); showPrivates(currentPrivatesId); }
    else toast(r.data.error || '標記失敗');
}

function showFeatures(id, replyOn, proactiveOn) {
    currentFeaturesId = id;
    document.getElementById('feat_reply').checked = !!replyOn;
    document.getElementById('feat_proactive').checked = !!proactiveOn;
    document.getElementById('featuresModal').classList.add('active');
}

async function saveAccountFeatures() {
    if (!currentFeaturesId) return;
    const r = await api(`/api/accounts/${currentFeaturesId}/features`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            reply_enabled: document.getElementById('feat_reply').checked,
            proactive_enabled: document.getElementById('feat_proactive').checked,
        }),
    });
    if (r.ok) {
        toast('開關已儲存，重啟帳號後生效');
        closeModals();
        loadStatus();
    } else toast(r.data.error || '儲存失敗');
}

// ---------- 群組監控 ----------
let monitorGroupId = null;

function isBotRole(role) {
    return String(role || '').toLowerCase() === 'assistant';
}

async function loadMonitorGroups() {
    const r = await api('/api/groups/directory').catch(() => null);
    const sel = document.getElementById('monitorGroup');
    if (!sel) return;
    const groups = (r && r.ok && r.data.groups) || [];
    if (groups.length) {
        sel.innerHTML = groups.map(g => {
            const flags = [];
            if (g.selected_count) flags.push(`水軍 ${g.selected_count}`);
            if (g.human_senders) flags.push(`真人 ${g.human_senders}`);
            const suffix = flags.length ? '｜' + flags.join('・') : '';
            return `<option value="${g.id}">${esc(g.display)}${suffix}</option>`;
        }).join('');
    } else {
        // 後端目錄拿不到時，退回 /api/status 的舊資料，避免整條監控掛掉
        const s = await api('/api/status').catch(() => null);
        const opts = new Set();
        const accounts = (s && s.ok ? s.data.accounts : []) || [];
        accounts.forEach(a => (a.groups_available || []).forEach(g => { if (g && g.id) opts.add(g.id); }));
        accounts.forEach(a => (a.groups || []).forEach(gid => { if (gid) opts.add(gid); }));
        sel.innerHTML = Array.from(opts).map(gid =>
            `<option value="${gid}">群組 ${gid}</option>`
        ).join('');
    }
    let saved = null;
    try { saved = localStorage.getItem('sdf_monitor_group'); } catch (e) {}
    const values = [...sel.options].map(o => o.value);
    if (saved && values.includes(String(saved))) {
        sel.value = String(saved);
    } else if (values.length && groups.length) {
        // 預設看「最近有訊息且水軍有在裡面」的群，找不到就退回第一個
        const active = groups.find(g => g.selected_count && g.last_ts) || groups.find(g => g.last_ts) || groups[0];
        sel.value = String(active.id);
    }
    if (values.length) { monitorGroupId = sel.value; loadMonitor(); }
}

async function loadMonitor() {
    const sel = document.getElementById('monitorGroup');
    monitorGroupId = sel && sel.value;
    if (!monitorGroupId) return;
    try { localStorage.setItem('sdf_monitor_group', monitorGroupId); } catch (e) {}
    const feed = document.getElementById('monitorFeed');
    const stats = document.getElementById('monitorStats');
    feed.innerHTML = '<div class="meta">載入中…</div>';
    const r = await api('/api/groups/' + monitorGroupId + '/messages?limit=5000').catch(() => null);
    if (!r || !r.ok) {
        feed.innerHTML = '<div class="meta">載入失敗</div>';
        stats.innerHTML = '';
        return;
    }
    const msgs = (r.data.messages || []).slice().reverse(); // 舊 → 新
    const bots = msgs.filter(m => isBotRole(m.role));
    const humans = msgs.filter(m => !isBotRole(m.role));
    // 有來有回率：水軍發言後 10 分鐘內有無人（非水軍）回話
    let replied = 0;
    bots.forEach(b => {
        const t = b.timestamp;
        const hasReply = humans.some(h => (h.timestamp - t) > 0 && (h.timestamp - t) <= 600);
        if (hasReply) replied++;
    });
    const rate = bots.length ? Math.round(replied / bots.length * 100) : 0;
    const tmin = msgs.length ? new Date(Math.min(...msgs.map(m => m.timestamp * 1000))) : null;
    const tmax = msgs.length ? new Date(Math.max(...msgs.map(m => m.timestamp * 1000))) : null;
    const winLabel = (tmin && tmax)
        ? (tmin.toLocaleString('zh-TW') + ' → ' + tmax.toLocaleString('zh-TW'))
        : '無訊息';
    stats.innerHTML = `
        <div class="stat-card"><div class="value">${msgs.length}</div><div class="label">訊息總數</div></div>
        <div class="stat-card"><div class="value">${humans.length}</div><div class="label">人類訊息</div></div>
        <div class="stat-card"><div class="value">${bots.length}</div><div class="label">水軍訊息</div></div>
        <div class="stat-card"><div class="value">${rate}%</div><div class="label">有來有回率</div></div>
        <div class="stat-card" style="grid-column:1 / -1"><div class="label" style="margin-top:0">收集區間</div><div class="value" style="font-size:0.9rem;color:#94a3b8">${winLabel}</div></div>
    `;
    feed.innerHTML = msgs.slice(-60).map(m => {
        const bot = isBotRole(m.role);
        const time = new Date(m.timestamp * 1000).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' });
        return `<div class="feed-item">
            <div class="who"><span class="badge ${bot ? 'badge-bot' : 'badge-human'}">${bot ? '水軍' : '人類'}</span><br>${esc(m.sender_name || '匿名')}<br><span class="ts">${time}</span></div>
            <div class="body"><div class="bubble ${bot ? 'bubble-bot' : 'bubble-human'}">${esc(m.content || '')}</div></div>
        </div>`;
    }).join('') || '<div class="meta">群組目前沒有訊息</div>';
}

// ---------- 媒體實測 ----------
const LT_ACCOUNT_IDS = ['2ce525dfb0d4', 'faa9a202f96e', '038632e4395b', 'e63e27a4340d'];
const LT_GROUP_ID = -5428680940;

function buildLiveTestSchedule() {
    const events = [];
    for (let i = 0; i < 18; i++) {
        events.push({ event_id: 'text-' + i, offset_seconds: 0, account_id: LT_ACCOUNT_IDS[i % 4], kind: 'text', text: '測試文字 ' + i });
    }
    LT_ACCOUNT_IDS.forEach((id, i) => events.push({ event_id: 'voice-' + i, offset_seconds: 0, account_id: id, kind: 'voice' }));
    LT_ACCOUNT_IDS.forEach((id, i) => events.push({ event_id: 'image-' + i, offset_seconds: 0, account_id: id, kind: 'image', path: 'adult-' + i + '.jpg' }));
    [0, 1].forEach(i => events.push({ event_id: 'video-' + i, offset_seconds: 0, account_id: LT_ACCOUNT_IDS[i], kind: 'video' }));
    [2, 3].forEach(i => events.push({ event_id: 'vision-' + (i - 2), offset_seconds: 0, account_id: LT_ACCOUNT_IDS[i], kind: 'vision_reply', path: 'adult-' + (i - 2) + '.jpg' }));
    return events;
}

async function startLiveTest() {
    const btn = document.getElementById('liveTestStartBtn');
    btn.disabled = true; const old = btn.textContent; btn.textContent = '啟動中';
    const r = await api('/api/live-test/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            account_ids: LT_ACCOUNT_IDS,
            group_id: LT_GROUP_ID,
            duration_seconds: 3600,
            event_cap: 40,
            video_enabled: true,
            schedule: buildLiveTestSchedule(),
        }),
    }).catch(() => null);
    btn.disabled = false; btn.textContent = old;
    if (!r) { toast('啟動失敗（網路錯誤）'); return; }
    if (!r.ok) { toast(r.data.error || '啟動失敗'); return; }
    toast('媒體實測已啟動');
    loadLiveTestStatus();
}

async function stopLiveTest() {
    const r = await api('/api/live-test/stop', { method: 'POST' });
    if (r.ok) { toast('已請求停止實測'); loadLiveTestStatus(); }
    else toast(r.data.error || '停止失敗');
}

async function loadLiveTestStatus() {
    const r = await api('/api/live-test/status').catch(() => null);
    const state = document.getElementById('liveTestState');
    const detail = document.getElementById('liveTestDetail');
    const startBtn = document.getElementById('liveTestStartBtn');
    const stopBtn = document.getElementById('liveTestStopBtn');
    if (!r || !r.ok || !state) return;
    const lt = r.data.live_test || {};
    if (lt.status === 'idle' || !lt.run_id) {
        state.textContent = '無進行中實測';
        detail.textContent = '';
        startBtn.style.display = '';
        stopBtn.style.display = 'none';
    } else {
        state.textContent = `實測狀態：${lt.status}（run ${String(lt.run_id).slice(0, 8)}，運行中 ${lt.running || 0}/${(lt.account_ids || []).length} 帳號）`;
        detail.textContent = `排程 ${lt.schedule_count || 0} 事件｜已送出 ${lt.reserved || 0}/${lt.event_cap || 40}｜剩餘 ${lt.remaining || 0}｜影片${lt.video_enabled ? '開啟' : '關閉'}`;
        startBtn.style.display = 'none';
        stopBtn.style.display = '';
    }
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
            if (act === 'start') startAccount(b);
            else if (act === 'stop') stopAccount(b);
            else if (act === 'toggle') toggleAccount(b);
            else if (act === 'persona') showPersona(id);
            else if (act === 'groups') showGroups(id);
            else if (act === 'features') {
                const a = (latestStatusData && latestStatusData.accounts || []).find(x => x.id === id);
                showFeatures(id, a && a.reply_enabled, a && a.proactive_enabled);
            }
            else if (act === 'privates') showPrivates(id);
            else if (act === 'delete') deleteAccount(id);
        });
        loadStatus();
        loadMonitorGroups();
        loadLiveTestStatus();
        setInterval(() => {
            if (document.getElementById('mainBox').style.display !== 'none') {
                loadStatus();
                if (monitorGroupId) loadMonitor();
                loadLiveTestStatus();
            }
        }, 15000);
    }
})();
</script>
</body>
</html>
"""
