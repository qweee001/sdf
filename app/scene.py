"""NSFW 場景狀態機（純邏輯，無 DB／無 Telegram）。

問題：`_tension_ladder_hint()` 是每則訊息照群裡最近十則的露骨密度現算一次的
**分類器**——換目標不清零、真人一停就蒸發、沒有收尾。結果是一萬人群裡永遠
「跟著最熱那句撒尺度」，而不是「跟某個人演完一場」。

這裡補的是「場景對象」與它的生命週期：warm → build → peak → aftercare → closed，
外加 dormant（冷掉但保留記憶）。所有轉移都是純函數，方便單測與 shadow 比對；
真正的持久化與併發在 database.py（CAS），接線在 worker.py。

設計原則：
1. 階段只由代碼判定，模型不自報（它是被要求「演」的，不是被要求「記帳」的）。
2. 一切轉移都要滯回（hysteresis），否則會跟現在的階梯一樣瞬跳。
3. 一定有一件事叫「這一場只做一件事」（goal），build 之後不准再開新元素。
4. 一定關得掉：`SCENE_ENABLED=0` 完全回到舊路徑。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# 階段
WARM = "warm"
BUILD = "build"
PEAK = "peak"
AFTERCARE = "aftercare"
CLOSED = "closed"
DORMANT = "dormant"

OPEN_STAGES = frozenset({WARM, BUILD, PEAK, AFTERCARE})

# 門檻（秒）；與 docs 的設計一致，可用環境變量覆寫
BUILD_AFTER_SECONDS = 90.0        # 進 build 的滯回
BUILD_MIN_EXPLICIT_MSGS = 2       # 6 分鐘內至少兩則帶露骨軸的真人訊息
BUILD_WINDOW_SECONDS = 360.0
PEAK_DENSITY = 0.5                # 真人近十則露骨比例
PEAK_MAX_HITS = 6                 # 峰值發言上限
PEAK_IDLE_SECONDS = 480.0         # 峰值後無新露骨真人 → 收尾
DORMANT_IDLE_SECONDS = 720.0      # 12 分鐘無露骨 → dormant
SCENE_MAX_SECONDS = 1800.0        # 30 分鐘硬上限
AFTERCARE_TO_CLOSE = 1            # 發過一則收尾就關場

# 這一場唯一的「一件事」：只挑場所／道具，不挑體位（體位交給 prompt 現採）
GOALS = (
    "浴室",
    "剛洗完澡的浴室地板",
    "外送到門口的那十秒",
    "沙發縫裡滑下去的手機",
    "車上停紅燈的空檔",
    "床上剛換過的床單",
    "辦公椅前的一個轉身",
    "店裡拉下鐵門之後",
)


@dataclass(frozen=True)
class SceneSignals:
    """判定一次轉移需要的全部現場訊號（都由呼叫方現算，模組不碰 I/O）。"""

    age_seconds: float = 0.0
    stage_age_seconds: float = 0.0
    idle_seconds: float = 0.0           # 距最後一則帶露骨軸的真人訊息
    explicit_msgs_in_window: int = 0
    density: float = 0.0                # 真人近十則的露骨比例
    heat_fired: bool = False            # 話題溫度計是否命中
    peak_hits: int = 0
    aftercare_sent: int = 0


@dataclass(frozen=True)
class Transition:
    stage: str
    reason: str

    @property
    def changed(self) -> bool:
        return bool(self.reason)


def next_stage(stage: str, signals: SceneSignals) -> Transition:
    """回下一步要轉去哪；不需要轉就回同階段 + 空 reason。

    順序刻意如此：硬上限與冷卻優先於升溫，升溫一律要滯回，
    確保「只會往前走或原地不動」，不會在 build/peak 之間來回抖。
    """
    if stage in (CLOSED,):
        return Transition(CLOSED, "")

    # 1) 收尾段先處理：它自己有一套收場條件，不能被下面的絕對上限再打回 aftercare
    if stage == AFTERCARE:
        if signals.aftercare_sent >= AFTERCARE_TO_CLOSE:
            return Transition(CLOSED, "aftercare_done")
        if signals.age_seconds >= SCENE_MAX_SECONDS + 300:
            return Transition(CLOSED, "aftercare_timeout")
        return Transition(AFTERCARE, "")

    # 2) 絕對上限：到點一律收，不硬斷
    if signals.age_seconds >= SCENE_MAX_SECONDS:
        return Transition(AFTERCARE, "max_age")

    # 3) peak 的三個出口：熱度計命中／峰值發言夠了／久無新露骨
    if stage == PEAK:
        if signals.heat_fired:
            return Transition(AFTERCARE, "topic_heat")
        if signals.peak_hits >= PEAK_MAX_HITS:
            return Transition(AFTERCARE, "peak_quota")
        if signals.idle_seconds >= PEAK_IDLE_SECONDS:
            return Transition(AFTERCARE, "peak_idle")
        return Transition(PEAK, "")

    # 4) 冷卻：任何階段久無露骨真人 → dormant（保留 goal，不清零）
    if signals.idle_seconds >= DORMANT_IDLE_SECONDS and stage in (WARM, BUILD):
        return Transition(DORMANT, "idle")

    if stage == DORMANT:
        # 有人又撩起來才回到 warm，不要直接跳回原階段
        if signals.explicit_msgs_in_window >= 1 and signals.idle_seconds < 120:
            return Transition(WARM, "revived")
        return Transition(DORMANT, "")

    # 5) warm → build：需要滯回與複數訊息
    if stage == WARM:
        if (
            signals.stage_age_seconds >= BUILD_AFTER_SECONDS
            and signals.explicit_msgs_in_window >= BUILD_MIN_EXPLICIT_MSGS
        ):
            return Transition(BUILD, "warm_escalated")
        return Transition(WARM, "")

    # 6) build → peak：現場真的在燒且額度未滿
    if stage == BUILD:
        if (
            signals.density >= PEAK_DENSITY
            and not signals.heat_fired
            and signals.peak_hits < PEAK_MAX_HITS
        ):
            return Transition(PEAK, "density")
        return Transition(BUILD, "")

    return Transition(stage, "")


def should_open(
    *, explicit_axes: int, mentioned: bool, booking_asked: bool, cooldown_left: float
) -> str:
    """要不要為這則訊息開一場？回開啟原因或空字串。"""
    if cooldown_left > 0:
        return ""
    if booking_asked:
        return "booking"
    if mentioned:
        return "mentioned"
    if explicit_axes >= 1:
        return "explicit"
    return ""


def sample_goal(rng: random.Random | None = None) -> str:
    return (rng or random).choice(GOALS)


def keeps_goal(stage: str) -> bool:
    """build 之後不准再開新元素——只有 warm 允許換 goal。"""
    return stage not in (BUILD, PEAK, AFTERCARE)


# 階段 → 硬指令。peak 段刻意最露骨、aftercare 段刻意回生活，
# 與 `_tension_ladder_hint` 的舊 L1-L4 並存（無場景時走舊路徑）。
_DIRECTIVES = {
    WARM: (
        "【場景・暖場】：還在做日常，但處處帶鉤。只講「{goal}」這一個場面，"
        "一個微動作或一句身體感覺就收，不准列生活流水帳、不准一次講完。"
    ),
    BUILD: (
        "【場景・升溫】：就在「{goal}」裡，把張力往身體推一步。"
        "只能推進「{goal}」這一件事，不准換場、不准引入第二個新道具；"
        "形容詞最多兩個，句子短。"
    ),
    PEAK: (
        "【場景・釋放】：就在「{goal}」裡放開，講你身上的具體感覺（哪裡濕、緊、燙、軟）"
        "或直接給他一個動作。仍只做「{goal}」這一件事，不扯日常瑣事、不換場。"
    ),
    AFTERCARE: (
        "【場景・收尾】：從「{goal}」退出來，只寫一句：一個生活碎片＋一個身體餘溫"
        "（例：「腳底板冰到發麻，小穴卻還有點熱」）。不開新話題、不再挑釁、"
        "不准再端露骨細節。"
    ),
}


def directive(stage: str, goal: str) -> str:
    """階段的 prompt 指令；closed 給空字串。"""
    template = _DIRECTIVES.get(stage)
    if not template:
        return ""
    return template.format(goal=goal or "這個場面")


def closing_line_hint() -> str:
    return (
        "【收尾後】：這一輪講完就讓場面過去，下一輪若無人續聊就回日常話題，"
        "不要主動再開同一場。"
    )
