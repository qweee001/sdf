import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config import load_settings
from app.crypto import SecretBox
from app.manager import AccountManager
from app.database import Database
from app.live_test import LiveTestOutboundGate
from app.media import MediaAsset
from app.worker import AccountWorker


ACCOUNT_IDS = [
    "2ce525dfb0d4",
    "faa9a202f96e",
    "038632e4395b",
    "e63e27a4340d",
]
GROUP_ID = -5428680940
PERSONA_AGES = dict(zip(ACCOUNT_IDS, (21, 25, 29, 34), strict=True))


def _fixed_schedule(events):
    schedule = list(events)
    for index in range(len(schedule), 30):
        schedule.append(
            {
                "event_id": f"filler-{index}",
                "account_id": ACCOUNT_IDS[index % 4],
                "kind": "text",
            }
        )
    assert len(schedule) == 30
    return schedule


async def _create_fixed_run(db, **kwargs):
    requested_cap = kwargs["event_cap"]
    kwargs["duration_seconds"] = 3_600
    kwargs["event_cap"] = 40
    kwargs["schedule"] = _fixed_schedule(kwargs["schedule"])
    created = await Database.create_live_test_run(db, **kwargs)
    if created and requested_cap != 40:
        await db._c.execute(
            "UPDATE live_test_runs SET event_cap = ? WHERE id = ?",
            (requested_cap, kwargs["run_id"]),
        )
        await db._c.commit()
    return created


class FakeTelegramClient:
    def __init__(self):
        self.messages = []
        self.files = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_file(self, chat_id, file_obj, **kwargs):
        self.files.append((chat_id, file_obj.read(), kwargs))


class NoopStatus:
    async def __call__(self, *args, **kwargs):
        return None


async def _worker(db, gate, account_id=ACCOUNT_IDS[0]):
    persona = {
        "name": account_id,
        "gender": "女",
        "age": PERSONA_AGES.get(account_id, 21),
    }
    if account_id in PERSONA_AGES:
        await db.create_account(
            account_id,
            account_id,
            "test-session-key",
            json.dumps(persona, ensure_ascii=False),
        )
    config = SimpleNamespace(
        min_typing_delay=0,
        max_typing_delay=0,
        media_enabled=True,
        voice_media_enabled=True,
    )
    worker = AccountWorker(
        account_id=account_id,
        session_key="never-decrypted-here",
        tg_api_id=1,
        tg_api_hash="hash",
        ai_client=None,
        db=db,
        config=config,
        managed_ids=set(),
        on_status_change=NoopStatus(),
        persona=persona,
        selected_groups=[GROUP_ID, GROUP_ID - 1],
        outbound_gate=gate,
    )
    worker.is_running = True
    worker.tg_client = FakeTelegramClient()
    return worker


async def _active_gate(db, cap=3):
    created = await _create_fixed_run(db,
        run_id="gate-run",
        account_ids=ACCOUNT_IDS,
        group_id=GROUP_ID,
        duration_seconds=3_600,
        event_cap=cap,
        schedule=[
            {
                "event_id": "scripted-1",
                "account_id": ACCOUNT_IDS[0],
                "kind": "text",
            },
            {
                "event_id": "stale-lockdown",
                "account_id": ACCOUNT_IDS[0],
                "kind": "text",
            },
        ],
        started_at=1_000,
    )
    assert created is True
    gate = LiveTestOutboundGate(db, clock=lambda: 1_001.0)
    gate.activate(
        run_id="gate-run", account_ids=ACCOUNT_IDS, group_id=GROUP_ID
    )
    return gate


class BlockingReservationDB:
    """Pause only the durable reservation await to expose stale permits."""

    def __init__(self, db):
        self.db = db
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def reserve_live_test_event(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        return await self.db.reserve_live_test_event(*args, **kwargs)

    async def finish_live_test_event(self, *args, **kwargs):
        return await self.db.finish_live_test_event(*args, **kwargs)


def test_final_rpc_gate_counts_scripted_and_ordinary_text_and_media(tmp_path):
    async def main():
        db = Database(str(tmp_path / "gate.db"))
        await db.connect()
        gate = await _active_gate(db, cap=3)
        worker = await _worker(db, gate)
        client = worker.tg_client

        assert await worker._send_message(
            GROUP_ID, "scripted", live_test_event_id="scripted-1"
        ) is True
        # The same scheduled ID can never reach Telegram twice.
        assert await worker._send_message(
            GROUP_ID, "duplicate", live_test_event_id="scripted-1"
        ) is False
        assert client.messages == [(GROUP_ID, "scripted")]

        # Ordinary human-reply text has no supplied ID; the gate allocates one.
        assert await worker._send_message(GROUP_ID, "organic reply") is True
        asset = MediaAsset("image", b"image", "image.jpg", "image/jpeg")
        # Ordinary media also consumes a slot at the final send_file boundary.
        assert await worker._send_media(GROUP_ID, asset) is True

        # Cap is exhausted: neither final Telegram RPC may run again.
        assert await worker._send_message(GROUP_ID, "overflow") is False
        assert await worker._send_media(GROUP_ID, asset) is False
        assert client.messages == [
            (GROUP_ID, "scripted"),
            (GROUP_ID, "organic reply"),
        ]
        assert len(client.files) == 1

        status = await db.get_live_test_run("gate-run")
        assert status["reserved"] == 3
        assert status["sent"] == 3
        assert status["pending"] == 0
        await db.close()

    asyncio.run(main())


def test_media_permit_requires_and_persists_exact_generation_evidence(tmp_path):
    async def main():
        db = Database(str(tmp_path / "gate-media-evidence.db"))
        await db.connect()
        assert await _create_fixed_run(db,
            run_id="gate-media-evidence",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=1,
            schedule=[
                {
                    "event_id": "bound-voice",
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "voice",
                }
            ],
            started_at=1_000.0,
        )
        gate = LiveTestOutboundGate(db, clock=lambda: 1_001.0)
        gate.activate(
            run_id="gate-media-evidence",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
        )
        missing = await gate.reserve(
            account_id=ACCOUNT_IDS[0],
            group_id=GROUP_ID,
            kind="voice",
            event_id="bound-voice",
        )
        assert missing.allowed is False
        request_id = "1" * 32
        snapshot_sha256 = "2" * 64
        output_sha256 = "3" * 64
        content_sha256 = "4" * 64
        trigger_received_at = 999.0
        snapshot_at = 1_000.0
        permit = await gate.reserve(
            account_id=ACCOUNT_IDS[0],
            group_id=GROUP_ID,
            kind="voice",
            event_id="bound-voice",
            request_id=request_id,
            snapshot_sha256=snapshot_sha256,
            output_sha256=output_sha256,
            trigger_received_at=trigger_received_at,
            snapshot_at=snapshot_at,
            profile_id="21",
            content_sha256=content_sha256,
        )
        assert permit.allowed is True
        assert permit.request_id == request_id
        assert permit.snapshot_sha256 == snapshot_sha256
        assert permit.output_sha256 == output_sha256
        assert permit.kind == "voice"
        assert permit.trigger_received_at == trigger_received_at
        assert permit.snapshot_at == snapshot_at
        assert permit.profile_id == "21"
        assert permit.content_sha256 == content_sha256
        assert permit.decode_metadata_sha256 == ""
        assert await gate.mark_rpc_started(permit) is True
        assert await gate.complete(permit, sent=True) is True
        event = await db.get_live_test_event(
            "gate-media-evidence", "bound-voice"
        )
        assert event is not None
        assert event["state"] == "sent"
        assert event["request_id"] == request_id
        assert event["snapshot_sha256"] == snapshot_sha256
        assert event["output_sha256"] == output_sha256
        assert event["trigger_received_at"] == trigger_received_at
        assert event["snapshot_at"] == snapshot_at
        assert event["profile_id"] == "21"
        assert event["content_sha256"] == content_sha256
        assert event["decode_metadata_sha256"] == ""
        await db.close()

    asyncio.run(main())


def test_final_rpc_gate_denies_wrong_account_and_group_without_rpc(tmp_path):
    async def main():
        db = Database(str(tmp_path / "wrong-scope.db"))
        await db.connect()
        gate = await _active_gate(db, cap=40)
        right_worker = await _worker(db, gate)
        wrong_worker = await _worker(db, gate, account_id="not-in-run")
        asset = MediaAsset("video", b"video", "v.mp4", "video/mp4")

        assert await right_worker._send_message(GROUP_ID - 1, "wrong group") is False
        assert await right_worker._send_media(GROUP_ID - 1, asset) is False
        assert await wrong_worker._send_message(GROUP_ID, "wrong account") is False
        assert await wrong_worker._send_media(GROUP_ID, asset) is False
        assert right_worker.tg_client.messages == []
        assert right_worker.tg_client.files == []
        assert wrong_worker.tg_client.messages == []
        assert wrong_worker.tg_client.files == []
        status = await db.get_live_test_run("gate-run")
        assert status["reserved"] == 0
        await db.close()

    asyncio.run(main())


def test_scripted_rpc_rechecks_human_pause_at_final_boundary(tmp_path):
    async def main():
        db = Database(str(tmp_path / "human-pause.db"))
        await db.connect()
        now = [1_001.0]
        human_activity = {GROUP_ID: 1_000.0}
        created = await _create_fixed_run(db,
            run_id="human-pause-run",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=40,
            schedule=[
                {
                    "event_id": "scripted-race",
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "text",
                }
            ],
            started_at=1_000,
        )
        assert created is True
        gate = LiveTestOutboundGate(
            db,
            clock=lambda: now[0],
            last_human_activity=human_activity,
            human_pause_seconds=180,
        )
        gate.activate(
            run_id="human-pause-run",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
        )
        worker = await _worker(db, gate)

        assert await worker._send_message(
            GROUP_ID,
            "generated before the human spoke",
            live_test_event_id="scripted-race",
        ) is False
        assert worker.tg_client.messages == []
        status = await db.get_live_test_run("human-pause-run")
        assert status["reserved"] == 0

        now[0] = 1_180.0
        assert await worker._send_message(
            GROUP_ID,
            "fresh after pause",
            live_test_event_id="scripted-race",
        ) is True
        assert worker.tg_client.messages == [(GROUP_ID, "fresh after pause")]
        await db.close()

    asyncio.run(main())


def test_outbound_gate_is_noop_outside_active_live_run(tmp_path):
    async def main():
        db = Database(str(tmp_path / "inactive.db"))
        await db.connect()
        gate = LiveTestOutboundGate(db)
        worker = await _worker(db, gate)
        asset = MediaAsset("image", b"image", "image.jpg", "image/jpeg")

        assert await worker._send_message(GROUP_ID, "normal") is True
        assert await worker._send_media(GROUP_ID, asset) is True
        assert await worker._send_message(12345, "private") is False
        assert worker.tg_client.messages == [(GROUP_ID, "normal")]
        assert len(worker.tg_client.files) == 1
        assert await db.get_live_test_run() is None
        await db.close()

    asyncio.run(main())


def test_outstanding_reservation_is_revoked_by_lockdown_and_worker_stop(tmp_path):
    async def main():
        db = Database(str(tmp_path / "stale-lockdown.db"))
        await db.connect()
        blocking_db = BlockingReservationDB(db)
        gate = await _active_gate(db, cap=1)
        gate.db = blocking_db
        worker = await _worker(db, gate)
        client = worker.tg_client

        send = asyncio.create_task(
            worker._send_message(
                GROUP_ID,
                "must not escape",
                live_test_event_id="stale-lockdown",
            )
        )
        await blocking_db.started.wait()
        assert gate.lockdown("gate-run") is True
        worker.is_running = False
        blocking_db.release.set()

        assert await send is False
        assert client.messages == []
        status = await db.get_live_test_run("gate-run")
        assert status["reserved"] == 1
        assert status["released"] == 1
        assert status["sent"] == 0
        await db.close()

    asyncio.run(main())


def test_outstanding_scripted_reservation_rechecks_new_human_activity(tmp_path):
    async def main():
        db = Database(str(tmp_path / "stale-human.db"))
        await db.connect()
        blocking_db = BlockingReservationDB(db)
        now = [1_001.0]
        human_activity = {}
        assert await _create_fixed_run(db,
            run_id="human-race",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=1,
            schedule=[
                {
                    "event_id": "stale-human",
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "text",
                }
            ],
            started_at=1_000.0,
        )
        gate = LiveTestOutboundGate(
            blocking_db,
            clock=lambda: now[0],
            last_human_activity=human_activity,
        )
        gate.activate(
            run_id="human-race", account_ids=ACCOUNT_IDS, group_id=GROUP_ID
        )
        worker = await _worker(db, gate)

        send = asyncio.create_task(
            worker._send_message(
                GROUP_ID,
                "stale scripted text",
                live_test_event_id="stale-human",
            )
        )
        await blocking_db.started.wait()
        human_activity[GROUP_ID] = now[0]
        blocking_db.release.set()

        assert await send is False
        assert worker.tg_client.messages == []
        status = await db.get_live_test_run("human-race")
        assert status["reserved"] == 1
        assert status["released"] == 1
        await db.close()

    asyncio.run(main())


def test_final_rpc_boundary_rejects_permit_revoked_after_reserve_returns(tmp_path):
    class RevokingGate(LiveTestOutboundGate):
        async def reserve(self, **kwargs):
            permit = await super().reserve(**kwargs)
            self.lockdown("rpc-boundary")
            return permit

    async def main():
        db = Database(str(tmp_path / "rpc-boundary.db"))
        await db.connect()
        assert await _create_fixed_run(db,
            run_id="rpc-boundary",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=1,
            schedule=[
                {
                    "event_id": "boundary-media",
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "image",
                }
            ],
            started_at=1_000.0,
        )
        gate = RevokingGate(db, clock=lambda: 1_001.0)
        gate.activate(
            run_id="rpc-boundary", account_ids=ACCOUNT_IDS, group_id=GROUP_ID
        )
        worker = await _worker(db, gate)
        client = worker.tg_client

        assert await worker._send_media(
            GROUP_ID,
            MediaAsset("image", b"image", "x.jpg", "image/jpeg"),
            live_test_event_id="boundary-media",
        ) is False
        assert client.files == []
        status = await db.get_live_test_run("rpc-boundary")
        assert status["released"] == 1
        assert status["sent"] == 0
        await db.close()

    asyncio.run(main())


def test_outstanding_reservation_is_revoked_when_clock_crosses_expiry(tmp_path):
    async def main():
        db = Database(str(tmp_path / "stale-expiry.db"))
        await db.connect()
        blocking_db = BlockingReservationDB(db)
        now = [1_001.0]
        assert await _create_fixed_run(db,
            run_id="expiry-race",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=1,
            schedule=[
                {
                    "event_id": "stale-expiry",
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "text",
                }
            ],
            started_at=1_000.0,
        )
        gate = LiveTestOutboundGate(blocking_db, clock=lambda: now[0])
        gate.activate(
            run_id="expiry-race",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            expires_at=4_600.0,
        )
        worker = await _worker(db, gate)

        send = asyncio.create_task(
            worker._send_message(
                GROUP_ID,
                "late text",
                live_test_event_id="stale-expiry",
            )
        )
        await blocking_db.started.wait()
        now[0] = 4_601.0
        blocking_db.release.set()

        assert await send is False
        assert worker.tg_client.messages == []
        status = await db.get_live_test_run("expiry-race")
        assert status["status"] == "needs_reconciliation"
        assert status["reserved"] == 0
        await db.close()

    asyncio.run(main())


def test_final_rpc_boundary_rejects_permit_from_prior_gate_generation(tmp_path):
    class ReactivatingGate(LiveTestOutboundGate):
        async def reserve(self, **kwargs):
            permit = await super().reserve(**kwargs)
            self.deactivate("generation-race")
            self.activate(
                run_id="generation-race",
                account_ids=ACCOUNT_IDS,
                group_id=GROUP_ID,
                expires_at=4_600.0,
            )
            return permit

    async def main():
        db = Database(str(tmp_path / "generation-race.db"))
        await db.connect()
        assert await _create_fixed_run(db,
            run_id="generation-race",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=1,
            schedule=[
                {
                    "event_id": "old-generation",
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "text",
                }
            ],
            started_at=1_000.0,
        )
        gate = ReactivatingGate(db, clock=lambda: 1_001.0)
        gate.activate(
            run_id="generation-race",
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            expires_at=4_600.0,
        )
        worker = await _worker(db, gate)

        assert await worker._send_message(
            GROUP_ID,
            "old generation",
            live_test_event_id="old-generation",
        ) is False
        assert worker.tg_client.messages == []
        status = await db.get_live_test_run("generation-race")
        assert status["released"] == 1
        await db.close()

    asyncio.run(main())


def test_rpc_unknown_failure_consumes_hard_cap_and_is_audited(tmp_path):
    class UnknownFailureClient(FakeTelegramClient):
        async def send_message(self, chat_id, text):
            raise RuntimeError("Telegram result unknown")

    async def main():
        db = Database(str(tmp_path / "unknown-rpc.db"))
        await db.connect()
        gate = await _active_gate(db, cap=1)
        worker = await _worker(db, gate)
        worker.tg_client = UnknownFailureClient()

        with pytest.raises(RuntimeError, match="result unknown"):
            await worker._send_message(GROUP_ID, "unknown")
        status = await db.get_live_test_run("gate-run")
        assert status["failed"] == 0
        assert status["hard_attempt"] == 1
        assert status["cap_used"] == 1

        worker.tg_client = FakeTelegramClient()
        assert await worker._send_message(GROUP_ID, "replacement blocked") is False
        assert worker.tg_client.messages == []
        assert (await db.get_live_test_run("gate-run"))["reserved"] == 1
        await db.close()

    asyncio.run(main())


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("run_id", "wrong-run"),
        ("event_id", "wrong-event"),
        ("account_id", ACCOUNT_IDS[1]),
        ("group_id", GROUP_ID - 1),
        ("kind", "video"),
        ("trigger_received_at", 999.0),
        ("snapshot_at", 1_002.0),
        ("profile_id", "25"),
        ("content_sha256", "c" * 64),
        ("decode_metadata_sha256", "d" * 64),
        ("request_id", "c" * 32),
        ("snapshot_sha256", "c" * 64),
        ("output_sha256", "c" * 64),
    ],
)
def test_trusted_bound_identity_releases_real_reservation_for_every_permit_mismatch(
    tmp_path, field, wrong_value
):
    async def main():
        db = Database(str(tmp_path / f"bound-release-{field}.db"))
        await db.connect()
        run_id = "bound-release"
        event_id = "bound-voice"
        assert await _create_fixed_run(
            db,
            run_id=run_id,
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            duration_seconds=3_600,
            event_cap=40,
            schedule=[
                {
                    "event_id": event_id,
                    "account_id": ACCOUNT_IDS[0],
                    "kind": "voice",
                }
            ],
            started_at=1_000.0,
        )
        gate = LiveTestOutboundGate(db, clock=lambda: 1_001.0)
        gate.activate(
            run_id=run_id,
            account_ids=ACCOUNT_IDS,
            group_id=GROUP_ID,
            expires_at=4_600.0,
        )
        trusted = {
            "run_id": run_id,
            "event_id": event_id,
            "account_id": ACCOUNT_IDS[0],
            "group_id": GROUP_ID,
            "kind": "voice",
            "trigger_received_at": 1_000.0,
            "snapshot_at": 1_000.5,
            "profile_id": "21",
            "content_sha256": "a" * 64,
            "decode_metadata_sha256": "",
            "request_id": "b" * 32,
            "snapshot_sha256": "b" * 64,
            "output_sha256": "e" * 64,
        }
        permit = await gate.reserve(
            account_id=trusted["account_id"],
            group_id=trusted["group_id"],
            kind=trusted["kind"],
            event_id=trusted["event_id"],
            trigger_received_at=trusted["trigger_received_at"],
            snapshot_at=trusted["snapshot_at"],
            profile_id=trusted["profile_id"],
            content_sha256=trusted["content_sha256"],
            decode_metadata_sha256=trusted["decode_metadata_sha256"],
            request_id=trusted["request_id"],
            snapshot_sha256=trusted["snapshot_sha256"],
            output_sha256=trusted["output_sha256"],
        )
        assert permit.allowed
        tampered = replace(permit, **{field: wrong_value})
        assert tampered != permit

        rpc_calls = 0
        assert await gate.release_bound(
            **trusted,
            detail=f"permit {field} mismatch before RPC",
        )
        event = await db.get_live_test_event(run_id, event_id)
        status = await db.get_live_test_run(run_id)
        assert rpc_calls == 0
        assert event is not None and event["state"] == "released"
        assert status is not None
        assert status["released"] == 1
        assert status["pending"] == 0
        await db.close()

    asyncio.run(main())
