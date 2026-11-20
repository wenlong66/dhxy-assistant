"""Deterministic screenshot refresh checks; no device, timers or Qt event loop."""
import importlib.util
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
class GuiPreviewTests(unittest.TestCase):
    def setUp(self):
        import gui_app
        self.now = 100.0
        self.frames = []
        self.session = SimpleNamespace(on_frame=None, capture=Mock(side_effect=self.capture))
        self.backend = SimpleNamespace(session=None, connect=Mock(side_effect=self.connect),
                                       capture=Mock(), shutdown=Mock())
        self.messages = Mock()
        with patch.object(gui_app.threading, "Thread"):
            self.worker = gui_app.DeviceWorker(SimpleNamespace(message=self.messages),
                                               backend_factory=lambda: self.backend)

    def connect(self):
        self.backend.session = self.session
        return {"address": "offline-device"}

    def capture(self):
        frame = np.full((100, 100, 3), len(self.frames), dtype=np.uint8)
        self.frames = [*self.frames, frame]
        if self.session.on_frame is not None:
            self.session.on_frame(frame)
        return frame

    def run_worker(self, *steps):
        script = iter(steps)
        def get_command(timeout):
            self.now, command = next(script)
            if command is None:
                raise queue.Empty
            return command, {}
        self.worker.queue.get = get_command
        with patch("time.monotonic", side_effect=lambda: self.now):
            self.worker._run()

    def previews(self):
        return [call.args[1] for call in self.messages.emit.call_args_list
                if call.args[0] == "preview"]

    def test_connect_immediately_then_refresh_every_ten_seconds(self):
        self.run_worker((100, "connect"), (100.2, None), (109.9, None),
                        (110, None), (119.9, None), (120, None), (120, "exit"))
        self.assertEqual(self.session.capture.call_count, 3)
        self.assertEqual(len(self.previews()), 3)
        self.backend.capture.assert_not_called()
        self.assertTrue(all(result["png"].startswith(b"\x89PNG") for result in self.previews()))
        kinds = [call.args[0] for call in self.messages.emit.call_args_list]
        self.assertLess(kinds.index("preview"), kinds.index("done"))

    def test_operation_frames_reset_idle_refresh_without_extra_capture(self):
        for command in ("answer", "guard", "auto_answer", "auto_daily", "auto_ring"):
            with self.subTest(command=command):
                self.setUp()
                def operation(**kwargs):
                    self.session.capture()
                    frame = self.session.capture()
                    if command == "auto_answer":
                        kwargs["on_result"]({"click_sent": True})
                    elif command in ("auto_daily", "auto_ring"):
                        kwargs["on_result"]({"message": "progress", "frame": frame})
                    self.assertEqual(len(self.previews()), 3)
                    return {}
                setattr(self.backend, command, operation)
                self.run_worker((100, "connect"), (109, command), (110, None),
                                (118.9, None), (119, None), (119, "exit"))
                self.assertEqual(self.session.capture.call_count, 4)
                self.assertEqual(len(self.previews()), 4)
                self.backend.capture.assert_not_called()
                self.assertFalse(any(call.args[0] == "error"
                                     for call in self.messages.emit.call_args_list))

    def test_paused_preview_skips_operation_frames_and_periodic_capture(self):
        def answer(**kwargs):
            self.worker.pause_preview()
            self.session.capture()
            return {}
        self.backend.answer = answer
        self.run_worker((100, "connect"), (105, "answer"), (120, None), (120, "exit"))
        self.assertEqual(self.session.capture.call_count, 2)
        self.assertEqual(len(self.previews()), 1)

    def test_preview_encoding_failure_does_not_fail_operation(self):
        import gui_app
        self.backend.answer = lambda **kwargs: self.session.capture()
        with patch.object(gui_app, "preview_png", side_effect=[b"png", ValueError("bad preview")]):
            self.run_worker((100, "connect"), (105, "answer"), (120, None), (120, "exit"))
        self.assertFalse(self.worker.preview_enabled)
        self.assertEqual(self.session.capture.call_count, 2)
        self.messages.emit.assert_any_call("preview_error", "bad preview")
        self.assertFalse(any(call.args[0] == "error" for call in self.messages.emit.call_args_list))

    def test_first_capture_failure_keeps_successful_connection(self):
        self.session.capture.side_effect = RuntimeError("screenshot unavailable")
        self.run_worker((100, "connect"), (120, None), (120, "exit"))
        self.assertFalse(self.worker.preview_enabled)
        self.messages.emit.assert_any_call("preview_error", "screenshot unavailable")
        self.messages.emit.assert_any_call("done", ("connect", {"address": "offline-device"}))
        self.session.capture.assert_called_once_with()

    def test_stop_during_connect_does_not_take_first_screenshot(self):
        def connect():
            result = self.connect()
            self.worker.stop()
            return result
        self.backend.connect.side_effect = connect
        self.run_worker((100, "connect"), (120, None), (120, "exit"))
        self.session.capture.assert_not_called()
        self.assertFalse(self.worker.preview_enabled)


if __name__ == "__main__":
    unittest.main()
