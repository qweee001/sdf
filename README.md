# Telegram AI Userbot — 多帳號控制台版

一個 Railway Service 可同時管理多個 Telegram Userbot 帳號。每個帳號都有獨立的
Telegram 連線、固定角色、群組範圍、任務說明、AI 模型與 24 小時記憶。

## 主要功能

- 同一個 Railway Service 同時啟動多個 Telegram 帳號。
- 控制台直接輸入手機號碼、Telegram 驗證碼及必要的兩步驗證密碼，
  自動建立並加密保存 Session。
- 控制台新增帳號、啟用、停用、重連及查看連線錯誤。
- 四種固定角色：男性／女性老成員、男性／女性觀望成員。
- 所有帳號與隨機性格都固定使用台灣繁體中文、台灣常用詞及自然群聊語序；
  角色採台灣在地生活語境，但不捏造真實國籍、住址或真人經歷。
- 每個帳號可編輯語氣、任務名稱、任務說明及回覆行為。
- 回覆節奏會自動分析同一群組最近 5 分鐘與 20 分鐘的人類訊息數、參與人數：
  群聊繁忙時降低隨機回覆並延長主動發言間隔，群聊安靜時提高回覆機率並提早開啟話題；
  被提及或直接回覆帳號時仍優先回應，控制台設定的回覆機率則作為基準值。多帳號會
  自動分攤同一個群組級回覆機率，不因連線帳號增加而成倍搶答。
- 新帳號若未手填角色風格，登入完成後會自動產生與其他帳號不同的隨機性格。
- 「角色、任務與模型」可一鍵預覽完整隨機設定，包含名稱、角色、性格、任務、
  Grok 文字模型及回覆節奏；確認後仍須手動儲存才會生效。
- 每個帳號可選擇所有群組或指定群組。
- 文字模型預設透過 OpenRouter 使用 Grok；每個帳號仍可設定 OpenAI-compatible Base URL 與模型，API Key 只從 Railway Variables 讀取。
- 每個帳號可設定輸出屏蔽詞與屏蔽主題；草稿會經字面、變形、近似詞與語意審核，未通過時不會發送。
- 每個帳號可獨立開啟 OpenRouter 圖片、台灣口語語音與影片，並設定模型／聲線、每日上限、冷卻時間及允許群組。
- 媒體請求由聊天模型輸出結構化意圖，通過屏蔽及安全審核後才排入背景任務；影片不會阻塞文字群聊。
- 圖片與完成的影片會再次執行視覺安全審核；所有媒體都由目前登入的 Telegram 帳號透過 `send_file` 發送。
- 模型可從控制台測試及切換，儲存後只重啟該帳號。
- 每個帳號的群聊記憶與統計完全隔離，超過 24 小時自動清除；生成回覆時只分析
  同一帳號、同一群組最近最多 20 條訊息。
- AI 自動回覆在發送前會合併空白行、不必要換行與連續空格，避免長內容出現
  制式分段；控制台手動發送仍完整保留管理員輸入的原始排版。
- 控制台集中顯示各帳號最近 24 小時的私聊提醒與未讀數；私聊不會觸發 AI 回覆，
  也不會進入群聊記憶或模型上下文。
- 所有受管帳號自動互相忽略，避免機器帳號互相回覆形成循環。
- 一個帳號連線或模型失敗，不會使其他帳號與控制台停止。
- Telegram Session 使用 Fernet 加密後保存在 Railway Volume；API Key 不寫入資料庫。
- 控制台與 API 使用登入速率限制、伺服器端 Session、CSRF 驗證及安全標頭。

## 首次升級

如果 Railway 已有舊版單帳號設定，第一次啟動新版時會自動：

1. 驗證現有 `TG_SESSION_STRING`。
2. 加密 Session 並建立 `primary` 帳號。
3. 保留原本角色、群組範圍、模型及啟停狀態。
4. 將舊訊息補上 `primary` 帳號 ID，不會清空現有 24 小時記憶。
5. 將舊版預設的 `gpt-5-mini`、`gpt-image-1.5`、`azure-speech` 與
   `sora-2` 設定一次性遷移到本版 OpenRouter／Grok 預設；自行指定的其他模型
   不會被改寫。

之後新增的帳號直接在控制台輸入新的 `TG_SESSION_STRING`，不需要再建立 Railway
Service。

## 必要 Railway Variables

```env
TG_API_ID=
TG_API_HASH=
TG_SESSION_STRING=

OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=x-ai/grok-4.20

# 一次性既有帳號遷移；平常保持 false
MIGRATE_EXISTING_ACCOUNTS_TO_GROK_ADULT=false

# 可選：媒體使用另一把 OpenRouter Key；留白時沿用 OPENROUTER_API_KEY
OPENROUTER_MEDIA_API_KEY=
OPENROUTER_MEDIA_BASE_URL=https://openrouter.ai/api/v1
MEDIA_IMAGE_MODEL=x-ai/grok-imagine-image-quality
MEDIA_TTS_MODEL=x-ai/grok-voice-tts-1.0
MEDIA_VIDEO_MODEL=x-ai/grok-imagine-video-1.5
MEDIA_MODERATION_MODEL=x-ai/grok-4.20

# 只有改用非 OpenRouter 語音供應商時才需要
AZURE_SPEECH_KEY=
AZURE_SPEECH_REGION=

ACCOUNT_ENCRYPTION_KEY=
# 可由 Railway Variables 依環境覆寫；預設可管理 20 個帳號
MAX_ACCOUNTS=20

MEMORY_TTL_HOURS=24
MEMORY_HISTORY_LIMIT=20
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
Telegram Session 將無法解密。

OpenRouter 金鑰只能放在 Railway Variables。控制台只顯示供應商是否就緒，不提供
金鑰輸入欄，也不會把金鑰保存到 SQLite、API 回應或日誌。沒有媒體金鑰時，原有
文字群聊仍可正常運作；各帳號的圖片、語音及影片預設皆為關閉。

若要把資料庫內所有既有帳號一次改成 OpenRouter `x-ai/grok-4.20` 並開啟受限的
「成人純文字模式」，可在 Railway Variables 將
`MIGRATE_EXISTING_ACCOUNTS_TO_GROK_ADULT=true`，只部署一次；確認成功後立即刪除該
Variable 或改回 `false`，避免日後新增或手動調整的帳號仍被此旗標覆寫。重複執行
不會再次修改已符合目標設定的帳號、增加版本或新增稽核紀錄。這項遷移不會修改媒體
設定、固定媒體安全閘門或影像／影片前後審核，也不會放寬未成年、非自願、剝削、
騷擾或其他固定攔截規則。媒體語意預審會使用各帳號的文字模型，因此遷移後該層會
隨帳號改用 Grok 4.20；審核失敗或無法確定時仍會封鎖。

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
升級時若資料庫仍含舊版帳號專用 API Key，啟動程序會直接清除該欄位。

## 控制台操作

控制台會以帳號卡片顯示設定。可收合不常調整的區塊，集中查看帳號狀態、角色、
群組範圍、任務與媒體設定；展開後再編輯並儲存。手動發送功能會由你選定帳號與
允許群組後原文送出內容；它不經 AI 改寫、角色審核或屏蔽詞檢查，但仍要求控制台
登入、有效 CSRF、已連線帳號，以及該帳號已加入且允許的群組。自動 AI 回覆仍會
套用既有內容政策。超過 Telegram 單則訊息容量的手動文字會自動分段發送。
若多段發送途中斷線，控制台會顯示已送出的則數，並只保留尚未送出的文字，
避免直接重試時重複前段內容。
收到私聊時，控制台的總覽、帳號卡片與帳號概覽會顯示未讀提醒。提醒只保存
發送者顯示名稱、安全截斷的文字或媒體類型預覽及收到時間，不下載私聊附件，
也不公開對方 Telegram ID、電話或其他敏感欄位。可逐筆或全部標記為已處理；
提醒與群聊記憶分開保存，並在 24 小時後自動刪除。
隨機設定不會修改群組權限、屏蔽詞、API Base URL、密鑰或媒體開關／配額；成人
純文字模式抽中的成人性格仍固定要求成年、自願、尊重隱私及拒絕即停止。清除某
帳號的聊天記憶時，系統會先排空正在處理的文字回覆，再原子清除記憶並為該帳號換一個不同
性格，不會刪除 Telegram Session 或其他設定。
帳號數量上限固定為 20；Railway 的 `MAX_ACCOUNTS` Variable 可設為 1–20 以降低環境
上限，但不能超過 20。不要將任何 API Key 或 Telegram Session 寫入程式碼、README
或 Git。

## 模型設定

每個帳號可使用不同的：

- AI Base URL（必須是公開 HTTPS 位址）
- Model ID

所有帳號共用 Railway Variables 內的 `OPENROUTER_API_KEY`；舊環境的
`AI_API_KEY` 仍可作為相容備援。控制台不接受或保存 Key。
控制台提供小型「測試模型」請求，測試失敗不會自動改寫其他帳號。
當 `OPENROUTER_API_KEY` 已設定時，系統只允許把該 Key 傳往
`https://openrouter.ai/api/v1`，避免誤把 OpenRouter 憑證送到其他供應商。

預設文字模型是 `x-ai/grok-4.20`，適合嚴格遵循角色與輸出規則的長對話；若偏好
更高品質可在帳號設定改成 `x-ai/grok-4.5`，也可使用
`x-ai/grok-4.3`。這些是本專案依模型能力做的配置建議，並非供應商官方宣稱
的「成人聊天專用模型」。

每個帳號另有「成人純文字模式」開關，預設關閉。開啟即代表管理員確認該帳號的
允許群組為 18+；群內普通成人聊天、玩笑與虛構情境預設為成年、自願，不必在每句
文字重複確認。這個預設不等於同意現實接觸、指定行為或分享私密素材，也不會放寬
圖片、語音或影片的安全審核。明確未成年、拒絕、停止、不舒服、非自願或脅迫、
剝削與人口販運、偷拍或未經同意的私密內容、真實人物性深偽、騷擾、開盒／個資
暴露及非法內容仍固定攔截。

## 輸出屏蔽規則

控制台的帳號設定提供兩種規則：

- `輸出屏蔽詞／詞組`：逐項比對原字、Unicode／全形正規化、插入空白或標點、常見數字替字、重複字元及少量拼寫差異。
- `輸出屏蔽主題`：以自然語言描述禁止提及的概念，額外執行一次語意審核，包含定義、解釋、翻譯、引用、近義改寫、暗示與委婉描述。

生成草稿先做字面與模糊檢查，再做語意審核；不合規時最多重新生成一次。第二次仍不合規、審核逾時或審核結果格式不正確時，系統採失敗即封鎖，不向 Telegram 發送，也不把該草稿寫入對話記憶。回覆與主動發言都會在實際發送前再檢查一次。

設定任何屏蔽規則後，每個候選回覆至少增加一次模型審核呼叫；第一次草稿被拒絕時會再增加重新生成與審核呼叫。自然語言的「意思相近」無法提供數學上的百分之百判定，本系統採保守、審核失敗即不發送的高強度模式。

## 任務資訊

`任務名稱` 與 `任務說明` 會加入該帳號的 system prompt，例如指定聊天重點、
歡迎新成員或回答某類話題。任務不能覆蓋成年、自願、尊重、隱私、不冒充真人及
不捏造經歷等共同規則。

## 媒體任務

控制台可針對每個帳號分別設定：

- 圖片：開關、OpenRouter 圖片模型、每日上限、冷卻秒數、允許群組；預設 `x-ai/grok-imagine-image-quality`。
- 語音：開關、OpenRouter TTS 模型與聲線、每日上限、冷卻秒數、允許群組；預設 `x-ai/grok-voice-tts-1.0`，女性 `eve`、男性 `rex`。
- 影片：開關、OpenRouter 影片模型、每日上限、冷卻秒數、允許群組；預設 `x-ai/grok-imagine-video-1.5`。

只有成員明確要求圖片、語音或影片時，聊天模型才會建立結構化媒體需求。需求會先
經現有屏蔽詞／屏蔽主題與媒體安全審核；圖片生成後再審核圖片，影片完成後則審核
預覽圖，再由該帳號的 Telethon 連線發送。語音文案採台灣繁體口語，OpenRouter
先輸出 MP3，再由容器內的 FFmpeg 轉成 OGG/Opus，以 Telegram 語音訊息送出。
Grok TTS 可讀中文，但供應商目前沒有保證特定台灣口音；本系統能保證文案使用
台灣繁體口語，若必須鎖定特定台灣聲線，可改用保留的 Azure Speech 備援設定。

媒體任務及配額保存在同一個 Railway Volume 的 SQLite。部署重新啟動時，未完成
任務可由帳號背景工作重新接手；每日上限及冷卻在排隊時原子檢查，避免同時訊息
繞過限制。

## 部署

- Railway 必須保留 `/data` Volume，資料庫預設在 `/data/memory.db`。
- 服務保持 **1 Replica**。SQLite Volume 與 Telegram Session 不支援多副本同時登入。
- Railway 會讀取根目錄的 `Dockerfile` 與 `railway.json`，並以 `/health` 驗證新版本啟動完成。
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
- 新帳號預設停用、主動發言關閉且允許群組為空。首次手動啟用只用來載入已加入
  群組；未選允許群組前不會自動回覆，請先在測試群驗證。
- 多個帳號同時收到同一則群訊息時，資料庫只允許其中一個帳號取得回覆權，避免
  重複搶答；最近最多 20 條群訊息（包含近期自身回覆）會納入 24 小時上下文，
  以減少重複笑聲、句型與生活填充句。
- 所有帳號被詢問時必須如實說明是自動互動角色，不是真人會員。
- 角色可參與成人交友話題，但必須以成年、自願、尊重、隱私與安全為前提。
