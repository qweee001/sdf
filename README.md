# Telegram AI Userbot — 固定角色群聊版

Railway 可部署的 Telegram Userbot。每個 Railway Service 綁定一個 Telegram 帳號及一個固定角色，只在群組內互動，使用 OpenAI-compatible API 生成回覆，並只保留最近 24 小時的群聊記憶。

## 功能

- 僅處理群聊，不回覆私聊。
- 固定四種角色組合：男性老成員、女性老成員、男性觀望成員、女性觀望成員。
- 同一帳號的性別與階段固定，不會每次發言重新隨機。
- 可設定群組白名單、一般回覆機率、被提及／被回覆時必答。
- 可忽略其他自動化帳號的 Telegram ID，避免多帳號互相回覆形成循環。
- 可在群組安靜一段時間後自然開啟非露骨話題。
- SQLite 群聊記憶以 `group_id` 分隔，超過 24 小時自動清除。
- 使用 `TG_SESSION_STRING`，不需要在 Railway 互動輸入 Telegram 驗證碼。
- 被直接問到時會如實說明是自動互動角色，不冒充真人會員。

## 角色設定

每個 Service 固定設定以下兩個值：

| `ACCOUNT_GENDER` | `ACCOUNT_STAGE` | 角色 |
|---|---|---|
| `male` | `old_member` | 男性老成員 |
| `female` | `old_member` | 女性老成員 |
| `male` | `observer` | 男性觀望成員 |
| `female` | `observer` | 女性觀望成員 |

可用 `ACCOUNT_STYLE` 補充該帳號的固定語氣，但不要填寫真實個資。

## 產生 Telegram Session String

先在可信任的本機環境安裝依賴：

```bash
python -m pip install -r requirements.txt
python scripts/generate_session.py
```

完成 Telegram 驗證後，工具會顯示 `TG_SESSION_STRING`。它等同登入憑證，只能保存於 Railway Variables，不得貼到聊天、日誌或 GitHub。

## Railway 部署

1. 在 Railway 建立 Project，選擇 **Deploy from GitHub repo**，連結此倉庫。
2. 建立一個 Service；Railway 會讀取根目錄的 `Dockerfile` 與 `railway.json`。
3. 在 Service 的 **Variables** 新增：

```env
TG_API_ID=
TG_API_HASH=
TG_SESSION_STRING=
AI_API_KEY=
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=
ACCOUNT_GENDER=male
ACCOUNT_STAGE=old_member
ACCOUNT_STYLE=穩重、自然、偶爾幽默
GROUP_CHAT_IDS=-1001234567890
IGNORE_SENDER_IDS=
MEMORY_TTL_HOURS=24
MEMORY_DB_PATH=/data/memory.db
```

4. 為了讓記憶在重新部署後仍保留，在 Service 掛載 Railway Volume，Mount Path 設成 `/data`。
5. 部署後查看 Logs，成功時會看到 `Userbot connected`，但不會顯示 API Key、API Hash 或 Session String。
6. 用其他帳號在允許的測試群發訊息，確認只在群聊回覆、私聊不回覆。

若要部署多個帳號，為每個帳號建立獨立 Railway Service，分別設定自己的 `TG_SESSION_STRING`、角色與 `/data` Volume。不要讓多個 Service 共用同一個 Telegram Session String。建議把其他自動化帳號的 Telegram 數字 ID 填入 `IGNORE_SENDER_IDS`，避免帳號彼此接話形成循環。

## 本機執行

複製範例設定並填入自己的值：

```bash
cp .env.example .env
docker compose up --build
```

`.env`、`*.session`、SQLite 記憶檔都已被 Git 與 Docker 忽略。

## 發布前檢查

```bash
python scripts/audit_release.py .
python -m unittest discover -s tests -v
```

若要檢查壓縮包：

```bash
python scripts/audit_release.py telegram-ai-userbot-fixed-roles.zip
```

## 重要限制

- Telegram Userbot 使用一般使用者帳號自動化，可能受 Telegram 條款與風控影響；請先在小型測試群驗證。
- 角色可以參與成人交友話題，但程式提示要求所有人已成年、明確自願、尊重隱私與安全，且不捏造真人見證。
- SQLite 僅適合單一 Service／單一副本。不要把同一資料庫 Volume 同時掛給多個副本。
