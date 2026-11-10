from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jyd_probe.ui_automation_thread import (  # noqa: E402
    initialize_ui_automation_in_current_thread,
)
from jyd_probe.render_agent import RenderAgent  # noqa: E402
from jyd_probe.web_api import RenderJobQueue  # noqa: E402


class _StopWorker(Exception):
    pass


class UiAutomationThreadTest(unittest.TestCase):
    def test_non_windows_context_is_a_noop(self) -> None:
        with patch("jyd_probe.ui_automation_thread.os.name", "posix"):
            with initialize_ui_automation_in_current_thread():
                pass

    def test_embedded_worker_runs_inside_ui_automation_context(self) -> None:
        state = {"active": False, "entered": 0, "exited": 0}

        @contextmanager
        def fake_initializer():
            state["active"] = True
            state["entered"] += 1
            try:
                yield
            finally:
                state["active"] = False
                state["exited"] += 1

        class StopQueue:
            def get(self):
                self.assert_active()
                raise _StopWorker

            @staticmethod
            def assert_active() -> None:
                if not state["active"]:
                    raise AssertionError("worker accessed its queue before UIAutomation init")

        worker = RenderJobQueue.__new__(RenderJobQueue)
        worker._queue = StopQueue()

        with patch(
            "jyd_probe.web_api.initialize_ui_automation_in_current_thread",
            fake_initializer,
        ):
            with self.assertRaises(_StopWorker):
                worker._worker_loop()

        self.assertEqual(state["entered"], 1)
        self.assertEqual(state["exited"], 1)

    def test_render_agent_lifecycle_runs_inside_ui_automation_context(self) -> None:
        state = {"active": False, "entered": 0, "exited": 0}

        @contextmanager
        def fake_initializer():
            state["active"] = True
            state["entered"] += 1
            try:
                yield
            finally:
                state["active"] = False
                state["exited"] += 1

        agent = RenderAgent(
            object(),
            agent_id="test-agent",
            name="测试处理机",
        )
        stop_event = threading.Event()
        stop_event.set()

        def assert_registered_after_init() -> None:
            self.assertTrue(state["active"])

        with (
            patch(
                "jyd_probe.render_agent.initialize_ui_automation_in_current_thread",
                fake_initializer,
            ),
            patch.object(agent, "register", side_effect=assert_registered_after_init),
        ):
            self.assertEqual(agent.run_forever(stop_event=stop_event), 0)

        self.assertEqual(state["entered"], 1)
        self.assertEqual(state["exited"], 1)


if __name__ == "__main__":
    unittest.main()
