"""外語碎片守門：模型跑掉時吐出的假名／孟加拉／拉丁殘句不得進群。

背景：換模型後群裡真的出現過「群馬の精液は直接流し込む」「天天開心 এসেছেappliquée」
「喝完KV就ங்க」「和進出口 我 convertir喲」——全都含 2 個以上漢字，
舊的 cjk_count < 2 底線完全擋不住。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.text_guard import is_foreign_debris


def test_real_failures_are_caught():
    for text in (
        "群馬の精液は直接流し込む",
        "天天開心 এসেছেappliquée",
        "喝完KV就ங்க",
        "和進出口 我 convertir喲",
        "점심 뭐 먹지",
        "สวัสดีครับ",
    ):
        assert is_foreign_debris(text) is True, text


def test_normal_taiwan_lines_pass():
    for text in (
        "你想吃",
        "地暖你就陪我去吃飯",
        "奶頭硬硬的，你忍得住嗎",
        "下午吃甜點配咖啡，等下看誰能把我餵飽",
        "OK 我先去洗澡",
        "IG 帳號被鎖了",
        "方 sir 眼光好好",
        "心情放軟的時候最容易被親破防",
    ):
        assert is_foreign_debris(text) is False, text


def test_empty_is_not_debris():
    assert is_foreign_debris("") is False
