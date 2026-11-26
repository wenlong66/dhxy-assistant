"""Sanitized singing quiz and failed-dialogue OCR replays; no device input or AI."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import numpy as np

from daily_tasks import DailyStopped, DailyView
from maa_adapter import ROOT
from quiz_core import QuizError, QuizStore
from quiz_vision import Layout, ReadError, TextBlock, answer_once
from ring_quiz import UnifiedQuizReader
from ring_tasks import QUIZ_FAILURE_DISMISS, RingRunner, quiz_failure_visible
from test_daily_tasks import assert_clicks_in_boxes
from test_ring_tasks import screen

REPLAY = json.loads((Path(__file__).parent / "fixtures/popup_singing_ocr.json").read_text(encoding="utf-8"))


def failure(name="歌王", speech="咋样，通不过我的考验吧"):
    width, height = REPLAY["size"]
    blocks = [TextBlock(name if field == "name" else speech, b["score"], tuple(b["box"]))
              for field, values in REPLAY["failure"].items() for b in values]
    return DailyView(np.zeros((height, width, 3), np.uint8), blocks)


class SingingQuizTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.now = time.monotonic()
        clock = patch("time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.session = MagicMock()
        self.runner = RingRunner(self.session, self.folder, lambda: False,
                                 answer_quiz=MagicMock(return_value={"click_sent": True}),
                                 on_quiz_retry=MagicMock())
        self.runner.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        self.runner.save = MagicMock()
        self.runner.save_metrics = MagicMock()

    def feed(self, *views):
        frames = iter(views)
        def read():
            current = next(frames, views[-1])
            return DailyView(current.frame, current.blocks, self.now)
        self.runner.read = read

    def test_failure_matches_only_calibrated_npc_name_regardless_of_speech(self):
        for name in ("诗词鬼", "歌王", "问题鬼"):
            for speech in ("", "答得不错", "通不过我的考验吧"):
                with self.subTest(name=name, speech=speech):
                    self.assertTrue(quiz_failure_visible(failure(name, speech)))
            current = failure(name)
            name_only = DailyView(current.frame, [b for b in current.blocks if b.text == name])
            self.assertTrue(quiz_failure_visible(name_only))
        for name in ("其他NPC", "问题", "小问题鬼"):
            with self.subTest(name=name), self.assertRaises(QuizError):
                self.runner.dismiss_quiz_failure(failure(name))
        current = failure()
        unreadable = DailyView(current.frame, [replace(b, score=.79) for b in current.blocks])
        self.assertFalse(quiz_failure_visible(unreadable))
        self.session.click.assert_not_called()

    def test_failed_singing_quiz_dismisses_once_and_answers_again(self):
        self.feed(screen("quiz", count=22), failure(), failure(), screen("quiz", count=22), screen("battle"))
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.assertEqual(self.runner.answer_quiz.call_count, 2)
        self.runner.on_quiz_retry.assert_called_once_with()
        box = tuple(round(v * s) for v, s in zip(QUIZ_FAILURE_DISMISS, (830 / 834, 466 / 470) * 2))
        assert_clicks_in_boxes(self, [self.session.click.call_args.args], box)

    def test_failed_question_ghost_quiz_dismisses_once_and_answers_again(self):
        self.feed(screen("quiz", count=34), failure("问题鬼"), failure("问题鬼", speech=""),
                  screen("quiz", count=34), screen("battle"))
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.assertEqual(self.runner.answer_quiz.call_count, 2)
        self.runner.on_quiz_retry.assert_called_once_with()
        self.session.click.assert_called_once()

    def test_singing_failure_at_start_precedes_activity_entry(self):
        self.feed(failure(), failure(), screen("quiz", count=22), screen("battle"))
        with patch.object(self.runner, "open_ring") as activity:
            self.runner.run()
        activity.assert_not_called()
        self.runner.answer_quiz.assert_called_once_with()
        self.runner.on_quiz_retry.assert_called_once_with()
        self.session.click.assert_called_once()

    def test_changed_npc_or_page_during_pause_does_not_click_or_rearm(self):
        quiz = DailyView(failure().frame, screen("quiz").blocks)
        for current in (failure("诗词鬼"), failure("问题鬼"), failure("其他NPC"), quiz):
            with self.subTest():
                self.feed(current)
                self.runner.dismiss_quiz_failure(failure())
        self.session.click.assert_not_called()
        self.runner.on_quiz_retry.assert_not_called()

    def test_stop_or_stale_failure_does_not_click(self):
        with patch.object(self.runner, "sleep", side_effect=DailyStopped), self.assertRaises(DailyStopped):
            self.runner.dismiss_quiz_failure(failure())
        current = failure()
        stale = DailyView(current.frame, current.blocks, self.now - 11)
        with self.assertRaisesRegex(QuizError, "过期"):
            self.runner.dismiss_quiz_failure(stale)
        self.session.click.assert_not_called()
        self.runner.on_quiz_retry.assert_not_called()

    def test_singing_failure_still_open_is_not_clicked_again(self):
        self.feed(failure())
        with self.assertRaisesRegex(QuizError, "未重复点击"):
            self.runner.dismiss_quiz_failure(failure())
        self.session.click.assert_called_once()
        self.runner.on_quiz_retry.assert_not_called()

    def test_real_two_option_lyrics_use_shared_submission_and_duplicate_guard(self):
        size = tuple(REPLAY["size"])
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*size)
        fields = {name: [TextBlock(b["text"], b["score"], tuple(b["box"])) for b in values]
                  for name, values in REPLAY["fields"].items()}
        frame = np.zeros((size[1], size[0], 3), np.uint8)
        def ocr(image, roi, **kwargs):
            return fields.get(next((name for name, rect in regions.items() if rect == roi), ""), [])
        session = NS(address="offline-singing", capture=lambda: frame, ocr=ocr, click=MagicMock())
        reader = UnifiedQuizReader(session, Layout.load(ROOT / "config/dali.layout.json"), "offline-account")
        observed = reader.read()
        self.assertEqual(observed.question.stem, "摇晃的红酒杯嘴唇像")
        self.assertEqual(observed.question.options, (("A", "染着鲜血"), ("B", "涂了口红")))
        store = QuizStore(self.folder / "quiz.sqlite3")
        self.addCleanup(store.close)
        def answer():
            return answer_once(reader, session, store, execute=True, layout_confirmed=True,
                               receipt_dir=self.folder, random_percentage=100, sleep=lambda _: None)
        self.assertTrue(answer()["click_sent"])
        with self.assertRaises(QuizError):
            answer()
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        session.click.assert_called_once()

    def test_unreadable_lyric_field_reports_which_region_failed(self):
        from test_ring_quiz import fields, reader
        data = {**fields(2), "option_B": []}
        with self.assertRaisesRegex(ReadError, "option_B"):
            reader(data).read()


if __name__ == "__main__":
    unittest.main()
