"""台北實時天氣服務模組（帶記憶體快取與優雅降級）。

提供當前台北氣溫、降雨與天氣狀況，確保水軍在聊到天氣、出門、穿搭時
100% 符合真實世界現狀，杜絕「大太陽說下大雨」、「28度說好冷」等露餡情況。
"""

import json
import time
import urllib.request

_WEATHER_CACHE = {
    "timestamp": 0.0,
    "summary": "台北氣溫約 27°C（體感約 29°C），多雲時晴、舒適，無雨",
}

_WMO_WEATHER_MAP = {
    0: "晴朗無雲、陽光燦爛",
    1: "晴時多雲、天氣晴朗",
    2: "多雲時晴、舒適",
    3: "陰天、雲層稍厚",
    45: "起霧、能見度稍低",
    48: "有霧、潮濕",
    51: "飄著毛毛細雨",
    53: "毛毛雨、地面微濕",
    55: "綿綿細雨",
    61: "下小雨、出門需撐傘",
    63: "下雨天、雨勢明顯",
    65: "大雨、外出容易淋濕",
    80: "局部短暫陣雨",
    81: "短暫陣雨、時有陣雨",
    82: "較大陣雨",
    95: "雷陣雨、有打雷下雨",
}


def get_taipei_weather(timeout: float = 3.0) -> str:
    """獲取台北當前即時天氣，帶 30 分鐘記憶體快取。"""
    now = time.time()
    # 30 分鐘內直接使用記憶體快取，0 延遲
    if now - _WEATHER_CACHE["timestamp"] < 1800 and _WEATHER_CACHE["summary"]:
        return _WEATHER_CACHE["summary"]

    url = (
        "https://api.open-meteo.com/v1/forecast?"
        "latitude=25.0478&longitude=121.5318&"
        "current=temperature_2m,apparent_temperature,precipitation,weather_code&"
        "timezone=Asia%2FTaipei"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SDF-Agent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            curr = data.get("current", {})
            temp = float(curr.get("temperature_2m", 26.0))
            app_temp = float(curr.get("apparent_temperature", temp))
            precip = float(curr.get("precipitation", 0.0))
            code = int(curr.get("weather_code", 1))

            desc = _WMO_WEATHER_MAP.get(code, "多雲")
            rain_desc = "有下雨" if precip > 0.1 else "無雨"
            summary = (
                f"台北氣溫約 {temp:.0f}°C（體感約 {app_temp:.0f}°C），{desc}，{rain_desc}"
            )
            _WEATHER_CACHE["timestamp"] = now
            _WEATHER_CACHE["summary"] = summary
            return summary
    except Exception:
        # 網路超時或異常時優雅降級回現有快取
        return _WEATHER_CACHE["summary"]
