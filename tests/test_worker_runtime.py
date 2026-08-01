from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from app.worker import AccountWorker


class _FakeAI:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("ai_closed")


class _FakeClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.events.append("client_disconnected")
        self.connected = False


class WorkerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_worker(events: list[str]) -> AccountWorker:
        worker = AccountWorker.__new__(AccountWorker)
        worker.account = SimpleNamespace(id="runtime-test-account")
        worker.background_tasks = []
        worker._message_tasks = set()
        worker._closing = False
        worker._closed = False
        worker.media_service = None
        worker.ai = _FakeAI(events)
        worker.client = _FakeClient(events)
        worker.state = "running"
        return worker

    async def test_close_with_drain_waits_for_active_message_before_disconnect(
        self,
    ) -> None:
        events: list[str] = []
        worker = self.make_worker(events)
        message_started = asyncio.Event()
        release_message = asyncio.Event()

        async def active_message() -> None:
            events.append("message_started")
            message_started.set()
            await release_message.wait()
            events.append("message_finished")

        message_task = asyncio.create_task(active_message())
        worker._message_tasks.add(message_task)
        await message_started.wait()

        close_task = asyncio.create_task(worker.close(drain_messages=True))
        await asyncio.sleep(0)

        self.assertFalse(close_task.done())
        self.assertNotIn("client_disconnected", events)

        release_message.set()
        await close_task

        self.assertTrue(message_task.done())
        self.assertFalse(message_task.cancelled())
        self.assertLess(
            events.index("message_finished"),
            events.index("client_disconnected"),
        )
        self.assertEqual(worker.state, "stopped")

    async def test_close_without_drain_cancels_active_message(self) -> None:
        events: list[str] = []
        worker = self.make_worker(events)
        message_started = asyncio.Event()

        async def active_message() -> None:
            events.append("message_started")
            message_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("message_cancelled")
                raise

        message_task = asyncio.create_task(active_message())
        worker._message_tasks.add(message_task)
        await message_started.wait()

        await worker.close(drain_messages=False)

        self.assertTrue(message_task.cancelled())
        self.assertLess(
            events.index("message_cancelled"),
            events.index("client_disconnected"),
        )
        self.assertEqual(worker.state, "stopped")


if __name__ == "__main__":
    unittest.main()
