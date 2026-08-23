import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.worker import AccountWorker


def test_call_ai_disables_thinking_when_configured():
    async def main():
        create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="可以啊，先聊聊看"))]
        ))
        ai_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        config = SimpleNamespace(
            ai_model="obsidian/Qwen3.8-27B",
            ai_temperature=0.85,
            ai_max_tokens=200,
            ai_timeout=60,
            ai_disable_thinking=True,
        )
        worker = AccountWorker(
            account_id="acct",
            session_key="session",
            tg_api_id=1,
            tg_api_hash="hash",
            ai_client=ai_client,
            db=None,
            config=config,
            managed_ids=set(),
            on_status_change=None,
            persona={
                "name": "測試",
                "gender": "女",
                "age": 29,
                "city": "台中",
                "district": "北屯",
                "industry": "上班族",
                "university": "逢甲",
                "personality": "自然",
                "hobbies": ["喝咖啡"],
                "looking_for": "想認識人",
                "meetups_done": 1,
                "schedule": "正常",
            },
        )

        result = await worker._call_ai("system", "user")

        assert result == "可以啊，先聊聊看"
        kwargs = create.await_args.kwargs
        assert kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    asyncio.run(main())
