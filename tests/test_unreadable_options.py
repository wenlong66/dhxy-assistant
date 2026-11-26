"""Offline safety tests for partially unreadable option text."""
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

from quiz_core import Question, QuizError, QuizStore
from quiz_vision import Layout, QuizReader, ReadError, TextBlock, answer_once, recheck_for_answer


class UnreadableOptionTests(unittest.TestCase):
    def setUp(self):
        self.layout = Layout.load(Path("config/dali.layout.json"))
        self.regions = self.layout.scaled(834, 470)
        self.frame = np.empty((470, 834, 3), dtype=np.uint8)
        self.frame[:] = (220, 240, 250)
        self.data = {"question": [TextBlock("第10/10题：数字测试题？", .99, (280, 160, 200, 20))]}
        for label, value in zip("ABCD", ("3", "1", "2", "0")):
            self.data[f"label_{label}"] = [TextBlock(label, .99, self.regions[f"label_{label}"])]
            self.data[f"option_{label}"] = [TextBlock(value, .99, self.regions[f"option_{label}"])]
        self.data["option_B"] = []
        self.session = SimpleNamespace(address="offline", capture=MagicMock(return_value=self.frame), click=MagicMock())
        self.session.ocr = lambda frame, roi, **kwargs: self.data[next(k for k, v in self.regions.items() if v == roi)]
        self.reader = QuizReader(self.session, self.layout, "offline")

    def read(self):
        from quiz_vision import read_for_answer
        return read_for_answer(self.reader)

    def test_real_cropped_buttons_allow_unreadable_numeric_option(self):
        crop = np.asarray(Image.open(Path(__file__).parent / "fixtures/dali_numeric_buttons.png").convert("RGB"))[:, :, ::-1]
        self.frame[250:375, 277:635] = crop
        with tempfile.TemporaryDirectory() as folder, QuizStoreContext() as store, \
                patch("quiz_core.random.choice", side_effect=lambda options: options[1]):
            result = answer_once(self.reader, self.session, store, execute=True, layout_confirmed=True,
                                 random_percentage=100, receipt_dir=Path(folder), sleep=lambda _: None)
        self.assertEqual((result["decision"]["label"], result["decision"]["origin"]), ("B", "random_unreadable"))
        self.session.click.assert_called_once()

    def test_missing_text_is_preserved_as_unreadable_not_a_fake_answer(self):
        observed = self.read()
        self.assertEqual(observed.question.unreadable_options, ("B",))
        self.assertFalse(observed.question.is_readable)
        self.assertEqual(set(observed.buttons), set("ABCD"))
        self.assertEqual(dict(observed.question.options)["A"], "3")

    def test_low_confidence_options_and_every_label_can_use_fallback(self):
        base = dict(self.data)
        for label in "ABCD":
            with self.subTest(label=label):
                data = dict(base)
                data["option_B"] = [TextBlock("1", .99, self.regions["option_B"])]
                data[f"option_{label}"] = [TextBlock("?", .2, self.regions[f"option_{label}"])]
                self.data = data
                self.assertEqual(self.read().question.unreadable_options, (label,))

    def test_two_three_and_four_options_include_unreadable_last_button(self):
        original = dict(self.data)
        for count in (2, 3, 4):
            with self.subTest(count=count):
                self.data = dict(original)
                self.data["option_B"] = [TextBlock("1", .99, self.regions["option_B"])]
                last = "ABCD"[count - 1]
                self.data[f"option_{last}"] = []
                for label in "ABCD"[count:]:
                    self.data[f"label_{label}"] = []
                    self.data[f"option_{label}"] = []
                observed = self.read()
                self.assertEqual(tuple(observed.buttons), tuple("ABCD"[:count]))
                self.assertEqual(observed.question.unreadable_options, (last,))

    def test_unrecognized_option_glyph_is_never_treated_as_real_text(self):
        self.data["option_B"] = [TextBlock("�", .99, self.regions["option_B"])]
        observed = self.read()
        self.assertEqual(observed.question.unreadable_options, ("B",))
        self.assertEqual(dict(observed.question.options)["B"], "*B*")

    def test_unconfirmed_button_or_missing_frame_is_rejected(self):
        x, y, w, h = self.layout.scaled(834, 470, group="buttons")["B"]
        self.frame[y:y+h, x:x+w] = 0
        with self.assertRaisesRegex(ReadError, "按钮"):
            self.read()
        self.session.click.assert_not_called()

    def test_missing_or_wrong_label_still_rejects(self):
        for blocks in ([], [TextBlock("C", .99, self.regions["label_B"])],
                       [TextBlock("B", .1, self.regions["label_B"])]):
            with self.subTest(blocks=blocks):
                self.data["label_B"] = blocks
                with self.assertRaises(ReadError):
                    self.read()
        self.session.click.assert_not_called()

    def test_random_fallback_selects_unreadable_button_once_without_learning(self):
        with tempfile.TemporaryDirectory() as folder, QuizStoreContext() as store, \
                patch("quiz_core.random.choice", side_effect=lambda options: options[1]):
            result = answer_once(self.reader, self.session, store, execute=True, layout_confirmed=True,
                                 random_percentage=100, receipt_dir=Path(folder), sleep=lambda _: None)
            self.assertEqual((result["decision"]["label"], result["decision"]["origin"]), ("B", "random_unreadable"))
            self.assertTrue(result["click_sent"])
            store.feedback(result["decision"]["attempt_id"], "correct", "offline")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
            with self.assertRaises(QuizError):
                answer_once(self.reader, self.session, store, execute=True, layout_confirmed=True,
                            random_percentage=100, receipt_dir=Path(folder), sleep=lambda _: None)
        self.session.click.assert_called_once()

    def test_changed_unreadable_option_pixels_are_rejected_before_click(self):
        changed = self.frame.copy()
        x, y, _, _ = self.layout.scaled(834, 470, group="buttons")["B"]
        changed[y + 5, x + 5] = 0
        self.session.capture.side_effect = [self.frame, changed]
        with tempfile.TemporaryDirectory() as folder, QuizStoreContext() as store, self.assertRaises(ReadError):
            answer_once(self.reader, self.session, store, execute=True, layout_confirmed=True,
                        random_percentage=100, receipt_dir=Path(folder), sleep=lambda _: None)
        self.session.click.assert_not_called()

    def test_small_button_pixel_jitter_does_not_block_fallback(self):
        changed = self.frame.copy()
        x, y, _, _ = self.layout.scaled(834, 470, group="buttons")["B"]
        changed[y + 5, x + 5] = (217, 237, 247)
        self.session.capture.side_effect = [self.frame, changed, changed, changed]
        with tempfile.TemporaryDirectory() as folder, QuizStoreContext() as store, \
                patch("quiz_core.random.choice", side_effect=lambda options: options[1]):
            result = answer_once(self.reader, self.session, store, execute=True, layout_confirmed=True,
                                 random_percentage=100, receipt_dir=Path(folder), sleep=lambda _: None)
        self.assertTrue(result["click_sent"])
        self.session.click.assert_called_once()

    def test_recognition_regression_keeps_question_unlearnable_even_if_it_was_readable(self):
        self.data["option_B"] = [TextBlock("1", .99, self.regions["option_B"])]
        first = self.read()
        self.data["option_B"] = []
        current = self.read()
        stable = recheck_for_answer(first, current)
        self.assertFalse(stable.question.is_readable)
        self.assertEqual(stable.question.unreadable_options, ("B",))

    def test_changed_question_or_option_label_is_not_submitted(self):
        first = self.read()
        self.data["question"] = [TextBlock("第9/10题：数字测试题？", .99, (280, 160, 200, 20))]
        with self.assertRaises(ReadError):
            recheck_for_answer(first, self.read())
        self.data["question"] = [TextBlock("第10/10题：数字测试题？", .99, (280, 160, 200, 20))]
        self.data["label_B"] = [TextBlock("C", .99, self.regions["label_B"])]
        with self.assertRaises(ReadError):
            self.read()
        self.session.click.assert_not_called()

    def test_cancellation_during_pause_does_not_click(self):
        stopped = []
        def sleep(seconds):
            if seconds != .25:
                stopped.append(True)
        with tempfile.TemporaryDirectory() as folder, QuizStoreContext() as store, self.assertRaises(QuizError):
            answer_once(self.reader, self.session, store, execute=True, layout_confirmed=True,
                        random_percentage=100, should_stop=lambda: bool(stopped),
                        receipt_dir=Path(folder), sleep=sleep)
        self.session.click.assert_not_called()

    def test_verified_answer_can_match_readable_option_without_learning(self):
        with QuizStoreContext() as store:
            full = Question("第10/10题：数字测试题？", tuple(zip("ABCD", ("3", "1", "2", "0"))),
                            "prior", scope="offline")
            with patch("quiz_core.random.choice", return_value=("C", "2")):
                previous = store.resolve(full, random_percentage=100)
            store.feedback(previous.attempt_id, "correct", "offline verified")
            partial = self.read().question
            decision = store.resolve(partial, random_percentage=0)
            self.assertEqual((decision.label, decision.answer_text, decision.origin), ("C", "2", "bank_unreadable"))
            self.assertTrue(store.claim_submission(decision, partial))
            store.feedback(decision.attempt_id, "wrong", "offline")
            self.assertEqual(store.db.execute("SELECT status FROM answers WHERE qkey=?", (full.key,)).fetchone()[0], "verified")

    def test_verified_bank_answer_runs_through_click_without_learning(self):
        with tempfile.TemporaryDirectory() as folder, QuizStoreContext() as store:
            full = Question("第10/10题：数字测试题？", tuple(zip("ABCD", ("3", "1", "2", "0"))),
                            "prior", scope="offline")
            with patch("quiz_core.random.choice", return_value=("C", "2")):
                previous = store.resolve(full, random_percentage=100)
            store.feedback(previous.attempt_id, "correct", "offline")
            provider = MagicMock()
            result = answer_once(self.reader, self.session, store, provider=provider, execute=True,
                                 layout_confirmed=True, random_percentage=0, receipt_dir=Path(folder), sleep=lambda _: None)
            self.assertEqual((result["decision"]["label"], result["decision"]["origin"]), ("C", "bank_unreadable"))
            self.assertEqual(result["outcome"], "awaiting_manual_feedback")
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 1)
            provider.answer.assert_not_called()
            self.session.click.assert_called_once()

    def test_ambiguous_bank_falls_back_without_ai(self):
        with QuizStoreContext() as store:
            for values in (("3", "1", "2", "0"), ("3", "4", "2", "0")):
                question = Question("第10/10题：数字测试题？", tuple(zip("ABCD", values)),
                                    "prior-" + values[1], scope="offline")
                with patch("quiz_core.random.choice", return_value=("A", "3")):
                    decision = store.resolve(question, random_percentage=100)
                store.feedback(decision.attempt_id, "correct", "offline verified")
            provider = MagicMock()
            with patch("quiz_core.random.choice", side_effect=lambda options: options[1]):
                decision = store.resolve(self.read().question, provider, random_percentage=0)
            self.assertEqual((decision.label, decision.origin), ("B", "random_unreadable"))
            provider.answer.assert_not_called()

    def test_fully_random_mode_ignores_verified_partial_bank(self):
        with QuizStoreContext() as store:
            full = Question("第10/10题：数字测试题？", tuple(zip("ABCD", ("3", "1", "2", "0"))),
                            "prior", scope="offline")
            with patch("quiz_core.random.choice", return_value=("C", "2")):
                previous = store.resolve(full, random_percentage=100)
            store.feedback(previous.attempt_id, "correct", "offline")
            with patch("quiz_core.random.choice", side_effect=lambda options: options[1]):
                decision = store.resolve(self.read().question, random_percentage=100)
            self.assertEqual((decision.label, decision.origin), ("B", "random_unreadable"))


class QuizStoreContext:
    def __enter__(self):
        self.store = QuizStore(":memory:")
        return self.store

    def __exit__(self, *args):
        self.store.close()


if __name__ == "__main__":
    unittest.main()
