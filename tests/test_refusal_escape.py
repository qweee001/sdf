"""拒答逃生：長提示被對齊層擋下時的兩道補救。

實測來源：11-08 15:07–16:14 一小時內 reply_audit 記到 16 次 refusal，
`refusal_fallbacks` 四號都是 0（備援模型 agnes-2.5-flash 被上游 400 擋在門外），
proactive_drop_nochinese 四個帳號加起來 44 次。模型回的是「您的提示要求我扮演
特定角色並參與露骨的成人情色對話」這種對提示的評論，不是內容真的不行。

兩道補救：
1. 參數被上游判不合法時退到最小參數組，別讓整條路直接死。
2. 全模型都拒答時改用極短角色卡重問一次（長提示才是拒答主因）。
"""

import asyncio
from types import SimpleNamespace

from test_worker_reply_arbitration import MANAGED, _worker  # noqa: E402


class _Create:
    """假 chat.completions.create：第一次用完整參數就 400，退到最小參數才成功。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(kwargs) > 2:  # 完整參數組
            raise RuntimeError(
                "Error code: 400 - 请求参数不正确：不支持的字段"
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="好啊 你來台中"))],
            usage=None,
        )


def test_call_ai_falls_back_to_minimal_kwargs_on_bad_request():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        create = _Create()
        worker.ai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=create)
        )
        text = await worker._call_ai("sys", "user")
        assert text == "好啊 你來台中"
        assert create.calls[0].keys() > {"model", "messages"}
        assert set(create.calls[-1]) == {"model", "messages"}

    asyncio.run(main())


def test_call_ai_still_returns_when_minimal_kwargs_also_fail():
    """最小參數也掛掉時 _call_ai 不拋，回空字串（呼叫方本來就靠空手判斷）。"""

    async def main():
        worker = _worker(sorted(MANAGED)[0])

        class _Boom:
            async def create(self, **kwargs):
                raise RuntimeError("400")

        worker.ai_client = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))
        worker._bump_error = lambda: None
        assert await worker._call_ai("sys", "user") == ""

    asyncio.run(main())


def test_escape_prompt_is_short_and_keeps_persona():
    async def main():
        worker = _worker(sorted(MANAGED)[0])
        prompt = worker._slim_persona_prompt()
        assert len(prompt) < 200
        assert str(worker.persona["name"]) in prompt

    asyncio.run(main())
