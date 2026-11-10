"""Offline tests for the GUI's headless service and cancellation boundary."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
