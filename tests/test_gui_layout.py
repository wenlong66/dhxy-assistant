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
                self.assertEqual(window.random_percentage.value(), 100)
                self.assertFalse(window.ai_enabled.isChecked())
                self.assertFalse(window.result_details_btn.isEnabled())
                window.ai_enabled.setChecked(True)
                with patch.object(gui_app, "ai_settings", return_value={"model": "test-model", "api_key": "local-test-key"}):
                    window.random_percentage.setValue(60)
                    ai_params = window.answer_params(execute=False)
                self.assertEqual(ai_params["random_percentage"], 40)
                self.assertTrue(ai_params["use_ai"])
                self.assertEqual(ai_params["api_key"], "local-test-key")
                with patch.object(gui_app, "ai_settings", return_value={"model": "", "api_key": ""}):
                    with self.assertRaisesRegex(gui_app.QuizError, "ai.local.json"):
                        window.answer_params(execute=False)
                window.ai_enabled.setChecked(False)
                window.random_answer.setChecked(True)
                self.assertFalse(window.random_percentage.isEnabled())
                params = window.answer_params(execute=False)
                self.assertEqual(params["random_percentage"], 100)
                automatic = window.answer_params(execute=True, automatic=True)
                self.assertTrue(automatic["layout_confirmed"])
                self.assertEqual(automatic["random_percentage"], 100)
                self.assertTrue(window.answer_params(execute=True)["layout_confirmed"])
                window.random_answer.setChecked(False)
                self.assertTrue(window.random_percentage.isEnabled())
                window.random_percentage.setValue(50)
                window.layout_confirmed.setChecked(True)
                with self.assertRaisesRegex(gui_app.QuizError, "题目 OCR 尚未通过"):
                    window.answer_params(execute=True)
                window.last_inspection_valid = True
                window.layout_confirmed.setChecked(False)
                with self.assertRaisesRegex(gui_app.QuizError, "再勾选"):
                    window.answer_params(execute=True)
                window.layout_confirmed.setChecked(True)
                self.assertEqual(window.answer_params(execute=True)["random_percentage"], 50)
                window.last_inspection_valid = False
                window.layout_confirmed.setChecked(False)
                automatic = window.answer_params(execute=True, automatic=True)
                self.assertEqual(automatic["random_percentage"], 50)
                self.assertFalse(automatic["use_ai"])
                self.assertEqual(automatic["api_key"], "")
                window.random_answer.setChecked(True)
                with patch.object(gui_app.QMessageBox, "question", return_value=gui_app.QMessageBox.StandardButton.Yes), \
                        patch.object(window, "run_command") as run:
                    window.start_auto_answer()
                self.assertEqual(run.call_args.args[0], "auto_answer")
                self.assertTrue(run.call_args.kwargs["execute"])
                with patch.object(window, "run_command") as run:
                    window.start_daily()
                self.assertEqual(run.call_args.args[0], "auto_daily")
                self.assertEqual(set(run.call_args.kwargs["selected"]), set(window.daily_checks))
                self.assertEqual(run.call_args.kwargs["task_interval_max"], 180)
                self.assertEqual(window.daily_interval_max.minimum(), 10)
                window.daily_interval_max.setValue(90)
                with patch.object(window, "run_command") as interval_run:
                    window.start_daily()
                self.assertEqual(interval_run.call_args.kwargs["task_interval_max"], 90)
                window.daily_interval_max.setValue(180)
                self.assertIn("大理寺答题", run.call_args.kwargs["selected"])
                self.assertIs(window.daily_checks["大理寺答题"], window.dali_check)
                window.dali_check.setChecked(False)
                with patch.object(window, "run_command") as run:
                    window.start_daily()
                self.assertNotIn("大理寺答题", run.call_args.kwargs["selected"])
                window.dali_check.setChecked(True)
                self.assertEqual(run.call_args.kwargs["api_key"], "")
                window.dali_check.setChecked(False)
                with patch.object(window, "run_command") as run:
                    window.start_ring()
                self.assertEqual(run.call_args.args[0], "auto_ring")
                self.assertEqual(run.call_args.kwargs["random_percentage"], 100)
                window.dali_check.setChecked(True)
                window.connected = True
                window.current_busy = True
                window._refresh_controls()
                self.assertFalse(window.auto_btn.isEnabled())
                self.assertFalse(window.random_answer.isEnabled())
                self.assertFalse(window.random_percentage.isEnabled())
                self.assertFalse(window.daily_start_btn.isEnabled())
                self.assertFalse(window.ring_start_btn.isEnabled())
                self.assertFalse(window.daily_inspect_btn.isEnabled())
                self.assertFalse(window.daily_interval_max.isEnabled())
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
                answer = {"click_sent": False, "outcome": "not_submitted", "decision": {
                    "label": "B", "answer_text": "测试答案" * 100, "origin": "bank", "can_submit": True,
                }}
                window.on_event("done", ("answer", answer))
                self.assertTrue(window.answer_summary.isReadOnly())
                self.assertIn("仅预览", window.answer_summary.text())
                self.assertIn("题库", window.answer_summary.text())
                self.assertEqual(window.answer_summary.cursorPosition(), 0)
                window.result_details_btn.click()
                app.processEvents()
                self.assertTrue(window.answer_dialog.isVisible())
                self.assertFalse(window.answer_dialog.isModal())
                self.assertTrue(window.answer_out.isReadOnly())
                self.assertEqual(gui_app.json.loads(window.answer_out.toPlainText()), answer)
                progress = {**answer, "click_sent": True, "outcome": "wrong",
                            "question_number": 2, "question_total": 10}
                window.on_event("auto_progress", progress)
                self.assertIn("2/10", window.answer_summary.text())
                self.assertEqual(gui_app.json.loads(window.answer_out.toPlainText()), progress)
                partial = {"click_sent": True, "decision": {"label": "B", "answer_text": "*B*", "origin": "random_unreadable"}}
                window._show_answer_result(partial)
                self.assertIn("随机兜底", window.answer_summary.text())
                self.assertIn("选项文字未识别", window.answer_summary.text())
                partial_bank = {**partial, "decision": {"label": "C", "answer_text": "2", "origin": "bank_unreadable"}}
                window._show_answer_result(partial_bank)
                self.assertIn("题库匹配", window.answer_summary.text())
                window._show_answer_result(progress)
                window.on_event("done", ("auto_answer", {"reason": "completed", "submitted": 10}))
                self.assertIn("共提交 10 题", window.answer_summary.text())
                self.assertEqual(gui_app.json.loads(window.answer_out.toPlainText()), progress)
                window.answer_dialog.close()
                for command, result, summary in (
                    ("inspect_daily", {"png": b"", "valid": True, "cards": [], "diagnostics": "offline"}, "日常列表识别通过"),
                    ("auto_daily", {"reason": "completed"}, "所选日常均已核对完成"),
                    ("auto_ring", {"reason": "stopped"}, "200环已停止"),
                ):
                    with self.subTest(result=command):
                        window.on_event("done", (command, result))
                        self.assertIn(summary, window.answer_summary.text())
                        self.assertEqual(gui_app.json.loads(window.answer_out.toPlainText()),
                                         {k: v for k, v in result.items() if k != "png"})
                for width, height in ((1280, 850), (1000, 690)):
                    window.resize(width, height)
                    for tab in range(window.tabs.count()):
                        window.tabs.setCurrentIndex(tab)
                        app.processEvents()
                        if tab == 0:
                            with self.subTest(size=(width, height), layout="text first"):
                                answer_controls = window.random_percentage.parentWidget()
                                self.assertEqual(answer_controls.title(), "答题控制")
                                for control in (window.preview_btn, window.execute_btn, window.auto_btn,
                                                window.pause_btn, window.answer_summary, window.result_details_btn):
                                    self.assertIs(control.parentWidget(), answer_controls)
                                self.assertEqual(window.preview_btn.y(), window.execute_btn.y())
                                self.assertEqual(window.auto_btn.y(), window.pause_btn.y())
                                self.assertGreater(window.auto_btn.y(), window.preview_btn.y())
                                self.assertEqual(window.answer_summary.y(), window.result_details_btn.y())
                                self.assertLess(window.answer_summary.height(), window.preview_btn.height() * 2)
                                self.assertFalse(window.answer_out.isVisible())
                                self.assertEqual(
                                    window.dali_check.parentWidget().mapTo(window, QPoint()).y(),
                                    answer_controls.mapTo(window, QPoint()).y(),
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
