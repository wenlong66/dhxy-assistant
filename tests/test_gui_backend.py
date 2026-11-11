"""Offline tests for the GUI's headless service and cancellation boundary."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import gui_backend
from gui_backend import DeviceBackend, query_bank, save_local_settings
from quiz_core import AIAnswer, Decision, OpenAIProvider, Question, QuizError, QuizStore
from quiz_vision import Layout, Observation, answer_once


class SettingsAndBankTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_config_stores_only_allowlisted_fields_not_key(self):
        target = self.base / "local.json"
        with patch.object(gui_backend, "LOCAL_CONFIG", target):
            data = {"adb_path": "C:/adb/adb.exe", "address": "127.0.0.1:5555",
                    "account_scope": "test:profile", "layout": "config/dali.layout.json"}
            save_local_settings(data)
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["address"], data["address"])
            self.assertNotIn("OPENAI_API_KEY", saved)
            self.assertNotIn("api_key", saved)
            self.assertEqual(gui_backend.local_settings()["account_scope"], data["account_scope"])

    def test_config_unknown_key_cannot_be_saved(self):
        target = self.base / "local.json"
        with patch.object(gui_backend, "LOCAL_CONFIG", target):
            with self.assertRaises(QuizError):
                save_local_settings({"address": "127.0.0.1:5555", "api_key": "SENSITIVE"})
            self.assertFalse(target.exists())

    def test_bank_scoped_search_is_read_only_and_escaped(self):
        db = self.base / "中文.sqlite3"
        store = QuizStore(db)
        with store.transaction() as tx:
            tx.execute("INSERT INTO answers(qkey,scope,stem,options_json,answer_text,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                       ("key-a", "profile-a", "100% 正确的是", "{}", "选项A", "verified", "2026-01-01"))
            tx.execute("INSERT INTO answers(qkey,scope,stem,options_json,answer_text,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                       ("key-b", "profile-b", "别的题", "{}", "选项B", "candidate", "2026-01-01"))
        store.close()
        rows = query_bank(db, "profile-a", "100%")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qkey"], "key-a")
        self.assertEqual(query_bank(db, "profile-a", "100_"), [])
        self.assertEqual(query_bank(db, "profile-b")[0]["qkey"], "key-b")
        self.assertEqual(query_bank(self.base / "missing.db", "profile-a"), [])

    def test_openai_explicit_secret_is_not_from_env(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-key"}):
            provider = OpenAIProvider("fake-model", api_key="in-memory-secret")
            self.assertEqual(provider._api_key, "in-memory-secret")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "environment-key")


class FakeSession:
    def __init__(self, cfg, *, execute=False):
        self.cfg = cfg
        self.execute = execute
        self.address = "127.0.0.1:5555"
        self.closed = False
        self.connected = False
        self.click_count = 0

    def device_list(self):
        return [{"name": "Mock", "adb_path": "adb", "address": "127.0.0.1:5555"}]

    def connect(self):
        self.connected = True
        return self

    def capture(self):
        import numpy as np
        return np.zeros((720, 1280, 3), dtype=np.uint8)

    def click(self, x, y):
        if not self.execute:
            raise QuizError("Not executable")
        self.click_count += 1

    def close(self):
        self.closed = True


class BackendSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cfg = {
            "database": self.base / "quiz.sqlite3", "runtime_dir": self.base / "maa",
            "layout": Path(gui_backend.ROOT) / "config/dali.layout.json",
            "account_scope": "test-profile", "timeout_seconds": 4,
            "adb_path": "", "address": ""
        }
        self.backend = DeviceBackend(session_factory=FakeSession)
        self.backend._config = lambda: self.cfg
        self.addCleanup(self.backend.shutdown)

    def test_connect_capture_disconnect_without_click(self):
        self.assertEqual(len(self.backend.devices()), 1)
        self.assertEqual(self.backend.connect()["address"], "127.0.0.1:5555")
        p = self.backend.capture()
        self.assertEqual((p["width"], p["height"]), (1280, 720))
        self.assertTrue(p["png"].startswith(b"\x89PNG"))
        self.assertEqual(self.backend.session.click_count, 0)
        self.assertTrue(self.cfg["database"].with_suffix(".device.lock").exists())
        with self.assertRaises(QuizError):
            self.backend.connect()
        self.backend.disconnect()
        self.assertFalse(self.cfg["database"].with_suffix(".device.lock").exists())

    def test_feedback_cannot_verify_unsubmitted_attempt(self):
        store = QuizStore(self.cfg["database"])
        q = Question("第1/10题:什么？", tuple((k, v) for k, v in zip("ABCD", ["甲", "乙", "丙", "丁"])), "instance", scope="test-profile")
        with store.transaction() as db:
            db.execute("INSERT INTO answers(qkey,scope,stem,options_json,answer_text,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                       (q.key, q.scope, q.stem, '{}', '甲', 'candidate', '2026-01-01'))
        decision = store.resolve(q)
        store.close()
        with self.assertRaises(QuizError):
            self.backend.feedback(decision.attempt_id, "correct", "I saw correct")

    def test_random_answer_ignores_ai_configuration(self):
        self.backend.connect()
        with patch.object(gui_backend, "OpenAIProvider") as provider, patch.object(
            gui_backend, "answer_once", return_value={"click_sent": False}
        ) as answer:
            self.backend.answer(random_answer=True, use_ai=True, model="")
        provider.assert_not_called()
        self.assertTrue(answer.call_args.kwargs["random_answer"])
        self.assertIsNone(answer.call_args.kwargs["provider"])
        self.assertFalse(self.backend.session.execute)

    def test_guard_checks_question_region_instead_of_title(self):
        self.backend.connect()
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend.session, "guard_scene", create=True
        ) as guard:
            reader.return_value.read.return_value.size = (834, 470)
            result = self.backend.guard()
        self.assertTrue(result["matched"])
        reader.return_value.read.assert_called_once_with()
        guard.assert_called_once_with((279, 155, 353, 78))

    def auto_frame(self, number, total=3):
        question = Question(f"第{number}/{total}题:测试{number}？", (("A", "甲"), ("B", "乙")), f"frame-{number}")
        return Observation(question, (834, 470), {"A": (277, 250, 172, 58), "B": (462, 250, 173, 58)}, time.monotonic())

    def test_auto_ten_questions_with_real_submission_guards_and_store(self):
        for random_answer in (True, False):
            with self.subTest(random=random_answer):
                self.cfg["database"] = self.base / f"auto-{random_answer}.sqlite3"
                self.backend.connect()
                provider = MagicMock()
                provider.answer.side_effect = lambda question, rejected, image=None: AIAnswer(
                    "answer", dict(question.options)["A"], "Offline fixture", "test-model")
                if not random_answer:
                    store = QuizStore(self.cfg["database"])
                    first = store.resolve(self.auto_frame(1, 10).question, provider)
                    store.feedback(first.attempt_id, "correct", "Offline cache fixture")
                    store.close()
                    provider.answer.reset_mock()
                progress = []
                with patch.object(gui_backend, "QuizReader") as reader, patch.object(
                    gui_backend, "OpenAIProvider", return_value=provider
                ), patch.object(gui_backend, "answer_once", side_effect=lambda *args, **kw: answer_once(
                    *args, sleep=lambda _: None, **kw
                )):
                    reader.return_value.read.side_effect = lambda: self.auto_frame(self.backend.session.click_count + 1, 10)
                    result = self.backend.auto_answer(random_answer=random_answer, model="test-model", layout_confirmed=True,
                                                     should_stop=lambda: False, on_result=progress.append)
                self.assertEqual(result, {"submitted": 10, "reason": "completed"})
                self.assertEqual(self.backend.session.click_count, 10)
                self.assertEqual([r["question_number"] for r in progress], list(range(1, 11)))
                self.assertEqual(provider.answer.call_count, 0 if random_answer else 9)
                self.assertFalse(self.backend.session.execute)
                store = QuizStore(self.cfg["database"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM attempts WHERE submitted_at IS NOT NULL").fetchone()[0], 10)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers WHERE status='verified'").fetchone()[0], 0 if random_answer else 1)
                store.close()
                self.backend.disconnect()

    def test_auto_tolerates_transient_ocr_failure_before_submitting(self):
        self.backend.connect()
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", return_value={"click_sent": True}
        ) as answer, patch("gui_backend.time.sleep"):
            reader.return_value.read.side_effect = [QuizError("transition"), self.auto_frame(1, 1)]
            self.assertEqual(self.backend.auto_answer(random_answer=True, layout_confirmed=True,
                                                     should_stop=lambda: False)["submitted"], 1)
            answer.assert_called_once()

    def test_auto_waits_for_new_question_and_finishes_last(self):
        self.backend.connect()
        frames = [self.auto_frame(n) for n in (1, 1, 2, 2, 3)]
        results = []
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", side_effect=lambda **kw: {"click_sent": True}
        ) as answer, patch("gui_backend.time.sleep"):
            reader.return_value.read.side_effect = frames
            result = self.backend.auto_answer(random_answer=True, layout_confirmed=True,
                                             should_stop=lambda: False, on_result=results.append)
        self.assertEqual(result["submitted"], 3)
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(answer.call_count, 3)
        self.assertEqual([r["question_number"] for r in results], [1, 2, 3])
        self.assertTrue(all(not c.kwargs["use_ai"] for c in answer.call_args_list))
        self.assertEqual(answer.call_args_list[0].kwargs["expected_view_token"], frames[0].question.view_token)

    def test_auto_nonrandom_uses_ai_and_allows_candidate_submission(self):
        self.backend.connect()
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", return_value={"click_sent": True}
        ) as answer:
            reader.return_value.read.return_value = self.auto_frame(1, 1)
            self.backend.auto_answer(random_answer=False, model="test-model", layout_confirmed=True,
                                     should_stop=lambda: False)
        self.assertTrue(answer.call_args.kwargs["use_ai"])
        self.assertTrue(answer.call_args.kwargs["allow_trial"])
        self.assertTrue(answer.call_args.kwargs["execute"])

    def test_auto_stop_prevents_next_question_submission(self):
        self.backend.connect()
        stopped = False
        def on_result(result):
            nonlocal stopped
            stopped = True
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", return_value={"click_sent": True}
        ) as answer:
            reader.return_value.read.return_value = self.auto_frame(1)
            result = self.backend.auto_answer(random_answer=True, layout_confirmed=True,
                                             should_stop=lambda: stopped, on_result=on_result)
        self.assertEqual(result, {"submitted": 1, "reason": "stopped"})
        answer.assert_called_once()

    def test_auto_stops_on_skipped_question_and_on_no_answer(self):
        self.backend.connect()
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", side_effect=lambda **kw: {"click_sent": True}
        ) as answer:
            reader.return_value.read.side_effect = [self.auto_frame(1), self.auto_frame(3)]
            with self.assertRaisesRegex(QuizError, "题号"):
                self.backend.auto_answer(random_answer=True, layout_confirmed=True, should_stop=lambda: False)
            self.assertEqual(answer.call_count, 1)
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", return_value={"click_sent": False, "decision": {"status": "needs_review"}}
        ) as answer:
            reader.return_value.read.return_value = self.auto_frame(1)
            with self.assertRaisesRegex(QuizError, "可提交"):
                self.backend.auto_answer(random_answer=True, layout_confirmed=True, should_stop=lambda: False)
            answer.assert_called_once()

    def test_auto_times_out_without_repeating_click(self):
        self.backend.connect()
        frame = self.auto_frame(1)
        with patch.object(gui_backend, "QuizReader") as reader, patch.object(
            self.backend, "answer", return_value={"click_sent": True}
        ) as answer, patch("gui_backend.time.sleep"), patch(
            "gui_backend.time.monotonic", side_effect=[0, 0, 0, 10]
        ):
            reader.return_value.read.return_value = frame
            with self.assertRaisesRegex(QuizError, "等待"):
                self.backend.auto_answer(random_answer=True, layout_confirmed=True, should_stop=lambda: False)
            answer.assert_called_once()


class CancellationTests(unittest.TestCase):
    def test_cancel_after_api_response_prevents_game_click(self):
        cancelled = False
        q = Question("第1/10题:什么？", tuple((k, v) for k, v in zip("ABCD", ["甲", "乙", "丙", "丁"])), "instance")
        obs = Observation(q, (1280, 720), {k: (x * 200, 500, 180, 70) for x, k in enumerate("ABCD")}, time.monotonic())

        class Reader:
            def read(self):
                return obs

        class Store:
            def resolve(self, question, provider, **kwargs):
                nonlocal cancelled
                cancelled = True
                return Decision(q.key, q.view_token, "甲", "A", "candidate", "openai", "", "some-id", True)

            def claim_submission(self, *args):
                raise AssertionError("Cancellation must happen before submission claim")

        class Session:
            def click(self, *args):
                raise AssertionError("Cancellation must prevent all clicks")

        with self.assertRaisesRegex(QuizError, "cancelled"):
            answer_once(Reader(), Session(), Store(), provider=object(), execute=True,
                        layout_confirmed=True, allow_trial=True,
                        should_stop=lambda: cancelled, sleep=lambda _: None)

    def test_cancel_before_work_never_calls_provider(self):
        class Reader:
            def read(self):
                raise AssertionError("Cancelled before screenshot")
        with self.assertRaisesRegex(QuizError, "cancelled"):
            answer_once(Reader(), object(), object(), should_stop=lambda: True)


if __name__ == "__main__":
    unittest.main()
