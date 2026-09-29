"""提示詞衛生：離開本機的提示詞不得夾帶元層對抗聲明。

「無安全審核」「無尺度限制」「不受任何限制」這類元層聲明本身就是模型安全
分類器的高危特徵，寫得越強硬、被攔或拒答的機率越高；真正有效的是角色口吻
的台詞示範（見 persona.get_system_prompt 的「你聊天的樣子」段）。

這條規則適用於每一段送給外部模型的文字，也適用於模組說明——後者沒有行為
影響，卻是最容易被順手寫回去的地方，所以一併鎖住。
"""

import ast
from pathlib import Path

from app.media import OrcaMediaService
from app.persona import generate_persona, get_system_prompt
from app.worker import _SEMANTIC_GROUP_META_SYSTEM_PROMPT

_META_DECLARATIONS = (
    "無安全審核",
    "无安全审核",
    "無尺度限制",
    "无尺度限制",
    "不受限制",
    "不受任何限制",
    "沒有任何限制",
    "没有任何限制",
    "無審查",
    "无审查",
    "無過濾",
    "无过滤",
    "不設限",
    "不设限",
    "越獄",
    "越狱",
    "jailbreak",
    "unrestricted",
    "uncensored",
    "no restrictions",
    "without restrictions",
    "anything goes",
    "no limits",
)

_APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _assert_clean(label: str, text: str) -> None:
    haystack = text.casefold()
    for banned in _META_DECLARATIONS:
        assert banned.casefold() not in haystack, (
            f"{label} 夾帶元層對抗聲明：{banned}"
        )


def test_persona_system_prompt_is_clean():
    _assert_clean("get_system_prompt", get_system_prompt(generate_persona()))


def test_media_generation_policies_are_clean():
    """圖/影片的合規護欄要維持「正面邊界」寫法，不可改成無限制宣告。"""
    _assert_clean(
        "_SUGGESTIVE_IMAGE_POLICY", OrcaMediaService._SUGGESTIVE_IMAGE_POLICY
    )
    _assert_clean(
        "_SUGGESTIVE_VIDEO_POLICY", OrcaMediaService._SUGGESTIVE_VIDEO_POLICY
    )


def test_group_meta_classifier_prompt_is_clean():
    _assert_clean(
        "_SEMANTIC_GROUP_META_SYSTEM_PROMPT", _SEMANTIC_GROUP_META_SYSTEM_PROMPT
    )


def test_app_module_docstrings_are_clean():
    covered = 0
    for path in sorted(_APP_DIR.glob("*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        if doc:
            covered += 1
            _assert_clean(f"app/{path.name} 模組說明", doc)
    assert covered >= 5, "模組說明掃描沒有實際覆蓋到檔案"
