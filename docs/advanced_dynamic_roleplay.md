# SDF 高階動態演繹（Advanced Dynamic Roleplay）實裝報告

> 依據業界成熟標準（SillyTavern 角色卡 / World Info、多角色互動敘事系統
> Sense Murmur / Reverie、炒群劇本引擎）審查後，六大優化全部落地。

---

## 一、系統現況回顧（已完成）

1. **動態去重與解碼懲罰**：歷史視窗 30 則負向注入；`temperature 0.95`、
   `frequency_penalty 0.45`、`presence_penalty 0.35`。
2. **多帳號風格切換**：小小（28 歲金融）、小天後（30 歲櫃姐）、
   RICH（28 歲設計）、Chen（38 歲服飾店老闆娘）。
3. **文本與常識防護**：大陸用語替換庫、性別視角校正。
4. **地區標籤移除**：不再把角色綁死在特定行政區。

---

## 二、六大高階動態演繹優化（本次實裝）

### 優化 1｜性別視角硬校驗（Gender Perspective Guard）
**檔案**：`app/text_guard.py`

新增 `_female_male_perspective_violation()`，掛在 `gender_term_violation()`
最前面優先攔截。女性帳號一旦出現「男性視角」句型即攔掉重新生成：

- **器官自稱**：我雞巴／我老二／我大鵰／本屌／老子／我硬了／我射了
- **把器官給對方**：哥哥的雞巴／我的屌／給你吸／幫我吸／吹我的
- **命令對方舔跪**：舔就跪／舌頭別停／舔到跪好／跪著舔
- **荒謬口活**：吸到射／射給你／餵我吃到哭

**修掉的真實案例**（10-21 線上）：
> 小小（女）：`沒胸就多練嘴活，哥哥的雞巴給你吸到射`
> 小小（女）：`那你過來剝光 餵我吃到哭`
> 小小（女）：`想舔就跪好，舌頭別停`

同時在 `mainland_tw_terms.txt` 加入 `雞吧=屌`、`嘴活=口技`、`口活=口技`。

---

### 優化 2｜動態輪替配額制（Speaker Share Cap）
**檔案**：`app/worker.py` → `_apply_speaker_share_cap()`

取代原本固定「每帳號最多 3 則」的硬上限：

| 參數 | 值 | 說明 |
|---|---|---|
| `_SPEAKER_SHARE_CAP` | 0.45 | 單一帳號近期水軍發言佔比不得超過 45% |
| `_SPEAKER_HARD_CAP` | 3 | 份額比例全超標時的絕對次數保險絲 |

**修掉的真實案例**（10-21 線上抽樣 13 則水軍訊息）：
- 小小 9 則（**69%**）→ 小天後 3 則 → Chen 1 則 → **RICH 0 則**

---

### 優化 3｜跨輪記憶錨點（Memory Anchor）
**檔案**：`app/worker.py` → `_memory_anchors()` / `_memory_anchor_hint()`

從近期 24 則真人訊息中，用 34 個生活事件關鍵詞（感冒／加班／交稿／提案／
面試／搬家／生理期…）抽出「值得被記住的事」，注入提示詞要求自然追問。

**解決的問題**：群友十分鐘前說「今天感冒了」，半小時後水軍完全當作沒發生。
真人姐妹會追問「後來有比較好了嗎」。

**零成本設計**：純關鍵字擷取，不額外呼叫模型（延遲與費用都要顧）。

---

### 優化 4｜話題溫度計（Topic Heat Cooldown）
**檔案**：`app/worker.py` → `_topic_heat_hint()`

統計最近 8 則訊息中的色情詞濃度（30 個關鍵詞），連續 **≥4 則** 過熱就
強制注入冷卻指令，命令模型這一輪改講日常瑣事（外送／晚餐／追劇／生理期）。

**解決的問題**：`買保險套 → 蕾絲 → 舔 → 妹妹懷孕 → 推屁股` 的色情閉環。
真人聊完色情會自然膩掉轉回生活，只有機器人會無限打轉。

---

### 優化 5｜多角色社交動力學（Social Dynamics）
**檔案**：`app/worker.py` → `_social_dynamics_hint()`

三項真人特徵注入：

1. **不接話權**：群友講話沒爆點時，可只回表情／短評／講自己的事
2. **吐槽拆台**：上一則是姊妹說的 → 優先吐槽質疑，不許一味捧場
3. **小圈圈（Crew）**：
   - `小小` + `小天後` = **毒舌姐妹淘**（講話更熟、更酸、不客套）
   - `RICH` + `Chen` = **輕熟氣質組**（講話更內斂、更有距離感）

---

### 優化 6｜台灣腔原生語音條資產庫（Voice Note Library）
**新增檔案**：
- `app/assets/voices/manifest.json`（4 帳號 × 4 場景 = 16 條台詞）
- `app/assets/voices/{xiao,tian,rich,chen}/`（OGG 素材目錄）
- `app/worker.py` → `_send_group_voice()` / `_maybe_voice_verify()` /
  `_voice_duration_seconds()`
- `app/manager.py` → `_load_voice_library()`

**技術規格（絕不露餡）**：
| 項目 | 規格 |
|---|---|
| 容器 | `audio/ogg` |
| 編碼 | `opus`（48kHz 單聲道） |
| Telegram | `DocumentAttributeAudio(voice=True)` → 原生圓形播放器＋波形條 |
| 長度 | 3~8 秒（真人黃金長度，絕不長篇演講） |

**觸發邏輯**：
- **被質疑真人**（機器人／真假／發語音／證明…）→ **80%** 機率甩語音條打臉
- 只在被 @ 或被回覆時觸發（避免群裡別人問也搶著自證）

**16 條台詞已按角色年齡校正**（小小 28 歲金融輕熟女，非 22 歲學生妹）。

---

## 三、語音資產部署（可選，未部署則自動靜默停用）

```bash
# 1. 把真實台灣女生錄製的 OGG 檔放進對應目錄
app/assets/voices/xiao/xiao_verify.ogg
app/assets/voices/tian/tian_verify.ogg
app/assets/voices/rich/rich_verify.ogg
app/assets/voices/chen/chen_verify.ogg

# 2. 轉檔規格（ffmpeg）
ffmpeg -i input.m4a -c:a libopus -b:a 32k -ar 48000 -ac 1 -f ogg output.ogg

# 3. 部署到 Railway volume，並設 VOICE_ASSETS_DIR=/data/voice-assets
```

沒有素材時：`_load_voice_library()` 回 `None`，`_maybe_voice_verify()` 直接
回 `False`，文字／貼圖／照片等其他能力完全不受影響。

---

## 四、參數總表

```python
# app/worker.py
_SPEAKER_SHARE_CAP = 0.45          # 單帳號發言份額上限
_SPEAKER_HARD_CAP = 3              # 絕對次數保險絲
_TOPIC_HEAT_MAX_ROUNDS = 4         # 色情話題過熱閾值
_VOICE_VERIFY_PROBABILITY = 0.80   # 被質疑真人時甩語音條機率
_MAX_TOPIC_TURNS = 10              # 單話題水軍最多接話數

# app/config.py
ai_temperature = 0.95              # 採樣發散度
# API 呼叫
frequency_penalty = 0.45           # 頻率懲罰
presence_penalty = 0.35            # 存在懲罰
```

---

## 五、驗證

```bash
python -m pytest tests/ -q
# 656 passed
```
