"""Offline checks for Qt layout, controls and worker cancellation."""
import importlib.util
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
class GuiLayoutTests(unittest.TestCase):
    def test_daily_worker_stop_reaches_runner(self):
        import gui_app
        started, release, disconnected = (threading.Event() for _ in range(3))
        messages = []
        def emit(kind, payload):
            messages.append((kind, payload))
            if kind == "disconnected":
                disconnected.set()
        class Backend:
            session = None
            def auto_daily(self, *, selected, should_stop, on_result):
                if selected != ["师门任务"]:
                    raise AssertionError("Wrong task selection")
                on_result({"message": "领取后等待"})
                started.set()
                if not release.wait(2) or not should_stop():
                    raise AssertionError("Stop did not reach daily runner")
                return {"reason": "stopped"}
            def capture(self):
                return {"png": b"offline-preview"}
            def disconnect(self):
                pass
            def shutdown(self):
                pass
        worker = gui_app.DeviceWorker(SimpleNamespace(message=SimpleNamespace(emit=emit)), backend_factory=Backend)
        try:
            self.assertTrue(worker.submit("auto_daily", selected=["师门任务"]))
            self.assertTrue(started.wait(2))
            self.assertFalse(worker.submit("answer"))
            worker.stop()
            release.set()
            self.assertTrue(disconnected.wait(2))
        finally:
            release.set()
            worker.shutdown()
            worker.thread.join(2)
        self.assertFalse(any(kind == "error" for kind, _ in messages), messages)
        self.assertIn(("done", ("auto_daily", {"reason": "stopped"})), messages)
        self.assertIn(("daily_progress", {"message": "领取后等待"}), messages)

    def test_auto_worker_remains_busy_and_stop_disconnects(self):
        import gui_app
        started = threading.Event()
        release = threading.Event()
        disconnected = threading.Event()
        messages = []
        def emit(kind, payload):
            messages.append((kind, payload))
            if kind == "disconnected":
                disconnected.set()
        class Backend:
            session = None
            def auto_answer(self, *, should_stop, on_result, **kwargs):
                on_result({"click_sent": True, "question_number": 1, "question_total": 10})
                started.set()
                if not release.wait(2):
                    raise AssertionError("Worker test was not released")
                if not should_stop():
                    raise AssertionError("Stop did not reach automatic answering")
                return {"submitted": 1, "reason": "stopped"}
            def capture(self):
                return {"png": b"offline-preview"}
            def disconnect(self):
                pass
            def shutdown(self):
                pass
        worker = gui_app.DeviceWorker(SimpleNamespace(message=SimpleNamespace(emit=emit)), backend_factory=Backend)
        try:
            self.assertTrue(worker.submit("auto_answer", random_answer=True))
            self.assertTrue(started.wait(2))
            self.assertFalse(worker.submit("answer"))
            worker.stop()
            release.set()
            self.assertTrue(disconnected.wait(2))
        finally:
            release.set()
            worker.shutdown()
            worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())
        self.assertFalse(worker.busy)
        self.assertFalse(any(kind == "error" for kind, _ in messages), messages)
        self.assertIn(("done", ("auto_answer", {"submitted": 1, "reason": "stopped"})), messages)
        self.assertTrue(any(kind == "auto_progress" for kind, _ in messages))

    def test_controls_fit_default_and_small_windows(self):
        import gui_app
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import (
            QApplication, QCheckBox, QComboBox, QLabel, QLineEdit, QPlainTextEdit,
            QPushButton, QSpinBox, QWidget,
        )

        def check_layout(app):
            window = next(w for w in app.topLevelWidgets() if isinstance(w, gui_app.MainWindow))
            try:
                window.ai_enabled.setChecked(True)
                window.model_edit.clear()
                window.random_answer.setChecked(True)
                self.assertFalse(window.ai_enabled.isEnabled())
                self.assertFalse(window.allow_trial.isEnabled())
                params = window.answer_params(execute=False)
                self.assertTrue(params["random_answer"])
                self.assertFalse(params["use_ai"])
                automatic = window.answer_params(execute=True, automatic=True)
                self.assertTrue(automatic["layout_confirmed"])
                self.assertFalse(automatic["use_ai"])
                self.assertTrue(window.answer_params(execute=True)["layout_confirmed"])
                window.random_answer.setChecked(False)
                window.ai_enabled.setChecked(False)
                window.layout_confirmed.setChecked(True)
                with self.assertRaisesRegex(gui_app.QuizError, "题目 OCR 尚未通过"):
                    window.answer_params(execute=True)
                window.last_inspection_valid = True
                window.layout_confirmed.setChecked(False)
                with self.assertRaisesRegex(gui_app.QuizError, "再勾选"):
                    window.answer_params(execute=True)
                window.layout_confirmed.setChecked(True)
                self.assertTrue(window.answer_params(execute=True)["layout_confirmed"])
                window.last_inspection_valid = False
                window.layout_confirmed.setChecked(False)
                window.random_answer.setChecked(False)
                self.assertTrue(window.ai_enabled.isEnabled())
                window.ai_enabled.setChecked(False)
                window.last_inspection_valid = True
                window.layout_confirmed.setChecked(True)
                with self.assertRaisesRegex(gui_app.QuizError, "模型 ID"):
                    window.answer_params(execute=True, automatic=True)
                window.model_edit.setText("test-model")
                automatic = window.answer_params(execute=True, automatic=True)
                self.assertTrue(automatic["use_ai"])
                self.assertTrue(automatic["allow_trial"])
                window.random_answer.setChecked(True)
                self.assertFalse(window.answer_params(execute=True, automatic=True)["use_ai"])
                window.last_inspection_valid = False
                window.layout_confirmed.setChecked(False)
                with patch.object(gui_app.QMessageBox, "question", return_value=gui_app.QMessageBox.StandardButton.Yes), \
                        patch.object(window, "run_command") as run:
                    window.start_auto_answer()
                self.assertEqual(run.call_args.args[0], "auto_answer")
                self.assertTrue(run.call_args.kwargs["execute"])
                with patch.object(window, "run_command") as run:
                    window.start_daily()
                self.assertEqual(run.call_args.args[0], "auto_daily")
                self.assertEqual(set(run.call_args.kwargs["selected"]), set(window.daily_checks))
                self.assertNotIn("api_key", run.call_args.kwargs)
                window.connected = True
                window.current_busy = True
                window._refresh_controls()
                self.assertFalse(window.auto_btn.isEnabled())
                self.assertFalse(window.random_answer.isEnabled())
                self.assertFalse(window.daily_start_btn.isEnabled())
                self.assertFalse(window.daily_inspect_btn.isEnabled())
                self.assertTrue(all(not check.isEnabled() for check in window.daily_checks.values()))
                window.on_event("daily_progress", {"message": "等待师门"})
                self.assertTrue(window.current_busy)
                window.on_event("auto_progress", {"question_number": 1, "question_total": 10})
                self.assertTrue(window.current_busy)
                window.current_busy = False
                window.connected = False
                window._refresh_controls()
                window.last_inspection_valid = False
                window.layout_confirmed.setChecked(False)
                for width, height in ((1280, 850), (1000, 690)):
                    window.resize(width, height)
                    for tab in range(window.tabs.count()):
                        window.tabs.setCurrentIndex(tab)
                        app.processEvents()
                        if tab == 0:
                            with self.subTest(size=(width, height), layout="text first"):
                                self.assertGreater(window.answer_out.width(),
                                                   window.preview_label.width() * 1.5)
                                self.assertEqual(
                                    window.dali_check.parentWidget().mapTo(window, QPoint()).y(),
                                    window.ai_enabled.parentWidget().mapTo(window, QPoint()).y(),
                                    "Task selection and answer controls should be side by side",
                                )
                        for widget in window.findChildren(QWidget):
                            if widget.isVisible() and isinstance(widget, (
                                QCheckBox, QComboBox, QLabel, QLineEdit, QPlainTextEdit,
                                QPushButton, QSpinBox,
                            )):
                                with self.subTest(size=(width, height), tab=tab,
                                                  widget=type(widget).__name__):
                                    self.assertGreaterEqual(
                                        widget.height(), widget.minimumSizeHint().height(),
                                        "Control text is vertically clipped",
                                    )
                                    self.assertGreaterEqual(
                                        widget.width(), widget.minimumSizeHint().width(),
                                        "Control text is horizontally clipped",
                                    )
                                    self.assertTrue(
                                        widget.parentWidget().rect().contains(widget.geometry()),
                                        "Control extends outside its container",
                                    )
            finally:
                window.close()
            return 0

        settings = dict.fromkeys(("adb_path", "address", "account_scope", "layout"), "")
        with patch.dict(os.environ, {"QT_QPA_PLATFORM": "offscreen"}), \
                patch.object(gui_app, "local_settings", return_value=settings), \
                patch.object(QApplication, "exec", check_layout):
            self.assertEqual(gui_app.main(), 0)


if __name__ == "__main__":
    unittest.main()
