"""Offline confirmed-retry lifecycle through the shared answer submission path."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import gui_backend
from quiz_core import Question, QuizError, QuizStore
from test_ring_quiz import reader as popup_reader


class RingRetryBackendTests(unittest.TestCase):
    def test_confirmed_failure_retries_without_learning_an_answer(self):
        source = popup_reader(scope="offline-account")
        backend = gui_backend.DeviceBackend()
        backend.session = source.session
        backend.session.execute = False
        with tempfile.TemporaryDirectory() as folder:
            backend.cfg = {"runtime_dir": Path(folder), "database": Path(folder) / "quiz.sqlite3",
                           "account_scope": "offline-account", "layout": gui_backend.ROOT / "config/dali.layout.json"}
            actual_answer_once = gui_backend.answer_once
            def run():
                answer = runner.call_args.kwargs["answer_quiz"]
                retry = runner.call_args.kwargs["on_quiz_retry"]
                first = answer()
                with self.assertRaises(QuizError):
                    answer()
                retry()
                second = answer()
                self.assertNotEqual(first["decision"]["attempt_id"], second["decision"]["attempt_id"])
                with self.assertRaises(QuizError):
                    answer()
                return {"reason": "stopped"}
            with patch.object(gui_backend, "RingRunner") as runner, \
                    patch.object(gui_backend, "UnifiedQuizReader", return_value=source), \
                    patch.object(gui_backend, "answer_once", side_effect=lambda *args, **kwargs: actual_answer_once(
                        *args, **kwargs, sleep=lambda _: None)):
                runner.return_value.run.side_effect = run
                self.assertEqual(backend.auto_ring(should_stop=lambda: False, random_percentage=100)["reason"], "stopped")
            self.assertEqual(source.session.click.call_count, 2)
            self.assertFalse(source.session.execute)
            store = QuizStore(backend.cfg["database"])
            try:
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                outcomes = [row[0] for row in store.db.execute("SELECT outcome FROM attempts WHERE submitted_at IS NOT NULL ORDER BY rowid")]
                self.assertEqual(outcomes, ["unknown", "pending"])
            finally:
                store.close()

    def test_restart_only_releases_pending_attempt_for_reidentified_question(self):
        source = popup_reader(scope="offline-account")
        backend = gui_backend.DeviceBackend()
        backend.session = source.session
        backend.session.execute = False
        with tempfile.TemporaryDirectory() as folder:
            backend.cfg = {"runtime_dir": Path(folder), "database": Path(folder) / "quiz.sqlite3",
                           "account_scope": "offline-account", "layout": gui_backend.ROOT / "config/dali.layout.json"}
            store = QuizStore(backend.cfg["database"])
            old = store.resolve(source.read().question, random_percentage=100)
            self.assertTrue(store.claim_submission(old, source.read().question))
            unrelated = Question("另一道题？", (("A", "甲"), ("B", "乙")), "unrelated", scope="offline-account")
            other = store.resolve(unrelated, random_percentage=100)
            self.assertTrue(store.claim_submission(other, unrelated))
            store.close()
            actual_answer_once = gui_backend.answer_once
            with patch.object(gui_backend, "RingRunner") as runner, \
                    patch.object(gui_backend, "UnifiedQuizReader", return_value=source), \
                    patch.object(gui_backend, "answer_once", side_effect=lambda *args, **kwargs: actual_answer_once(
                        *args, **kwargs, sleep=lambda _: None)):
                def run():
                    runner.call_args.kwargs["on_quiz_retry"]()
                    self.assertTrue(runner.call_args.kwargs["answer_quiz"]()["click_sent"])
                    return {"reason": "stopped"}
                runner.return_value.run.side_effect = run
                backend.auto_ring(should_stop=lambda: False, random_percentage=100)
            source.session.click.assert_called_once()
            store = QuizStore(backend.cfg["database"])
            try:
                self.assertEqual(store.db.execute("SELECT outcome FROM attempts WHERE id=?", (old.attempt_id,)).fetchone()[0], "unknown")
                self.assertEqual(store.db.execute("SELECT outcome FROM attempts WHERE id=?", (other.attempt_id,)).fetchone()[0], "pending")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
            finally:
                store.close()

    def test_unreadable_question_can_retry_after_confirmed_failure_without_learning(self):
        from test_popup_question_icon import FIELDS, source as unreadable_source
        source, session = unreadable_source({**FIELDS, "question": []}, unified=True)
        backend = gui_backend.DeviceBackend()
        backend.session = session
        session.execute = False
        with tempfile.TemporaryDirectory() as folder:
            backend.cfg = {"runtime_dir": Path(folder), "database": Path(folder) / "quiz.sqlite3",
                           "account_scope": "offline-account", "layout": gui_backend.ROOT / "config/dali.layout.json"}
            actual_answer_once = gui_backend.answer_once
            with patch.object(gui_backend, "RingRunner") as runner, \
                    patch.object(gui_backend, "UnifiedQuizReader", return_value=source), \
                    patch.object(gui_backend, "answer_once", side_effect=lambda *args, **kwargs: actual_answer_once(
                        *args, **kwargs, sleep=lambda _: None)):
                def run():
                    answer = runner.call_args.kwargs["answer_quiz"]
                    first = answer()
                    runner.call_args.kwargs["on_quiz_retry"]()
                    second = answer()
                    self.assertNotEqual(first["decision"]["attempt_id"], second["decision"]["attempt_id"])
                    with self.assertRaises(QuizError):
                        answer()
                    return {"reason": "stopped"}
                runner.return_value.run.side_effect = run
                self.assertEqual(backend.auto_ring(should_stop=lambda: False)["reason"], "stopped")
            self.assertEqual(session.click.call_count, 2)
            self.assertFalse(session.execute)
            store = QuizStore(backend.cfg["database"])
            try:
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                self.assertEqual([row[0] for row in store.db.execute(
                    "SELECT outcome FROM attempts WHERE submitted_at IS NOT NULL ORDER BY rowid")], ["unknown", "pending"])
            finally:
                store.close()

    def test_cancellation_does_not_rearm_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = gui_backend.DeviceBackend()
            backend.session = MagicMock(execute=False)
            backend.cfg = {"runtime_dir": Path(folder), "account_scope": "offline-account",
                           "layout": gui_backend.ROOT / "config/dali.layout.json"}
            with patch.object(gui_backend, "RingRunner") as runner, patch.object(gui_backend, "UnifiedQuizReader") as reader:
                def run():
                    with self.assertRaisesRegex(QuizError, "已停止"):
                        runner.call_args.kwargs["on_quiz_retry"]()
                    return {"reason": "stopped"}
                runner.return_value.run.side_effect = run
                backend.auto_ring(should_stop=lambda: True)
                reader.return_value.confirm_retry.assert_not_called()
            self.assertFalse(backend.session.execute)


if __name__ == "__main__":
    unittest.main()
