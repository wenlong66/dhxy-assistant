"""Submission-to-learning association tests; all screen pixels and OCR are synthetic."""
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import numpy as np

from quiz_core import Question, QuizError, QuizStore
from quiz_vision import Observation, ReadError, TextBlock, answer_once
from test_quiz_feedback import GOLD, TEAL, GREEN, TICK


BUTTONS = {"A": (20, 40, 235, 50), "B": (270, 40, 235, 50)}
QUESTION_ROI = (20, 5, 480, 25)
OPTION_ROIS = {"A": (40, 45, 150, 35), "B": (290, 45, 150, 35)}


def screen(tick=None, body=GOLD):
    frame = np.full((110, 530, 3), 35, dtype=np.uint8)
    for label, (x, y, w, h) in BUTTONS.items():
        yy, xx = np.mgrid[:110, :530]
        mask = (xx >= x) & (xx < x + w) & (yy >= y) & (yy < y + h)
        frame = np.where(mask[..., None], body, frame).astype(np.uint8)
        if tick == label or tick == "both":
            glyph = np.pad(TICK, ((y + 11, 110 - y - 11 - TICK.shape[0]),
                                  (x + 211, 530 - x - 211 - TICK.shape[1])))
            frame = np.where(glyph[..., None], GREEN, frame).astype(np.uint8)
    return frame


class QuizLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = QuizStore(":memory:")
        self.addCleanup(self.store.close)
        self.question = Question("测试诗词题？", (("A", "甲"), ("B", "乙")), "one-view", scope="test")
        self.observed = Observation(self.question, (530, 110), BUTTONS, time.monotonic(), screen(),
                                    QUESTION_ROI, OPTION_ROIS)
        self.reader = NS(read=MagicMock(side_effect=lambda: replace(self.observed, captured_at=time.monotonic())))
        self.session = NS(click=MagicMock(), capture=MagicMock(return_value=screen("B")),
                          ocr=lambda frame, roi: [TextBlock(self.question.stem if roi == QUESTION_ROI else
                                                           ("甲" if roi == OPTION_ROIS["A"] else "乙"), .99, roi)])

    def answer(self, **kwargs):
        with patch("quiz_core.random.choice", return_value=("A", "甲")):
            return answer_once(self.reader, self.session, self.store, execute=True, layout_confirmed=True,
                               random_percentage=100, receipt_dir=Path(self.tmp.name), sleep=lambda _: None, **kwargs)

    def test_wrong_random_learns_only_revealed_correct_option(self):
        result = self.answer()
        self.assertEqual(result["outcome"], "wrong")
        row = self.store.db.execute("SELECT * FROM answers").fetchone()
        self.assertEqual((row["answer_text"], row["status"]), ("乙", "verified"))
        hit = self.store.resolve(replace(self.question, instance_id="new-view"), random_percentage=0)
        self.assertEqual((hit.origin, hit.answer_text), ("bank", "乙"))
        self.session.click.assert_called_once()

    def test_correct_random_learns_selected_answer_on_teal_or_gold(self):
        for body in (GOLD, TEAL):
            with self.subTest(body=body):
                self.session.capture.return_value = screen("A", body)
                self.question = replace(self.question, instance_id=str(body), stem=str(body))
                self.observed = replace(self.observed, question=self.question)
                result = self.answer()
                self.assertEqual(result["outcome"], "correct")
                row = self.store.db.execute("SELECT * FROM answers WHERE qkey=?", (self.question.key,)).fetchone()
                self.assertEqual(row["answer_text"], "甲")

    def test_no_tick_or_ambiguous_tick_does_not_learn(self):
        for tick in (None, "both"):
            with self.subTest(tick=tick):
                self.question = replace(self.question, instance_id=str(tick), stem=str(tick))
                self.observed = replace(self.observed, question=self.question)
                self.session.capture.return_value = screen(tick)
                self.assertEqual(self.answer()["outcome"], "awaiting_manual_feedback")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_changed_question_or_revealed_option_text_does_not_learn(self):
        for change_roi in (QUESTION_ROI, OPTION_ROIS["B"]):
            with self.subTest(roi=change_roi):
                self.question = replace(self.question, instance_id=str(change_roi), stem=str(change_roi))
                self.observed = replace(self.observed, question=self.question)
                self.session.ocr = lambda frame, roi: [TextBlock("另一道题" if roi == change_roi else self.question.stem, .99, roi)]
                self.assertEqual(self.answer()["outcome"], "awaiting_manual_feedback")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_preexisting_tick_prevents_submission(self):
        self.observed = replace(self.observed, frame=screen("B"))
        with self.assertRaises(ReadError):
            self.answer()
        self.session.click.assert_not_called()

    def test_stopping_after_click_prevents_feedback_storage(self):
        with self.assertRaises(QuizError):
            self.answer(should_stop=lambda: self.session.click.called)
        self.session.click.assert_called_once()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
