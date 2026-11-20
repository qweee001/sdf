# 可靠性收斂與場景狀態機（2026-11-19）

這一輪修的是「**功能死亡卻全綠**」：11-19 線上四個號的回覆＋主動同時被關，`/health`
自己算出 `degraded`（`muted: 4`、45 小時真空、`ai_calls_24h: 0`）卻照樣回 200，
Railway 只認 2xx，於是容器一路綠燈，直到人肉發現。

## 一、健康度分家：`/health` 給人、`/ready` 給平台

| 端點 | 語義 | 狀態碼 |
|---|---|---|
| `/health` | 詳情：DB 探測、帳號數、靜音數、24h 模型呼叫、失敗原因計數 | 永遠 200 |
| `/ready` | 同一份判定，但真的要命才 503 | 200 / 503 |

- `/ready` 啟動寬限 `READY_GRACE_SECONDS`（默認 300 秒）：剛起來還沒有機器人訊息，
  沒有寬限會一啟動就判死、一路重啟到 healthcheckTimeout。
- `railway.json` 的 `healthcheckPath` 已改指 `/ready`。
- 外部輪詢用 `python tools/ready_probe.py <base_url>`，非 200 即非 0 退出。
- 全靜音是正常開關，但全靜音＋長時間真空時 503 會讓平台重啟 10 次（
  `restartPolicyMaxRetries`）後停住——重啟不會解除靜音（旗標在 DB），失敗是預期的。

## 二、指標要能自證

`images_understood` 只在「生成成功且送出」那一刻 +1，所以 0 同時可能代表
「圖下載失敗」「模型看不懂」「回覆被閘掉」。現在拆成三段並進 `/health.metrics`：

- `img_seen` / `img_fetched` / `img_vision_ok` / `img_vision_empty` / `img_vision_err` /
  `img_rejected`（`img_none_<reason>` 的加總）
- `reply_drops`、`reply_skips`：放棄與跳過的原因分佈
- `loop_lag_ms`：事件循環延遲峰值

## 三、單進程爆炸半徑

四條 `_proactive_loop` / `_memory_cleanup_loop` / `_loop_lag_probe` 全部走
`_spawn_supervised()`：任務非正常結束會記 `task_death_*`、`task_last_death`，
並以 2→60 秒指數退避重拉；`stop()` 的正常取消不算事故。
`on_message` 套了 `_safe_on_message()` 外殼，一則訊息的例外不會帶走整條收訊線。

## 四、場景狀態機（`app/scene.py` + `group_scenes` 表）

舊的 `_tension_ladder_hint()` 是**每則現算的分類器**：換目標不清零、真人一停就蒸發、
沒有收尾。現在有了場景對象：

```
warm → build → peak → aftercare → closed
                ↘ dormant（冷掉但保留 goal）
```

- 階段只由代碼判定（模型不自報），轉移全部帶滯回與硬上限
- 同群同時只允許一場（`idx_group_scenes_open` 部分唯一索引）
- 推進用 CAS（`transition_scene` 的 `WHERE stage = ?`），多號併發搶輸的一方放棄
- 開場抽一個 `goal`（「這一場只做一件事」），build 之後不准換場
- aftercare 只准一句：一個生活碎片＋一個身體餘溫，然後關場
- 冷卻 `SCENE_COOLDOWN_SECONDS`（默認 1800 秒）內同群不重開

**開關**：`SCENE_ENABLED=0`（默認，完全不注入）／`SCENE_SHADOW=1`（只算狀態、寫 stats、
打 `scene-shadow` 日誌，仍走舊階梯）。順序是 shadow 看分歧 → 再 enabled。

## 五、守門分層

`text_guard` 現在把破綻分成 `HARD_VIOLATIONS`（簡繁、時段、字數、格式殘留、性別詞、
無中文）與 `SOFT_VIOLATIONS`（拒答腔、群務語意、複讀、口頭禪、節拍不足、主語斷裂）。
只有軟語感不合格時，默認只計 `guard_soft_only`；`GUARD_SOFT_ENFORCE=1` 才真放行
（那種句子重寫四五秒往往還是同一句）。硬結構永遠擋。

## 六、其他

- `_proactive_loop` 在主動全關時只低頻喚醒（120 秒），不再跑整套 tick
- 啟動時自檢 `VISION_MODEL` 是否在白名單，否則大聲警告（白名單外會靜默失敗）
- `.env.example` 補齊新變量；`analysis_msgs.json` 改存 gz（809KB → 122KB）
- `_setup_routes` 798 行拆成五段（公開／讀／群組寫入／自檢／帳號），static mount
  仍留在尾巴，否則掛在 `/` 的 static 會吃掉 API
- `_topic_heat_hint` / `_tension_ladder_hint` / `_social_dynamics_hint` 的實作移到
  `app/reply_hints.py`，worker 的 method 只剩一行委託

## 七、成本走勢

`GET /api/metrics/hourly?hours=24`（需登入）回逐小時 `calls` / `retries` /
`avg_latency_ms`：`/health` 給的是當下總量，看不出「22 點開始每小時掉到 0」
或「延遲從 2 秒爬到 18 秒」，走勢要看這支。`hours` 夾在 1–168。

## 八、看圖入口與計數修正（同日）

`img_seen` 原本在 `on_message` 與 `_generate_reply` 兩處各加一次，而且
`_incoming_image` 把「大小未知」(`file.size` 為 0／None) 直接判成 `bad_size`——
等於整批照片連下載都不試。修正：

- 大小未知 → 照樣下載，下載後再用 `len(data)` 判上限
- 已知過大 → `too_big_known`，不再與 `bad_size` 混用
- `images_seen` 只代表「群裡出現的照片」（`on_message`），新增 `img_admitted`
  代表「真的進入生成」，兩者相減才是「沒輪到回覆」
- `/health.metrics` 增列 `img_admitted` 與 `img_reject_reasons`（逐原因），
  不再只有一個 `img_rejected` 總數

## 九、事件循環卡頓（loop_lag 3373ms）

上線後探針抓到 `loop_lag_ms` 一路從 2894 爬到 3373：三秒多的期間四個號一起停止收訊，
而舊的探針只有一個毫秒數字，查不出是哪一行。兩件事一起做：

1. **留證據**：卡頓 ≥800ms 時抓當下 event loop 執行緒的堆疊（`capture_loop_stack`），
   寫進 `loop_lag_last_stack` 並打日誌，60 秒最多一次。下次再卡就是有名有姓的 frame。
2. **先削一刀**：長壽命進程的全域 GC 每次要掃過幾十萬個長壽物件，gen2 一次可以停數秒。
   啟動完成後 `gc.collect() + gc.freeze()`（把已建好的物件搬進永久代不再掃），
   並把門檻放寬到 `(50000, 500, 500)` 降低短命配置的 churn。`GC_TUNE=0` 可關。

freeze 只解「GC 造成的停頓」；若堆疊顯示是某段純 CPU（例如災難性回溯的正則），
下一步就是把那段搬到 `asyncio.to_thread`——現在有堆疊可以直接指名，不必再猜。
