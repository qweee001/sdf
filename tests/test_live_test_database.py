import asyncio

import pytest

from app.database import Database


FIXED_ACCOUNT_IDS = [
    "2ce525dfb0d4",
    "faa9a202f96e",
    "038632e4395b",
    "e63e27a4340d",
]
FIXED_GROUP_ID = -5428680940


def _fixed_schedule(*events):
    schedule = list(events)
    for index in range(len(schedule), 30):
        schedule.append(
            {
                "event_id": f"filler-{index}",
                "account_id": FIXED_ACCOUNT_IDS[index % 4],
                "kind": "text",
            }
        )
    assert len(schedule) == 30
    return schedule


async def _create_run(
    db,
    *,
    run_id,
    schedule,
    cap=40,
    group_id=FIXED_GROUP_ID,
    started_at=1_000.0,
    duration=3_600,
):
    schedule[:] = _fixed_schedule(*schedule)
    created = await db.create_live_test_run(
        run_id=run_id,
        account_ids=list(FIXED_ACCOUNT_IDS),
        group_id=group_id,
        duration_seconds=duration,
        event_cap=40,
        schedule=schedule,
        started_at=started_at,
    )
    if created and cap != 40:
        await db._c.execute(
            "UPDATE live_test_runs SET event_cap = ? WHERE id = ?", (cap, run_id)
        )
        await db._c.commit()
    return created


def test_database_create_enforces_fixed_live_test_envelope(tmp_path):
    async def main():
        valid = {
            "account_ids": list(FIXED_ACCOUNT_IDS),
            "group_id": FIXED_GROUP_ID,
            "duration_seconds": 3_600,
            "event_cap": 40,
            "schedule": _fixed_schedule(),
            "started_at": 1_000.0,
        }
        invalid_overrides = [
            {"duration_seconds": 3_599},
            {"event_cap": 39},
            {"group_id": FIXED_GROUP_ID - 1},
            {"account_ids": FIXED_ACCOUNT_IDS[:-1]},
            {"account_ids": [*FIXED_ACCOUNT_IDS[:-1], "wrong-account"]},
            {"schedule": _fixed_schedule()[:-1]},
        ]
        for index, override in enumerate(invalid_overrides):
            db = Database(str(tmp_path / f"invalid-envelope-{index}.db"))
            await db.connect()
            with pytest.raises(ValueError, match="fixed live-test envelope"):
                await db.create_live_test_run(
                    run_id=f"invalid-{index}", **(valid | override)
                )
            assert await db.get_live_test_run() is None
            await db.close()

    asyncio.run(main())


def test_live_test_reservations_are_persistent_idempotent_and_bounded(tmp_path):
    async def main():
        path = str(tmp_path / "live-test.db")
        db = Database(path)
        await db.connect()
        schedule = [
            {"event_id": "e1", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"},
            {"event_id": "e2", "account_id": FIXED_ACCOUNT_IDS[1], "kind": "image"},
            {"event_id": "e3", "account_id": FIXED_ACCOUNT_IDS[2], "kind": "video"},
        ]
        assert await _create_run(
            db, run_id="run-1", schedule=schedule, cap=2
        )

        assert await db.reserve_live_test_event(
            "run-1", "e1", FIXED_ACCOUNT_IDS[0], "text", group_id=FIXED_GROUP_ID,
            scripted=True, now=1_001.0,
        )
        assert not await db.reserve_live_test_event(
            "run-1", "e1", FIXED_ACCOUNT_IDS[0], "text", group_id=FIXED_GROUP_ID,
            scripted=True, now=1_002.0,
        )
        assert await db.reserve_live_test_event(
            "run-1", "e2", FIXED_ACCOUNT_IDS[1], "image", group_id=FIXED_GROUP_ID,
            scripted=True, now=1_003.0,
        )
        assert not await db.reserve_live_test_event(
            "run-1", "e3", FIXED_ACCOUNT_IDS[2], "video", group_id=FIXED_GROUP_ID,
            scripted=True, now=1_004.0,
        )

        assert await db.mark_live_test_event_rpc_started(
            "run-1",
            "e1",
            account_id=FIXED_ACCOUNT_IDS[0],
            group_id=FIXED_GROUP_ID,
            kind="text",
        )
        assert await db.finish_live_test_event(
            "run-1", "e1", "sent", kind="text"
        )
        assert await db.finish_live_test_event(
            "run-1", "e2", "failed", "telegram", kind="image"
        )
        status = await db.get_live_test_run("run-1")
        assert status["reserved"] == 2
        assert status["sent"] == 1
        assert status["failed"] == 1
        assert status["cap_used"] == 2
        await db.close()

        reopened = Database(path)
        await reopened.connect()
        persisted = await reopened.get_live_test_run("run-1")
        assert persisted["account_ids"] == [FIXED_ACCOUNT_IDS[0], FIXED_ACCOUNT_IDS[1], FIXED_ACCOUNT_IDS[2], FIXED_ACCOUNT_IDS[3]]
        assert persisted["schedule"] == schedule
        assert persisted["reserved"] == 2
        await reopened.close()

    asyncio.run(main())


def test_live_test_cap_is_atomic_across_database_connections(tmp_path):
    async def main():
        path = str(tmp_path / "live-test-atomic.db")
        first = Database(path)
        second = Database(path)
        await first.connect()
        await second.connect()
        schedule = [
            {"event_id": "one", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"},
            {"event_id": "two", "account_id": FIXED_ACCOUNT_IDS[1], "kind": "text"},
        ]
        assert await _create_run(
            first, run_id="run-atomic", schedule=schedule,
            cap=1, started_at=2_000.0,
        )
        start = asyncio.Event()

        async def reserve(db, event_id, account_id):
            await start.wait()
            return await db.reserve_live_test_event(
                "run-atomic", event_id, account_id, "text",
                group_id=FIXED_GROUP_ID, scripted=True, now=2_001.0,
            )

        tasks = [
            asyncio.create_task(reserve(first, "one", FIXED_ACCOUNT_IDS[0])),
            asyncio.create_task(reserve(second, "two", FIXED_ACCOUNT_IDS[1])),
        ]
        start.set()
        results = await asyncio.gather(*tasks)
        assert results.count(True) == 1
        assert results.count(False) == 1
        status = await first.get_live_test_run("run-atomic")
        assert status["cap_used"] == 1
        await first.close()
        await second.close()

    asyncio.run(main())


def test_live_test_reservation_expires_run_before_dispatch(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-expired.db"))
        await db.connect()
        schedule = [{"event_id": "late", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"}]
        assert await _create_run(
            db, run_id="run-expired", schedule=schedule,
            started_at=3_000.0,
        )
        assert not await db.reserve_live_test_event(
            "run-expired", "late", FIXED_ACCOUNT_IDS[0], "text", group_id=FIXED_GROUP_ID,
            scripted=True, now=6_600.001,
        )
        status = await db.get_live_test_run("run-expired")
        assert status["status"] == "needs_reconciliation"
        assert status["stopped_at"] is None
        assert status["stop_reason"] == "expired"
        assert await db.has_live_test_reconciliation() is True
        assert status["reserved"] == 0
        await db.close()

    asyncio.run(main())


def test_live_test_reservation_reads_clock_after_begin_immediate(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-realtime-clock.db"))
        await db.connect()
        schedule = [{"event_id": "late", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"}]
        assert await _create_run(
            db, run_id="run-realtime-clock", schedule=schedule,
            started_at=4_000.0,
        )
        now = [7_601.0]
        assert not await db.reserve_live_test_event(
            "run-realtime-clock", "late", FIXED_ACCOUNT_IDS[0], "text",
            group_id=FIXED_GROUP_ID, scripted=True, clock=lambda: now[0],
        )
        status = await db.get_live_test_run("run-realtime-clock")
        assert status["status"] == "needs_reconciliation"
        assert status["stopped_at"] is None
        assert status["stop_reason"] == "expired"
        assert await db.has_live_test_reconciliation() is True
        assert status["reserved"] == 0
        await db.close()

    asyncio.run(main())


def test_create_rejects_new_run_and_marks_expired_running_for_reconciliation(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-create-expired.db"))
        await db.connect()
        schedule = [{"event_id": "old", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"}]
        assert await _create_run(
            db, run_id="old-run", schedule=schedule, started_at=1_000.0
        )

        assert not await _create_run(
            db, run_id="new-run", schedule=schedule, started_at=5_000.0
        )
        old = await db.get_live_test_run("old-run")
        assert old["status"] == "needs_reconciliation"
        assert old["stopped_at"] is None
        assert old["stop_reason"] == "expired"
        assert await db.get_live_test_run("new-run") is None
        assert await db.has_live_test_reconciliation() is True
        await db.close()

    asyncio.run(main())


def test_historical_terminal_expired_row_is_still_reconciliation_work(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-historical-expired.db"))
        await db.connect()
        schedule = [{"event_id": "old", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"}]
        assert await _create_run(
            db, run_id="historical-expired", schedule=schedule, started_at=1_000.0
        )
        await db._c.execute(
            "UPDATE live_test_runs SET status='expired', stopped_at=1060, "
            "stop_reason='expired' WHERE id='historical-expired'"
        )
        await db._c.commit()

        assert await db.has_live_test_reconciliation() is True
        pending = await db.get_live_test_reconciliation_run()
        assert pending is not None
        assert pending["id"] == "historical-expired"
        assert pending["status"] == "expired"
        await db.close()

    asyncio.run(main())


def test_reservation_authorizes_scope_schedule_and_audit_in_one_transaction(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-authorization.db"))
        await db.connect()
        schedule = [
            {"event_id": "right", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"},
            {"event_id": "second", "account_id": FIXED_ACCOUNT_IDS[1], "kind": "text"},
            {"event_id": "wrong-group", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"},
            {"event_id": "wrong-account", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"},
            {"event_id": "wrong-kind", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "text"},
        ]
        assert await _create_run(
            db,
            run_id="authorized-run",
            schedule=schedule,
            group_id=-5428680940,
            cap=10,
            started_at=5_000.0,
        )

        assert await db.reserve_live_test_event(
            "authorized-run", "right", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680940, scripted=True, now=5_001.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "right", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680940, scripted=True, now=5_002.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "second", FIXED_ACCOUNT_IDS[1], "text",
            group_id=-5428680940, scripted=True, request_id="request-1",
            now=5_002.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "wrong-group", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680941, scripted=True, now=5_002.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "wrong-account", "outside", "text",
            group_id=-5428680940, scripted=True, now=5_002.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "wrong-kind", FIXED_ACCOUNT_IDS[0], "image",
            group_id=-5428680940, scripted=True, now=5_002.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "not-scheduled", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680940, scripted=True, now=5_002.0,
        )

        assert await db.reserve_live_test_event(
            "authorized-run", "organic:text:one", FIXED_ACCOUNT_IDS[1], "text",
            group_id=-5428680940, scripted=False, now=5_003.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "organic:text:wrong-group", FIXED_ACCOUNT_IDS[1], "text",
            group_id=-5428680941, scripted=False, now=5_003.0,
        )
        assert not await db.reserve_live_test_event(
            "authorized-run", "organic:text:wrong-account", "outside", "text",
            group_id=-5428680940, scripted=False, now=5_003.0,
        )

        event = await db.get_live_test_event("authorized-run", "right")
        assert event == {
            "run_id": "authorized-run",
            "event_id": "right",
            "account_id": FIXED_ACCOUNT_IDS[0],
            "group_id": -5428680940,
            "kind": "text",
            "state": "reserved",
            "reserved_at": 5_001.0,
            "completed_at": None,
            "detail": "",
            "request_id": "",
            "snapshot_sha256": "",
            "output_sha256": "",
            "trigger_received_at": 0.0,
            "snapshot_at": 0.0,
            "profile_id": "",
            "content_sha256": "",
            "decode_metadata_sha256": "",
        }
        assert not await db.finish_live_test_event(
            "authorized-run", "right", "sent", request_id="different"
        )
        assert await db.mark_live_test_event_rpc_started(
            "authorized-run",
            "right",
            account_id=FIXED_ACCOUNT_IDS[0],
            group_id=FIXED_GROUP_ID,
            kind="text",
        )
        assert await db.finish_live_test_event(
            "authorized-run", "right", "sent", kind="text",
        )
        assert not await db.finish_live_test_event(
            "authorized-run", "right", "failed"
        )
        await db.close()

    asyncio.run(main())


def test_released_reservations_do_not_consume_hard_cap_but_failed_attempts_do(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-cap-states.db"))
        await db.connect()
        assert await _create_run(
            db,
            run_id="cap-states",
            schedule=[],
            group_id=-5428680940,
            cap=1,
            started_at=6_000.0,
        )
        assert await db.reserve_live_test_event(
            "cap-states", "organic:text:cancelled", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680940, scripted=False, now=6_000.5,
        )
        assert await db.finish_live_test_event(
            "cap-states", "organic:text:cancelled", "cancelled",
            "generation failed before RPC",
        )
        assert await db.reserve_live_test_event(
            "cap-states", "organic:text:released", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680940, scripted=False, now=6_001.0,
        )
        assert await db.finish_live_test_event(
            "cap-states", "organic:text:released", "released", "pre-RPC revoke"
        )
        assert await db.reserve_live_test_event(
            "cap-states", "organic:text:replacement", FIXED_ACCOUNT_IDS[1], "text",
            group_id=-5428680940, scripted=False, now=6_002.0,
        )
        assert await db.finish_live_test_event(
            "cap-states", "organic:text:replacement", "failed", "RPC unknown",
            kind="text",
        )
        assert not await db.reserve_live_test_event(
            "cap-states", "organic:text:overflow", FIXED_ACCOUNT_IDS[2], "text",
            group_id=-5428680940, scripted=False, now=6_003.0,
        )
        assert not await db.reserve_live_test_event(
            "cap-states", "organic:text:released", FIXED_ACCOUNT_IDS[0], "text",
            group_id=-5428680940, scripted=False, now=6_004.0,
        )
        status = await db.get_live_test_run("cap-states")
        assert status["reserved"] == 3
        assert status["cancelled"] == 1
        assert status["released"] == 1
        assert status["failed"] == 1
        assert status["cap_used"] == 1
        await db.close()

    asyncio.run(main())


def test_realtime_media_reservation_and_finish_require_exact_complete_evidence(
    tmp_path,
):
    async def main():
        db = Database(str(tmp_path / "live-test-media-evidence.db"))
        await db.connect()
        schedule = [
            {"event_id": "voice-bound", "account_id": FIXED_ACCOUNT_IDS[0], "kind": "voice"},
            {"event_id": "video-bound", "account_id": FIXED_ACCOUNT_IDS[1], "kind": "video"},
        ]
        assert await _create_run(
            db,
            run_id="media-evidence",
            schedule=schedule,
            group_id=-5428680940,
            cap=2,
            started_at=7_000.0,
        )
        request_id = "c" * 32
        snapshot_sha256 = "d" * 64
        output_sha256 = "e" * 64
        content_sha256 = "f" * 64
        trigger_received_at = 6_999.0
        snapshot_at = 7_000.0
        profile_id = "21"
        assert not await db.reserve_live_test_event(
            "media-evidence", "voice-bound", FIXED_ACCOUNT_IDS[0], "voice",
            group_id=-5428680940, scripted=True, now=7_001.0,
        )
        assert not await db.reserve_live_test_event(
            "media-evidence", "voice-bound", FIXED_ACCOUNT_IDS[0], "voice",
            group_id=-5428680940, scripted=True,
            request_id=request_id.upper(), snapshot_sha256=snapshot_sha256,
            output_sha256=output_sha256, now=7_001.0,
        )
        assert not await db.reserve_live_test_event(
            "media-evidence", "voice-bound", FIXED_ACCOUNT_IDS[0], "voice",
            group_id=-5428680940, scripted=True,
            request_id=request_id, snapshot_sha256=snapshot_sha256.upper(),
            output_sha256=output_sha256, now=7_001.0,
        )
        assert not await db.reserve_live_test_event(
            "media-evidence", "voice-bound", FIXED_ACCOUNT_IDS[0], "voice",
            group_id=-5428680940, scripted=True,
            request_id=request_id, snapshot_sha256=snapshot_sha256,
            output_sha256=f" {output_sha256}", now=7_001.0,
        )
        assert not await db.reserve_live_test_event(
            "media-evidence", "voice-bound", FIXED_ACCOUNT_IDS[0], "voice",
            group_id=-5428680940, scripted=True,
            request_id=request_id, snapshot_sha256=snapshot_sha256,
            now=7_001.0,
        )
        assert await db.reserve_live_test_event(
            "media-evidence", "voice-bound", FIXED_ACCOUNT_IDS[0], "voice",
            group_id=-5428680940, scripted=True,
            request_id=request_id, snapshot_sha256=snapshot_sha256,
            output_sha256=output_sha256,
            trigger_received_at=trigger_received_at, snapshot_at=snapshot_at,
            profile_id=profile_id, content_sha256=content_sha256,
            now=7_001.0,
        )
        assert await db.mark_live_test_event_rpc_started(
            "media-evidence",
            "voice-bound",
            account_id=FIXED_ACCOUNT_IDS[0],
            group_id=FIXED_GROUP_ID,
            kind="voice",
            request_id=request_id,
            snapshot_sha256=snapshot_sha256,
            output_sha256=output_sha256,
            trigger_received_at=trigger_received_at,
            snapshot_at=snapshot_at,
            profile_id=profile_id,
            content_sha256=content_sha256,
        )
        assert not await db.finish_live_test_event(
            "media-evidence", "voice-bound", "sent"
        )
        assert not await db.finish_live_test_event(
            "media-evidence", "voice-bound", "sent",
            kind="voice", request_id=request_id,
            snapshot_sha256=snapshot_sha256,
            output_sha256="f" * 64,
            trigger_received_at=trigger_received_at, snapshot_at=snapshot_at,
            profile_id=profile_id, content_sha256=content_sha256,
        )
        assert await db.finish_live_test_event(
            "media-evidence", "voice-bound", "sent",
            kind="voice", request_id=request_id,
            snapshot_sha256=snapshot_sha256,
            output_sha256=output_sha256,
            trigger_received_at=trigger_received_at, snapshot_at=snapshot_at,
            profile_id=profile_id, content_sha256=content_sha256,
        )
        event = await db.get_live_test_event("media-evidence", "voice-bound")
        assert event is not None
        assert event["request_id"] == request_id
        assert event["snapshot_sha256"] == snapshot_sha256
        assert event["output_sha256"] == output_sha256
        assert event["trigger_received_at"] == trigger_received_at
        assert event["snapshot_at"] == snapshot_at
        assert event["profile_id"] == profile_id
        assert event["content_sha256"] == content_sha256
        assert event["decode_metadata_sha256"] == ""
        await db.close()

    asyncio.run(main())


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("kind", "video"),
        ("request_id", "a" * 32),
        ("snapshot_sha256", "a" * 64),
        ("output_sha256", "a" * 64),
        ("trigger_received_at", 6_999.0),
        ("snapshot_at", 7_002.0),
        ("profile_id", "25"),
        ("content_sha256", "a" * 64),
        ("decode_metadata_sha256", "a" * 64),
    ],
)
def test_live_test_event_finish_rejects_every_envelope_mismatch(
    tmp_path, field, wrong_value
):
    async def main():
        db = Database(str(tmp_path / f"finish-{field}.db"))
        await db.connect()
        event_id = f"voice-{field}"
        assert await _create_run(
            db,
            run_id="finish-envelope",
            schedule=[
                {
                    "event_id": event_id,
                    "account_id": FIXED_ACCOUNT_IDS[0],
                    "kind": "voice",
                }
            ],
            started_at=7_000.0,
        )
        envelope = {
            "kind": "voice",
            "request_id": "1" * 32,
            "snapshot_sha256": "2" * 64,
            "output_sha256": "3" * 64,
            "trigger_received_at": 7_000.0,
            "snapshot_at": 7_001.0,
            "profile_id": "21",
            "content_sha256": "4" * 64,
            "decode_metadata_sha256": "",
        }
        assert await db.reserve_live_test_event(
            "finish-envelope",
            event_id,
            FIXED_ACCOUNT_IDS[0],
            "voice",
            group_id=-5428680940,
            scripted=True,
            now=7_002.0,
            **{key: value for key, value in envelope.items() if key != "kind"},
        )
        assert await db.mark_live_test_event_rpc_started(
            "finish-envelope",
            event_id,
            account_id=FIXED_ACCOUNT_IDS[0],
            group_id=FIXED_GROUP_ID,
            **envelope,
        )
        tampered = dict(envelope)
        tampered[field] = wrong_value
        assert not await db.finish_live_test_event(
            "finish-envelope", event_id, "sent", **tampered
        )
        pending = await db.get_live_test_event("finish-envelope", event_id)
        assert pending is not None and pending["state"] == "rpc_started"
        assert await db.finish_live_test_event(
            "finish-envelope", event_id, "sent", **envelope
        )
        finished = await db.get_live_test_event("finish-envelope", event_id)
        assert finished is not None and finished["state"] == "sent"
        await db.close()

    asyncio.run(main())


def test_rpc_started_and_reconciliation_are_atomic_and_leave_no_pending(tmp_path):
    async def main():
        db = Database(str(tmp_path / "live-test-rpc-state.db"))
        await db.connect()
        assert await _create_run(
            db,
            run_id="rpc-state",
            schedule=[],
            cap=2,
            started_at=8_000.0,
        )
        for event_id in ("organic:pre-rpc", "organic:rpc-unknown"):
            assert await db.reserve_live_test_event(
                "rpc-state",
                event_id,
                FIXED_ACCOUNT_IDS[0],
                "text",
                group_id=FIXED_GROUP_ID,
                scripted=False,
                now=8_001.0,
            )
        assert await db.mark_live_test_event_rpc_started(
            "rpc-state",
            "organic:rpc-unknown",
            account_id=FIXED_ACCOUNT_IDS[0],
            group_id=FIXED_GROUP_ID,
            kind="text",
        )
        started = await db.get_live_test_event("rpc-state", "organic:rpc-unknown")
        assert started["state"] == "rpc_started"
        assert not await db.finish_live_test_event(
            "rpc-state", "organic:pre-rpc", "sent", kind="text"
        )

        result = await db.reconcile_live_test_events(
            "rpc-state", "process_restart"
        )
        assert result == {"released": 1, "hard_attempt": 1}
        pre_rpc = await db.get_live_test_event("rpc-state", "organic:pre-rpc")
        unknown = await db.get_live_test_event("rpc-state", "organic:rpc-unknown")
        assert pre_rpc["state"] == "released"
        assert unknown["state"] == "hard_attempt"
        status = await db.get_live_test_run("rpc-state")
        assert status["pending"] == 0
        assert status["released"] == 1
        assert status["hard_attempt"] == 1
        assert status["cap_used"] == 1
        await db.close()

    asyncio.run(main())
