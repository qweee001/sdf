from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.worker import LOGGER, AccountWorker


class _FakeAI:
    async def close(self) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False


class WorkerBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_worker() -> AccountWorker:
        worker = AccountWorker.__new__(AccountWorker)
        worker.account = SimpleNamespace(id="background-test-account")
        worker.background_tasks = []
        worker._message_tasks = set()
        worker._closing = False
        worker._closed = False
        worker.errors = 0
        worker.last_error = ""
        worker.media_service = None
        worker.ai = _FakeAI()
        worker.client = _FakeClient()
        worker.state = "online"
        return worker

    async def test_background_exception_is_retrieved_and_reported(self) -> None:
        worker = self.make_worker()

        async def fail() -> None:
            raise RuntimeError("proactive loop failed")

        with patch.object(LOGGER, "error") as log_error:
            task = worker._start_background_task("proactive", fail())
            result = await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertIsInstance(result[0], RuntimeError)
        self.assertEqual(worker.errors, 1)
        self.assertEqual(worker.last_error, "proactive loop failed")
        log_error.assert_called_once_with(
            "Account %s background task %s stopped: %s",
            "background-test-account",
            "proactive",
            "proactive loop failed",
        )

    async def test_unexpected_normal_exit_is_reported(self) -> None:
        worker = self.make_worker()

        async def stop() -> None:
            return None

        with patch.object(LOGGER, "error") as log_error:
            task = worker._start_background_task("media", stop())
            await task
            await asyncio.sleep(0)

        self.assertEqual(worker.errors, 1)
        self.assertEqual(
            worker.last_error,
            "media background task stopped unexpectedly",
        )
        self.assertEqual(log_error.call_count, 1)

    async def test_close_cancellation_is_not_reported_as_failure(self) -> None:
        worker = self.make_worker()
        started = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            await asyncio.Event().wait()

        with patch.object(LOGGER, "error") as log_error:
            task = worker._start_background_task("media", wait_forever())
            await started.wait()
            await worker.close()
            await asyncio.sleep(0)

        self.assertTrue(task.cancelled())
        self.assertEqual(worker.errors, 0)
        self.assertEqual(worker.last_error, "")
        log_error.assert_not_called()
        self.assertEqual(worker.state, "stopped")


if __name__ == "__main__":
    unittest.main()
