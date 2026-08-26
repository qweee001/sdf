import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telethon.tl.types import MessageMediaPhoto, PhotoEmpty

from app.config import load_settings
from app.media import MediaAsset
from app.worker import AccountWorker


ACCOUNT_IDS: tuple[int, ...] = (101, 202, 303, 404, 505, 606, 707, 808)
MANAGED: set[int] = set(ACCOUNT_IDS[:4])


class _ClaimDB:
    def __init__(self):
        self.claims = {}
        self.text_claims = set()
        self.text_claim_lock = asyncio.Lock()
        self.followup_claims = set()
        self.followup_pending = {}
        self.followup_completed = []
        self.followup_released = []
        self.messages = []
        self.activities = []
        self.reply_events = []
        self.pressure = {
            "human_5m": 0,
            "human_sent_5m": 0,
            "sent_20s": 0,
            "sent_10m": 0,
        }

    async def get_recent_messages(self, *_args, **_kwargs):
        return []

    async def get_recent_group_replies(self, *_args, **_kwargs):
        return []

    async def claim_message_response(
        self, group_id: int, message_id: int, account_id: str
    ) -> bool:
        key = (group_id, message_id)
        if key in self.claims:
            return False
        self.claims[key] = account_id
        return True

    async def release_message_response_claim(
        self, group_id: int, message_id: int, account_id: str
    ) -> bool:
        key = (group_id, message_id)
        if self.claims.get(key) != account_id:
            return False
        self.claims.pop(key)
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

    async def interaction_pressure(self, *_args, **_kwargs):
        return dict(self.pressure)

    async def admit_ordinary_reply(self, group_id, message_id, account_id):
        if (
            int(self.pressure.get("ordinary_claimed_10m", 0) or 0) >= 8
            or (
                int(self.pressure.get("human_5m", 0) or 0) >= 14
                and (
                    int(self.pressure.get("human_sent_5m", 0) or 0) >= 2
                    or int(self.pressure.get("ordinary_claimed_5m", 0) or 0) >= 2
                    or int(self.pressure.get("ordinary_claimed_20s", 0) or 0) >= 1
                )
            )
        ):
            return False
        self.reply_events.append(
            (
                (),
                {
                    "group_id": group_id,
                    "message_id": message_id,
                    "account_id": account_id,
                    "stage": "claimed",
                    "reason": "human",
                },
            )
        )
        return True

    async def record_reply_event(self, *args, **kwargs):
        self.reply_events.append((args, kwargs))


def _worker(
    tg_user_id: int,
    active_ids: set[int] | None = None,
    managed_ids: set[int] | None = None,
    db=None,
    managed_origins=None,
    human_owners=None,
    recent_proactive_owners=None,
    last_human_activity=None,
    base_reply_probability=1.0,
    media_service=None,
    active_group_ids=None,
    selected_groups=None,
    reply_claim_signals=None,
    failed_reply_claimants=None,
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
        managed_ids=set(MANAGED) if managed_ids is None else managed_ids,
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
        selected_groups=[-5428680940] if selected_groups is None else selected_groups,
        media_service=media_service,
        active_group_ids=active_group_ids,
        reply_claim_signals=reply_claim_signals,
        failed_reply_claimants=failed_reply_claimants,
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


def test_hostile_human_message_has_exactly_one_responder_without_pile_on():
    async def main():
        event = _event(text="這群爛死了，你們都是機器人吧")
        db = _ClaimDB()
        decisions = [
            await _worker(uid, db=db)._should_reply(event)
            for uid in sorted(MANAGED)
        ]
        assert decisions.count(True) == 1

    asyncio.run(main())


@pytest.mark.parametrize("account_count", [1, 4, 5, 8])
def test_human_image_has_exactly_one_deterministic_responder(account_count):
    async def main():
        active_ids = set(ACCOUNT_IDS[:account_count])
        owner_id = ACCOUNT_IDS[account_count - 1]
        owners = {(-5428680940, 999): (owner_id, float("inf"))}
        event = _event(text="", message_id=780 + account_count)
        event.media = MessageMediaPhoto(photo=PhotoEmpty(id=account_count))
        db = _ClaimDB()
        workers = [
            _worker(
                uid,
                active_ids,
                managed_ids=active_ids,
                db=db,
                human_owners=owners,
            )
            for uid in ACCOUNT_IDS[:account_count]
        ]

        decisions = await asyncio.gather(
            *(worker._should_reply(event) for worker in workers)
        )

        assert decisions.count(True) == 1
        assert decisions[account_count - 1] is True

    asyncio.run(main())


@pytest.mark.parametrize(
    "failure_mode",
    ["empty", "exception", "download_empty", "download_exception"],
)
def test_failed_image_claimant_is_replaced_once(failure_mode):
    async def main():
        db = _ClaimDB()
        active = {101, 202, 303}
        owners = {(-5428680940, 999): (101, float("inf"))}
        signals = {}
        failed = {}
        first_attempt_started = asyncio.Event()
        allow_first_failure = asyncio.Event()
        replacement_vision_started = asyncio.Event()
        allow_replacement_success = asyncio.Event()
        vision_accounts = []
        download_count = 0

        async def download_media(*, file):
            nonlocal download_count
            assert file is bytes
            download_count += 1
            if download_count == 1 and failure_mode.startswith("download_"):
                first_attempt_started.set()
                await allow_first_failure.wait()
                if failure_mode == "download_exception":
                    raise RuntimeError("download unavailable")
                return b""
            return b"jpeg"

        async def understand_image(account_id, *_args):
            vision_accounts.append(account_id)
            if account_id == "101":
                first_attempt_started.set()
                await allow_first_failure.wait()
                if failure_mode == "exception":
                    raise RuntimeError("vision unavailable")
                return ""
            if account_id == "202":
                replacement_vision_started.set()
                await allow_replacement_success.wait()
                return "第二個帳號看懂了照片"
            raise AssertionError("third worker must not call vision")

        media = SimpleNamespace(
            understand_image=AsyncMock(side_effect=understand_image)
        )
        event = _event(text="", message_id=990)
        event.media = MessageMediaPhoto(photo=PhotoEmpty(id=990))
        event.file = SimpleNamespace(size=4, mime_type="image/jpeg")
        event.download_media = AsyncMock(side_effect=download_media)

        workers = [
            _worker(
                uid,
                active,
                managed_ids=active,
                db=db,
                human_owners=owners,
                media_service=media,
                reply_claim_signals=signals,
                failed_reply_claimants=failed,
            )
            for uid in sorted(active)
        ]
        clients = []
        for worker in workers:
            worker.config.media_enabled = True
            worker.config.media_max_input_bytes = 1024
            client = SimpleNamespace(send_message=AsyncMock())
            worker.tg_client = client
            worker.is_running = True
            clients.append(client)

        assert await workers[0]._should_reply(event) is True
        first_task = asyncio.create_task(workers[0]._reply_later(event, 0))
        await first_attempt_started.wait()

        async def wait_and_reply(worker):
            claimed = await worker._should_reply(event)
            if claimed:
                await worker._reply_later(event, 0)
            return claimed

        second_task = asyncio.create_task(wait_and_reply(workers[1]))
        await asyncio.sleep(0)
        third_task = asyncio.create_task(wait_and_reply(workers[2]))
        await asyncio.sleep(0)
        allow_first_failure.set()
        await replacement_vision_started.wait()

        claim_key = (-5428680940, 990)
        assert failed == {claim_key: {101}}
        assert db.claims[claim_key] == "202"
        assert await workers[0]._should_reply(event) is False
        allow_replacement_success.set()
        decisions = await asyncio.gather(second_task, third_task)
        await first_task

        assert decisions == [True, False]
        assert vision_accounts == (
            ["202"]
            if failure_mode.startswith("download_")
            else ["101", "202"]
        )
        assert media.understand_image.await_count == len(vision_accounts)
        assert event.download_media.await_count == 2
        assert sum(client.send_message.await_count for client in clients) == 1
        clients[0].send_message.assert_not_awaited()
        clients[1].send_message.assert_awaited_once_with(
            -5428680940, "第二個帳號看懂了照片"
        )
        clients[2].send_message.assert_not_awaited()
        assert signals == {}
        assert failed == {}

    asyncio.run(main())


@pytest.mark.parametrize("send_path", ["text", "media"])
@pytest.mark.parametrize("record_failure", ["add_message", "touch_activity"])
def test_post_send_record_failure_keeps_image_claim(send_path, record_failure):
    async def main():
        db = _ClaimDB()
        active = {101, 202}
        owners = {(-5428680940, 999): (101, float("inf"))}
        signals = {}
        failed = {}
        first_generation_started = asyncio.Event()
        allow_first_generation = asyncio.Event()
        vision_accounts = []
        media_accounts = []

        async def add_message(*args):
            if record_failure == "add_message" and args[0] == "101":
                raise RuntimeError("post-send add_message failed")
            db.messages.append(args)

        async def touch_activity(*args):
            if record_failure == "touch_activity" and args[0] == "101":
                raise RuntimeError("post-send touch_activity failed")
            db.activities.append(args)

        async def understand_image(account_id, *_args):
            vision_accounts.append(account_id)
            if account_id == "101":
                first_generation_started.set()
                await allow_first_generation.wait()
                return "第一個帳號看懂了照片"
            return "第二個帳號不應再次看圖"

        async def generate_image(account_id, *_args):
            media_accounts.append(account_id)
            if account_id == "101":
                first_generation_started.set()
                await allow_first_generation.wait()
            return MediaAsset("image", b"png", "reply.png", "image/png")

        db.add_message = AsyncMock(side_effect=add_message)
        db.touch_activity = AsyncMock(side_effect=touch_activity)
        media = SimpleNamespace(
            understand_image=AsyncMock(side_effect=understand_image),
            generate_image=AsyncMock(side_effect=generate_image),
        )
        event = _event(
            text="傳張自拍看看" if send_path == "media" else "",
            message_id=991,
        )
        event.media = MessageMediaPhoto(photo=PhotoEmpty(id=991))
        event.file = SimpleNamespace(size=4, mime_type="image/jpeg")
        event.download_media = AsyncMock(return_value=b"jpeg")

        workers = [
            _worker(
                uid,
                active,
                managed_ids=active,
                db=db,
                human_owners=owners,
                media_service=media,
                reply_claim_signals=signals,
                failed_reply_claimants=failed,
            )
            for uid in sorted(active)
        ]
        clients = []
        for worker in workers:
            worker.config.media_enabled = True
            worker.config.media_max_input_bytes = 1024
            client = SimpleNamespace(
                send_message=AsyncMock(),
                send_file=AsyncMock(),
            )
            worker.tg_client = client
            worker.is_running = True
            clients.append(client)

        assert await workers[0]._should_reply(event) is True
        first_task = asyncio.create_task(workers[0]._reply_later(event, 0))
        await first_generation_started.wait()

        async def wait_and_reply():
            claimed = await workers[1]._should_reply(event)
            if claimed:
                await workers[1]._reply_later(event, 0)
            return claimed

        second_task = asyncio.create_task(wait_and_reply())
        await asyncio.sleep(0)
        allow_first_generation.set()
        assert await second_task is False
        await first_task

        claim_key = (-5428680940, 991)
        assert db.claims == {claim_key: "101"}
        assert vision_accounts == (["101"] if send_path == "text" else [])
        assert media_accounts == (["101"] if send_path == "media" else [])
        assert sum(client.send_message.await_count for client in clients) == (
            1 if send_path == "text" else 0
        )
        assert sum(client.send_file.await_count for client in clients) == (
            1 if send_path == "media" else 0
        )
        clients[1].send_message.assert_not_awaited()
        clients[1].send_file.assert_not_awaited()
        assert signals == {}
        assert failed == {}
        audit = [kwargs for _args, kwargs in db.reply_events]
        persistence = [
            item for item in audit if item.get("stage") == "persistence"
        ]
        assert persistence[-1]["reason"] == "RuntimeError"
        assert any(item.get("stage") == "sent" for item in audit)

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


def test_managed_followup_winner_is_limited_to_group_eligible_accounts():
    async def main():
        db = _ClaimDB()
        active = {101, 202, 303}
        eligible = {-5428680940: {101, 202}}
        origins = {
            (-5428680940, 101, "今晚有人想聊天嗎"): float("inf")
        }
        event = _event(sender_id=101, message_id=503)
        workers = [
            _worker(
                uid,
                active,
                db=db,
                managed_origins=origins,
                active_group_ids=eligible,
            )
            for uid in sorted(active)
        ]

        decisions = [await worker._should_reply(event) for worker in workers]

        assert decisions == [False, True, False]

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


def test_atomic_claim_never_duplicates_with_different_active_views():
    async def main():
        event = _event(message_id=91)
        db = _ClaimDB()
        first = _worker(101, {101, 303}, db=db)
        second = _worker(303, {101, 202, 303}, db=db)
        decisions = [
            await first._should_reply(event),
            await second._should_reply(event),
        ]
        assert decisions.count(True) <= 1

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


def test_managed_empty_generation_releases_without_send_or_cooldown():
    async def main():
        db = _ClaimDB()
        worker = _worker(202, {101, 202}, db=db)
        client = SimpleNamespace(
            send_message=AsyncMock(), disconnect=AsyncMock()
        )
        worker.tg_client = client
        worker.is_running = True
        event = _event(sender_id=101, message_id=701, text="今天穿很好看的衣服")
        await db.reserve_managed_followup(-5428680940, 701, worker.account_id)
        worker._call_ai = AsyncMock(side_effect=["", ""])

        await worker._reply_later(event, 0, managed_followup=True)

        client.send_message.assert_not_awaited()
        assert db.followup_completed == []
        assert db.followup_released == [(-5428680940, 701, "202")]
        assert worker.stats["managed_sent"] == 0
        assert worker.stats["reply_drops"]["ai_empty"] == 1

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


def test_base_zero_suppresses_ordinary_human_message():
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

        assert decisions == [False, False, False, False]
        assert (-5428680940, 999) not in owners

    asyncio.run(main())


def test_base_zero_also_suppresses_sticky_owner():
    async def main():
        db = _ClaimDB()
        owners = {(-5428680940, 999): (101, float("inf"))}
        event = _event(sender_id=999, message_id=903, text="繼續聊剛才的話題")
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

        assert decisions == [False, False, False, False]

    asyncio.run(main())


def test_high_human_traffic_suppresses_sticky_owner():
    async def main():
        db = _ClaimDB()
        db.pressure.update({"human_5m": 20, "human_sent_5m": 2})
        owners = {(-5428680940, 998): (101, float("inf"))}
        event = _event(sender_id=998, message_id=902, text="今天台北雨超大")
        workers = [
            _worker(
                uid,
                set(MANAGED),
                db=db,
                human_owners=owners,
                base_reply_probability=1.0,
            )
            for uid in sorted(MANAGED)
        ]

        decisions = [await worker._should_reply(event) for worker in workers]

        assert decisions == [False, False, False, False]

    asyncio.run(main())


def test_group_ineligible_account_is_never_selected_as_winner():
    async def main():
        db = _ClaimDB()
        active = {101, 202}
        eligible = {-5428680940: {202}}
        event = _event(sender_id=997, message_id=904, text="台中今晚有人嗎")
        workers = [
            _worker(
                uid,
                active,
                db=db,
                active_group_ids=eligible,
                base_reply_probability=1.0,
            )
            for uid in sorted(active)
        ]

        decisions = [await worker._should_reply(event) for worker in workers]

        assert decisions == [False, True]

    asyncio.run(main())


def test_group_membership_map_tracks_group_changes_and_stop():
    active = {101}
    memberships = {}
    worker = _worker(
        101,
        active,
        active_group_ids=memberships,
        selected_groups=[-1],
    )
    worker.is_running = True

    worker.update_selected_groups({-1, -2})
    assert memberships == {-1: {101}, -2: {101}}

    worker.update_selected_groups({-2})
    assert memberships == {-2: {101}}

    worker._sync_active_group_memberships(False)
    assert memberships == {}


def test_new_human_ownership_is_distributed_across_active_accounts():
    async def main():
        db = _ClaimDB()
        owners = {}
        counts = {uid: 0 for uid in MANAGED}
        workers = [
            _worker(
                uid,
                set(MANAGED),
                db=db,
                human_owners=owners,
                base_reply_probability=1.0,
            )
            for uid in sorted(MANAGED)
        ]

        for index in range(240):
            event = _event(
                sender_id=10_000 + index,
                message_id=20_000 + index,
                text=f"第{index}個真人話題",
            )
            decisions = [await worker._should_reply(event) for worker in workers]
            assert decisions.count(True) == 1
            winner_id = workers[decisions.index(True)].tg_user_id
            assert winner_id is not None
            counts[winner_id] += 1

        assert all(count > 0 for count in counts.values())
        assert max(counts.values()) < 90

    asyncio.run(main())


def test_recent_human_activity_suppresses_proactive_message():
    activity = {-5428680940: float("inf")}
    worker = _worker(101, last_human_activity=activity)
    assert worker._should_suppress_proactive(-5428680940) is True


def test_proactive_topic_does_not_repeat_normalized_text_within_account_day(monkeypatch):
    worker = _worker(101)
    day = 321
    monkeypatch.setattr(worker, "_today_index", lambda: day)
    monkeypatch.setattr(
        "app.worker.generate_proactive_topic",
        Mock(side_effect=[
            "週末想唱歌",
            "週末想唱歌！",
            "今天想吃牛肉麵",
            "週末想唱歌",
        ]),
    )

    assert worker._next_proactive_topic() == "週末想唱歌"
    assert worker._next_proactive_topic() == "今天想吃牛肉麵"

    day = 322
    assert worker._next_proactive_topic() == "週末想唱歌"


def test_live_proactive_loop_dedupes_rolls_day_and_stops_cleanly(monkeypatch):
    async def main():
        worker = _worker(101)
        selected_group = -5428680940
        unselected_group = -999999
        clock = [321 * 86400.0]
        sleep_calls = 0
        loop_is_blocked = asyncio.Event()
        never_resume = asyncio.Event()
        third_send = asyncio.Event()
        sends = []

        worker.is_running = True
        worker._last_activity = {
            selected_group: clock[0],
            unselected_group: clock[0],
        }
        worker._known_groups = {selected_group, unselected_group}
        worker.config.proactive_enabled = True
        worker.config.proactive_loop_min_seconds = 1.0
        worker.config.proactive_loop_max_seconds = 1.0
        worker.config.proactive_max_per_day = 10
        worker.config.proactive_min_interval_minutes = 1
        worker.db.claim_proactive_slot = AsyncMock(return_value=True)
        worker._is_sleeping = Mock(return_value=False)
        worker._is_busy_hour = Mock(return_value=False)
        worker._should_suppress_proactive = Mock(return_value=False)

        async def controlled_sleep(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                clock[0] = 321 * 86400.0 + 60
                return
            if sleep_calls == 2:
                clock[0] = 321 * 86400.0 + 180
                return
            if sleep_calls == 3:
                clock[0] = 322 * 86400.0 + 60
                return
            loop_is_blocked.set()
            await never_resume.wait()

        async def record_send(group_id, text, **kwargs):
            sends.append((group_id, text, kwargs))
            if len(sends) == 3:
                third_send.set()
            return True

        def select_only_group(groups):
            assert groups == [selected_group]
            return groups[0]

        monkeypatch.setattr("app.worker.asyncio.sleep", controlled_sleep)
        monkeypatch.setattr("app.worker.time.time", lambda: clock[0])
        monkeypatch.setattr(worker, "_today_index", lambda: int(clock[0] // 86400))
        monkeypatch.setattr("app.worker.random.choice", Mock(side_effect=select_only_group))
        monkeypatch.setattr(
            "app.worker.generate_proactive_topic",
            Mock(side_effect=[
                "週末想唱歌",
                "週末想唱歌！",
                "今天想吃牛肉麵",
                "週末想唱歌",
            ]),
        )
        monkeypatch.setattr(worker, "_send_text_recorded", record_send)

        worker._proactive_task = asyncio.create_task(worker._proactive_loop())
        live_task = worker._proactive_task
        await third_send.wait()
        await loop_is_blocked.wait()

        assert [(group_id, text) for group_id, text, _kwargs in sends] == [
            (selected_group, "週末想唱歌"),
            (selected_group, "今天想吃牛肉麵"),
            (selected_group, "週末想唱歌"),
        ]
        assert worker._normalized_reply(sends[0][1]) != worker._normalized_reply(
            sends[1][1]
        )
        assert worker._normalized_reply(sends[0][1]) == worker._normalized_reply(
            sends[2][1]
        )

        await worker.stop()

        assert worker._proactive_task is None
        assert live_task.done()
        assert not worker.is_running

    asyncio.run(main())
