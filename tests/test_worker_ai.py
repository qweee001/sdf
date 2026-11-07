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


def test_clean_ai_content_strips_thinking_and_english_preamble():
    # 1. 思考標籤去除
    s1 = "<think>思考過程...</think>今晚想吃什麼"
    assert AccountWorker._clean_ai_content(s1) == "今晚想吃什麼"

    s2 = "<thought>Some reasoning in English</thought>\n擦著乳液看你們聊"
    assert AccountWorker._clean_ai_content(s2) == "擦著乳液看你們聊"

    # 2. 模型輸出思考前言（例如 Im thinking through...）自動剝除，保留中文
    s3 = "Im thinking through how to reply.\n\n擦著乳液看你們聊，手都停不下來"
    assert AccountWorker._clean_ai_content(s3) == "擦著乳液看你們聊，手都停不下來"

    s4 = "Initial suspicion is that this is dirty talk.\n妹妹他哪裡持久了"
    assert AccountWorker._clean_ai_content(s4) == "妹妹他哪裡持久了"

    # 3. 普通中文保持不變
    s5 = "先換 LINE 啊，之後有空約出來見面"
    assert AccountWorker._clean_ai_content(s5) == "先換 LINE 啊，之後有空約出來見面"
