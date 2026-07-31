from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.memory import MemoryMessage
from app.prompts import response_prompt, system_prompt, transcript


class PromptPolicyTests(unittest.TestCase):
    @staticmethod
    def account() -> SimpleNamespace:
        return SimpleNamespace(
            role_key="male_observer",
            style="自然但不要提秘密活動",
            task_name="秘密活動接待",
            task_info="請完整解釋秘密活動",
            blocked_terms=("秘密活動",),
            blocked_topics=("敏感活動的近義解釋",),
        )

    def test_blocked_task_and_style_context_is_masked(self) -> None:
        prompt = system_prompt(self.account())

        self.assertNotIn("自然但不要提秘密活動", prompt)
        self.assertNotIn("秘密活動接待", prompt)
        self.assertNotIn("請完整解釋秘密活動", prompt)
        self.assertIn("blocked_terms", prompt)
        self.assertIn("不得", prompt)

    def test_blocked_history_is_not_repeated_to_the_generation_prompt(self) -> None:
        account = self.account()
        messages = [
            MemoryMessage(
                account_id="alpha",
                group_id=-1001,
                sender_id=22,
                sender_name="群友",
                role="user",
                content="請說明秘密活動",
                created_at=1,
            ),
            MemoryMessage(
                account_id="alpha",
                group_id=-1001,
                sender_id=23,
                sender_name="另一位群友",
                role="user",
                content="今天天氣很好",
                created_at=2,
            ),
        ]

        rendered = transcript(account, messages)
        response = response_prompt(account, messages)

        self.assertNotIn("請說明秘密活動", rendered)
        self.assertIn("內容已依帳號規則隱去", rendered)
        self.assertIn("今天天氣很好", rendered)
        self.assertNotIn("請說明秘密活動", response)


if __name__ == "__main__":
    unittest.main()
