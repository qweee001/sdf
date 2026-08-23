import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import load_settings
from app.worker import AccountWorker


MANAGED = {101, 202, 303, 404}


class _ClaimDB:
    def __init__(self):
        self.claims = set()
        self.text_claims = set()
        self.text_claim_lock = asyncio.Lock()
        self.followup_claims = set()
        self.followup_pending = {}
        self.followup_completed = []
        self.followup_released = []
        self.messages = []
        self.activities = []

    async def claim_message_response(
        self, group_id: int, message_id: int, account_id: str
    ) -> bool:
        key = (group_id, message_id)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def claim_group_text(
        self, group_id: int, text: str, account_id: str, window_seconds=3600
    ) -> bool:
        async with self.text_claim_lock:
            key = (group_id, "".join(text.split()).casefold())
            if key in self.text_claims:
                return False
            self.text_claims.add(key)
            return True

    async def claim_managed_followup(
        self, group_id, message_id, account_id, cooldown_seconds=600
    ):
        key = (group_id, message_id)
        if key in self.followup_claims:
            return False
        self.followup_claims.add(key)
        return True

    async def reserve_managed_followup(
        self, group_id, message_id, account_id,
        pending_seconds=120, cooldown_seconds=600,
    ):
        key = (group_id, message_id)
        if key in self.followup_claims or group_id in self.followup_pending:
            return False
        self.followup_claims.add(key)
        self.followup_pending[group_id] = (message_id, account_id)
        return True

    async def complete_managed_followup(
        self, group_id, message_id, account_id, cooldown_seconds=600,
    ):
        if self.followup_pending.get(group_id) != (message_id, account_id):
            return False
        self.followup_pending.pop(group_id, None)
        self.followup_completed.append((group_id, message_id, account_id))
        return True

    async def release_managed_followup(self, group_id, message_id, account_id):
        self.followup_claims.discard((group_id, message_id))
        if self.followup_pending.get(group_id) == (message_id, account_id):
            self.followup_pending.pop(group_id, None)
        self.followup_released.append((group_id, message_id, account_id))

    async def add_message(self, *args):
        self.messages.append(args)

    async def touch_activity(self, *args):
        self.activities.append(args)


def _worker(
    tg_user_id: int,
    active_ids: set[int] | None = None,
    db=None,
    managed_origins=None,
    human_owners=None,
    recent_proactive_owners=None,
    last_human_activity=None,
    base_reply_probability=1.0,
) -> AccountWorker:
    config = load_settings()
    config.base_reply_probability = base_reply_probability
    config.min_typing_delay = 0.0
    config.max_typing_delay = 0.0
    config.water_cross_talk_probability = 1.0
    worker = AccountWorker(
        account_id=str(tg_user_id),
        session_key="session",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=SimpleNamespace(),
        db=db or _ClaimDB(),
        config=config,
        managed_ids=set(MANAGED),
        active_ids=set(MANAGED) if active_ids is None else active_ids,
        managed_origins={} if managed_origins is None else managed_origins,
        human_owners={} if human_owners is None else human_owners,
        recent_proactive_owners=(
            {} if recent_proactive_owners is None else recent_proactive_owners
        ),
        last_human_activity=(
            {} if last_human_activity is None else last_human_activity
        ),
        on_status_change=None,
        persona={
            "name": f"帳號{tg_user_id}", "gender": "女", "age": 25,
            "city": "台中", "district": "北屯", "industry": "上班族",
            "university": "中興", "personality": "直爽", "hobbies": ["咖啡"],
            "looking_for": "想認識人", "meetups_done": 1, "schedule": "正常",
        },
        selected_groups=[-5428680940],
    )
    worker.tg_user_id = tg_user_id
    return worker


def _event(*, sender_id=999, message_id=77, mentioned=False,
           is_reply=False, reply_sender_id=None, text="今晚有人想聊天嗎"):
    event = SimpleNamespace(
        sender_id=sender_id,
        chat_id=-5428680940,
        id=message_id,
        mentioned=mentioned,
        is_reply=is_reply,
        reply_to=SimpleNamespace(reply_to_msg_id=66) if is_reply else None,
        raw_text=text,
    )
    event.get_reply_message = AsyncMock(
        return_value=SimpleNamespace(sender_id=reply_sender_id)
        if reply_sender_id is not None else None
    )
    return event


def test_ordinary_human_message_has_at_most_one_responder():
    async def main():
        event = _event()
        db = _ClaimDB()
        decisions = [
            await _worker(uid, db=db)._should_reply(event)
            for uid in sorted(MANAGED)
        ]
        assert decisions.count(True) == 1

    asyncio.run(main())


def test_unregistered_managed_message_never_triggers_other_accounts():
    async def main():
        event = _event(sender_id=101)
        with patch("app.worker.random.random", return_value=0.0):
            decisions = [await _worker(uid)._should_reply(event) for uid in sorted(MANAGED)]
        assert decisions == [False, False, False, False]

    asyncio.run(main())


def test_proactive_origin_allows_exactly_one_adaptive_followup():
    async def main():
        db = _ClaimDB()
        active = set(MANAGED)
        origins = {
            (-5428680940, 101, "今晚有人想聊天嗎"): float("inf")
        }
        event = _event(sender_id=101, message_id=501)
        workers = [
            _worker(uid, active, db=db, managed_origins=origins)
            for uid in sorted(MANAGED)
        ]

        decisions = [await worker._should_reply(event) for worker in workers]

        assert decisions.count(True) == 1
        assert decisions[0] is False
        assert origins == {}

        # 接話內容沒有被登記成新 origin，因此不能觸發第三輪。
        followup = _event(
            sender_id=202, message_id=502, text="我剛好也還醒著啊"
        )
        again = [await worker._should_reply(followup) for worker in workers]
        assert again == [False, False, False, False]

    asyncio.run(main())


def test_reply_is_answered_only_by_the_replied_to_account():
    async def main():
        event = _event(is_reply=True, reply_sender_id=303)
        db = _ClaimDB()
        decisions = [
            await _worker(uid, db=db)._should_reply(event)
            for uid in sorted(MANAGED)
        ]
        assert decisions == [False, False, True, False]
        assert all(_worker(uid).tg_user_id in MANAGED for uid in MANAGED)

    asyncio.run(main())


def test_atomic_claim_survives_different_active_views():
    async def main():
        event = _event(message_id=91)
        db = _ClaimDB()
        first = _worker(101, {101, 303}, db=db)
        second = _worker(303, {101, 202, 303}, db=db)
        decisions = [
            await first._should_reply(event),
            await second._should_reply(event),
        ]
        assert decisions.count(True) == 1

    asyncio.run(main())


def test_stop_cancels_delayed_replies_and_removes_active_id():
    async def main():
        active = {101}
        worker = _worker(101, active)
        worker.is_running = True
        worker.tg_client = SimpleNamespace(disconnect=AsyncMock())

        async def sleeper():
            await asyncio.sleep(3600)

        task = asyncio.create_task(sleeper())
        worker._reply_tasks.add(task)
        await worker.stop()

        assert task.cancelled()
        assert worker._reply_tasks == set()
        assert active == set()
        assert worker.tg_client is None

    asyncio.run(main())


def test_stop_waits_for_inflight_send_and_database_record():
    async def main():
        db = _ClaimDB()
        worker = _worker(101, {101}, db=db)
        send_started = asyncio.Event()
        allow_send_finish = asyncio.Event()

        async def send_message(_chat_id, _text):
            send_started.set()
            await allow_send_finish.wait()

        client = SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            disconnect=AsyncMock(),
        )
        worker.tg_client = client
        worker.is_running = True

        send_task = asyncio.create_task(
            worker._send_text_recorded(
                -5428680940,
                "測試訊息",
                activity_kind="reply",
                stats_key="replies_sent",
            )
        )
        await send_started.wait()
        stop_task = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)
        assert stop_task.done() is False

        allow_send_finish.set()
        assert await send_task is True
        await stop_task

        assert len(db.messages) == 1
        assert len(db.activities) == 1
        assert worker.stats["replies_sent"] == 1
        assert worker.tg_client is None

    asyncio.run(main())


def test_send_rechecks_group_after_hot_whitelist_removal():
    async def main():
        worker = _worker(101, {101}, db=_ClaimDB())
        client = SimpleNamespace(
            send_message=AsyncMock(),
            disconnect=AsyncMock(),
        )
        worker.tg_client = client
        worker.is_running = True
        typing_started = asyncio.Event()
        allow_typing_finish = asyncio.Event()

        async def controlled_sleep(_seconds):
            typing_started.set()
            await allow_typing_finish.wait()

        with patch("app.worker.asyncio.sleep", side_effect=controlled_sleep):
            send_task = asyncio.create_task(
                worker._send_text_recorded(
                    -5428680940,
                    "測試訊息",
                    activity_kind="reply",
                    stats_key="replies_sent",
                )
            )
            await typing_started.wait()
            worker.selected_groups.clear()
            allow_typing_finish.set()
            assert await send_task is False

        client.send_message.assert_not_awaited()
        assert worker.stats["replies_sent"] == 0

    asyncio.run(main())


def test_concurrent_accounts_atomically_claim_identical_text():
    async def main():
        db = _ClaimDB()
        first = _worker(101, {101, 202}, db=db)
        second = _worker(202, {101, 202}, db=db)
        first_client = SimpleNamespace(
            send_message=AsyncMock(), disconnect=AsyncMock()
        )
        second_client = SimpleNamespace(
            send_message=AsyncMock(), disconnect=AsyncMock()
        )
        first.tg_client = first_client
        second.tg_client = second_client
        first.is_running = True
        second.is_running = True

        results = await asyncio.gather(
            first._send_text_recorded(
                -5428680940,
                "今晚要不要一起聊聊",
                activity_kind="reply",
                stats_key="replies_sent",
            ),
            second._send_text_recorded(
                -5428680940,
                "今晚要不要一起聊聊",
                activity_kind="reply",
                stats_key="replies_sent",
            ),
        )

        assert results.count(True) == 1
        assert (
            first_client.send_message.await_count
            + second_client.send_message.await_count
        ) == 1

    asyncio.run(main())


def test_only_proactive_send_registers_a_managed_origin():
    async def main():
        origins = {}
        worker = _worker(101, {101, 202}, db=_ClaimDB(), managed_origins=origins)
        worker.tg_client = SimpleNamespace(
            send_message=AsyncMock(), disconnect=AsyncMock()
        )
        worker.is_running = True

        assert await worker._send_text_recorded(
            -5428680940,
            "今晚有人想聊天嗎？",
            activity_kind="proactive",
            stats_key="proactive_sent",
            managed_origin=True,
        )
        assert (-5428680940, 101, "今晚有人想聊天嗎") in origins

        origins.clear()
        assert await worker._send_text_recorded(
            -5428680940,
            "我剛好也還醒著",
            activity_kind="followup",
            stats_key="replies_sent",
        )
        assert origins == {}

    asyncio.run(main())


def test_on_message_schedules_one_managed_followup_mode():
    async def main():
        db = _ClaimDB()
        active = set(MANAGED)
        origins = {
            (-5428680940, 101, "今晚有人想聊天嗎"): float("inf")
        }
        event = _event(sender_id=101, message_id=601)
        event.is_private = False
        event.is_group = True
        event.get_sender = AsyncMock(
            return_value=SimpleNamespace(first_name="主動帳號", last_name=None)
        )
        calls = []
        workers = [
            _worker(uid, active, db=db, managed_origins=origins)
            for uid in sorted(MANAGED)
        ]
        for worker in workers:
            worker.is_running = True
            worker.tg_client = object()
            worker._schedule_reply = (
                lambda _event, _delay, *, managed_followup=False, uid=worker.tg_user_id:
                calls.append((uid, managed_followup))
            )
            await worker.on_message(event)

        assert len(calls) == 1
        assert calls[0][1] is True

    asyncio.run(main())


def test_managed_empty_generation_uses_fallback_and_commits_after_send():
    async def main():
        db = _ClaimDB()
        worker = _worker(202, {101, 202}, db=db)
        worker.tg_client = SimpleNamespace(
            send_message=AsyncMock(), disconnect=AsyncMock()
        )
        worker.is_running = True
        event = _event(sender_id=101, message_id=701, text="今天穿很好看的衣服")
        await db.reserve_managed_followup(-5428680940, 701, worker.account_id)
        worker._generate_reply = AsyncMock(return_value="")

        await worker._reply_later(event, 0, managed_followup=True)

        worker.tg_client.send_message.assert_awaited_once()
        assert db.followup_completed == [(-5428680940, 701, "202")]
        assert db.followup_released == []
        assert worker.stats["managed_fallbacks"] == 1
        assert worker.stats["managed_sent"] == 1

    asyncio.run(main())


def test_managed_send_failure_releases_without_committing_cooldown():
    async def main():
        db = _ClaimDB()
        worker = _worker(202, {101, 202}, db=db)
        worker.tg_client = SimpleNamespace(
            send_message=AsyncMock(side_effect=RuntimeError("telegram down")),
            disconnect=AsyncMock(),
        )
        worker.is_running = True
        event = _event(sender_id=101, message_id=702)
        await db.reserve_managed_followup(-5428680940, 702, worker.account_id)
        worker._generate_reply = AsyncMock(return_value="我也還醒著")

        await worker._reply_later(event, 0, managed_followup=True)

        assert db.followup_completed == []
        assert db.followup_released == [(-5428680940, 702, "202")]
        assert worker.stats["managed_sent"] == 0

    asyncio.run(main())


def test_human_after_proactive_is_owned_by_original_account():
    async def main():
        db = _ClaimDB()
        owners = {}
        recent = {-5428680940: (101, float("inf"))}
        workers = [
            _worker(
                uid,
                set(MANAGED),
                db=db,
                human_owners=owners,
                recent_proactive_owners=recent,
            )
            for uid in sorted(MANAGED)
        ]
        first = _event(sender_id=999, message_id=801, text="穿什麼衣服")

        decisions = [await worker._should_reply(first) for worker in workers]

        assert decisions == [True, False, False, False]
        assert owners[(-5428680940, 999)][0] == 101

        second = _event(sender_id=999, message_id=802, text="看看")
        decisions = [await worker._should_reply(second) for worker in workers]
        assert decisions == [True, False, False, False]

    asyncio.run(main())


def test_meaningful_ordinary_human_message_has_one_responder_even_if_base_zero():
    async def main():
        db = _ClaimDB()
        owners = {}
        event = _event(sender_id=999, message_id=901, text="有人在台北嗎")
        workers = [
            _worker(
                uid,
                set(MANAGED),
                db=db,
                human_owners=owners,
                base_reply_probability=0.0,
            )
            for uid in sorted(MANAGED)
        ]

        decisions = [await worker._should_reply(event) for worker in workers]

        assert decisions.count(True) == 1
        owner_id, _expires_at = owners[(-5428680940, 999)]
        assert owner_id in MANAGED

    asyncio.run(main())


def test_recent_human_activity_suppresses_proactive_message():
    activity = {-5428680940: float("inf")}
    worker = _worker(101, last_human_activity=activity)
    assert worker._should_suppress_proactive(-5428680940) is True
