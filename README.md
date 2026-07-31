# Telegram AI Userbot — 多帳號控制台版

一個 Railway Service 可同時管理多個 Telegram Userbot 帳號。每個帳號都有獨立的
Telegram 連線、固定角色、群組範圍、任務說明、AI 模型與 24 小時記憶。

## 主要功能

- 同一個 Railway Service 同時啟動多個 Telegram 帳號。
- 控制台直接輸入手機號碼、Telegram 驗證碼及必要的兩步驗證密碼，
  自動建立並加密保存 Session。
- 控制台新增帳號、啟用、停用、重連及查看連線錯誤。
- 四種固定角色：男性／女性老成員、男性／女性觀望成員。
- 每個帳號可編輯語氣、任務名稱、任務說明及回覆行為。
- 每個帳號可選擇所有群組或指定群組。
- 每個帳號可設定 OpenAI-compatible Base URL、模型與專用 API Key。
- 模型可從控制台測試及切換，儲存後只重啟該帳號。
- 每個帳號的群聊記憶與統計完全隔離，超過 24 小時自動清除。
- 所有受管帳號自動互相忽略，避免機器帳號互相回覆形成循環。
- 一個帳號連線或模型失敗，不會使其他帳號與控制台停止。
- Telegram Session 與帳號專用 API Key 使用 Fernet 加密後保存在 Railway Volume。
- 控制台與 API 使用登入速率限制、伺服器端 Session、CSRF 驗證及安全標頭。

## 首次升級

如果 Railway 已有舊版單帳號設定，第一次啟動新版時會自動：

1. 驗證現有 `TG_SESSION_STRING`。
2. 加密 Session 並建立 `primary` 帳號。
3. 保留原本角色、群組範圍、模型及啟停狀態。
4. 將舊訊息補上 `primary` 帳號 ID，不會清空現有 24 小時記憶。

之後新增的帳號直接在控制台輸入新的 `TG_SESSION_STRING`，不需要再建立 Railway
Service。

## 必要 Railway Variables

```env
TG_API_ID=
TG_API_HASH=
TG_SESSION_STRING=

AI_API_KEY=
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-5-mini

ACCOUNT_ENCRYPTION_KEY=
MAX_ACCOUNTS=10

MEMORY_TTL_HOURS=24
MEMORY_HISTORY_LIMIT=30
MEMORY_DB_PATH=/data/memory.db

DASHBOARD_ENABLED=true
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=<至少12字元的強密碼>
```

產生 `ACCOUNT_ENCRYPTION_KEY`：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

這個 Key 只能放在 Railway Variables。建立帳號後不可任意更換，否則已保存的
Telegram Session 與 API Key 將無法解密。

## 新增多個帳號

1. 登入網頁控制台，點選「新增帳號」。
2. 輸入包含國碼的 Telegram 手機號碼，例如 `+886912345678`。
3. 輸入 Telegram 官方帳號或 App 傳送的驗證碼。
4. 如果帳號已啟用兩步驗證，再輸入 Telegram 兩步驗證密碼。
5. 設定名稱、角色、任務與模型；控制台會自動產生 Session、加密保存並啟動帳號。

電話、驗證碼、兩步驗證密碼與 `phone_code_hash` 只會在伺服器記憶體短暫存在，
不會寫入 SQLite、日誌或瀏覽器儲存空間。未完成流程 10 分鐘後自動失效。

如果電話驗證暫時不可用，仍可在「登入方式」選擇 `TG_SESSION_STRING（進階）`，
使用 `scripts/generate_session.py` 產生後貼入。

不得把同一個 Telegram 帳號重複加入；系統會檢查 Session 與 Telegram ID。

Session String 與 API Key 都不會由任何 GET API、HTML、狀態或日誌回傳。

## 模型設定

每個帳號可使用不同的：

- AI Base URL（必須是公開 HTTPS 位址）
- Model ID
- 帳號專用 API Key

專用 API Key 留空時使用 Railway 的全域 `AI_API_KEY`。更新欄位留空代表保留原
Key；勾選「清除自訂 Key」後才會改回全域 Key。控制台提供小型「測試模型」請求，
測試失敗不會自動改寫其他帳號。

## 任務資訊

`任務名稱` 與 `任務說明` 會加入該帳號的 system prompt，例如指定聊天重點、
歡迎新成員或回答某類話題。任務不能覆蓋成年、自願、尊重、隱私、不冒充真人及
不捏造經歷等共同規則。

## 部署

- Railway 必須保留 `/data` Volume，資料庫預設在 `/data/memory.db`。
- 服務保持 **1 Replica**。SQLite Volume 與 Telegram Session 不支援多副本同時登入。
- Railway 會讀取根目錄的 `Dockerfile` 與 `railway.json`。
- 公開網域只提供有密碼保護的控制台；敏感憑證仍由 Railway Variables 管理。

## 本機執行與檢查

```bash
cp .env.example .env
docker compose up --build
python scripts/audit_release.py .
python -m unittest discover -s tests -v
```

`.env`、`*.session`、SQLite 資料庫與所有真實密鑰都已被 Git 與 Docker 忽略。

## 使用限制

- Userbot 使用一般 Telegram 帳號自動化，可能受 Telegram 條款與風控影響。
- 新帳號預設仍有回覆機率與主動發言上限；請先在測試群驗證。
- 所有帳號被詢問時必須如實說明是自動互動角色，不是真人會員。
- 角色可參與成人交友話題，但必須以成年、自願、尊重、隱私與安全為前提。
