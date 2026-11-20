"""Choice-popup OCR tests; sanitized real OCR replay and synthetic safety cases."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from maa_adapter import ROOT
from quiz_core import QuizError, QuizStore, digest
from quiz_vision import Layout, Observation, ReadError, TextBlock, answer_once, assert_same
from ring_quiz import RingQuizReader, UnifiedQuizReader


STEM = '“海上生明月,天涯共此时”是谁的诗句?'
TITLE = "200环任务链(91/200)"
SCOPE = "synthetic-account:ring200"
FIXTURE = Path(__file__).parent / "fixtures/popup_quiz_ocr.json"


def fields(count=3, *, title=TITLE):
    layout = Layout.load(ROOT / "config/ring.layout.json")
    text = {"question": STEM,
            **{f"label_{k}": k for k in "ABCD"[:count]},
            **{f"option_{k}": v for k, v in zip("ABCD"[:count], ("张九龄", "杜牧", "李白", "王维"))}}
    return {**{name: [TextBlock(value, .99, tuple(layout.data["regions"][name]))]
               for name, value in text.items()},
            "title": [TextBlock(title, .99, (676, 104, 152, 24))]}


def reader(data=None, *, device="offline-device", scope=SCOPE, size=(834, 470), frames=None):
    layout = Layout.load(ROOT / "config/ring.layout.json")
    scale_x, scale_y = size[0] / layout.width, size[1] / layout.height
    rois = {name: (round(x * scale_x), round(y * scale_y), round(w * scale_x), round(h * scale_y))
            for name, (x, y, w, h) in layout.data["regions"].items()}
    frame = NS(shape=(size[1], size[0], 3), fields=fields() if data is None else data)
    session = NS(address=device, capture=MagicMock(side_effect=frames) if frames else lambda: frame,
                 click=MagicMock(), ocr=MagicMock(side_effect=lambda image, roi, **kw: image.fields.get(
                     next(name for name, rect in rois.items() if rect == roi), [])))
    return RingQuizReader(session, scope)


class RingQuizTests(unittest.TestCase):
    def test_real_two_option_popup_reaches_auto_submission_despite_blank_dash(self):
        import gui_backend
        replay = json.loads((FIXTURE.parent / "popup_two_options_ocr.json").read_text(encoding="utf-8"))
        size = tuple(replay["size"])
        rois = Layout.load(ROOT / "config/ring.layout.json").scaled(*size)
        data = {name: [TextBlock(b["text"], b["score"], tuple(b["box"])) for b in blocks]
                for name, blocks in replay["fields"].items()}
        frame = NS(shape=(size[1], size[0], 3))
        session = NS(address="offline-two-options", execute=False, capture=MagicMock(return_value=frame),
                     click=MagicMock(), ocr=lambda image, roi, **kwargs: data.get(
                         next((name for name, rect in rois.items() if roi == rect), ""), []))
        backend = gui_backend.DeviceBackend()
        backend.session = session
        with tempfile.TemporaryDirectory() as folder:
            backend.cfg = {"account_scope": SCOPE, "layout": ROOT / "config/dali.layout.json",
                           "database": Path(folder) / "quiz.sqlite3", "runtime_dir": Path(folder),
                           "timeout_seconds": 2}
            with patch("gui_backend.answer_once", side_effect=lambda *args, **kwargs: answer_once(
                    *args, **kwargs, sleep=lambda _: None)):
                result = backend.auto_answer(random_percentage=100, should_stop=lambda: False)
        self.assertEqual(result, {"submitted": 1, "reason": "completed"})
        session.click.assert_called_once()
        x, y = session.click.call_args.args
        boxes = Layout.load(ROOT / "config/ring.layout.json").scaled(*size, "buttons")
        self.assertTrue(any(left < x < left + width and top < y < top + height
                            for key, (left, top, width, height) in boxes.items() if key in "AB"))
        self.assertFalse(session.execute)

    def test_low_confidence_real_or_trusted_punctuation_option_is_not_ignored(self):
        layout = Layout.load(ROOT / "config/ring.layout.json")
        source = fields(2)
        for text, score in (("读不清的选项", .1), ("-", .99), ("C", .2)):
            data = {**source, "option_C": [TextBlock(text, score, tuple(layout.data["regions"]["option_C"]))]}
            with self.subTest(text=text, score=score), self.assertRaises(ReadError):
                reader(data).read()

    def test_actual_ocr_replay_without_task_title_or_counter(self):
        replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
        layout = Layout.load(ROOT / "config/ring.layout.json")
        size = tuple(replay["size"])
        regions = layout.scaled(*size)
        data = {name: [TextBlock(b["text"], b["score"], tuple(b["box"])) for b in blocks]
                for name, blocks in replay["fields"].items()}
        session = NS(address="offline", capture=lambda: NS(shape=(size[1], size[0], 3)),
                     ocr=lambda frame, roi, **kwargs: data[next(name for name, rect in regions.items()
                                                              if roi == rect)])
        observed = RingQuizReader(session, SCOPE).read()
        self.assertEqual(dict(observed.question.options), {"A": "杨贵妃", "B": "欧阳修", "C": "苏轼"})
        self.assertIn("一骑红尘妃子笑", observed.question.stem)
        self.assertEqual(set(observed.buttons), set("ABC"))

    def test_unified_reader_chooses_popup_layout_without_a_second_capture(self):
        source = reader()
        source.session.capture = MagicMock(wraps=source.session.capture)
        ocr = source.session.ocr.side_effect
        popup_rois = set(source.layout.scaled(834, 470).values())
        source.session.ocr.side_effect = lambda frame, roi, **kwargs: ocr(frame, roi, **kwargs) if roi in popup_rois else []
        shared = UnifiedQuizReader(source.session, Layout.load(ROOT / "config/dali.layout.json"), SCOPE)
        observed = shared.read()
        self.assertEqual(observed.question.stem, STEM)
        source.session.capture.assert_called_once()
        self.assertEqual(observed.buttons, {k: rect for k, rect in source.layout.scaled(834, 470, "buttons").items()
                                          if k in "ABC"})

    def test_reads_actual_stem_and_two_three_or_four_options(self):
        for count in (2, 3, 4):
            with self.subTest(count=count):
                source = reader(fields(count))
                observed = source.read()
                self.assertIsInstance(observed, Observation)
                self.assertIsInstance(source.layout, Layout)
                self.assertEqual(observed.question.stem, STEM)
                self.assertEqual(observed.question.options, tuple(zip("ABCD"[:count],
                                 ("张九龄", "杜牧", "李白", "王维")[:count])))
                self.assertEqual(set(observed.buttons), set("ABCD"[:count]))
                self.assertEqual(source.session.ocr.call_count, 9)
                self.assertFalse(observed.question.stem.startswith("第"))

    def test_question_roi_excludes_the_changing_countdown(self):
        layout = Layout.load(ROOT / "config/ring.layout.json")
        _, y, _, height = layout.data["regions"]["question"]
        self.assertLessEqual(y + height, 207, "Timer starts below the question and changes every second")
        spaced = reader(fields(title="200环任务链 （91 / 200）")).read()
        self.assertEqual(spaced.signature, reader().read().signature)

    def test_task_titles_and_ring_counts_do_not_gate_popup_answers(self):
        baseline = reader().read()
        for title in ("", "91/200", "200环任务链", "第91/200题", "200环任务链(0/200)",
                      "200环任务链(200/200)", "大理寺答题", "其他活动标题"):
            with self.subTest(title=title):
                source = reader(fields(title=title))
                self.assertEqual(source.read().signature, baseline.signature)
                self.assertNotIn("title", source.layout.scaled(834, 470))
        data = {key: blocks for key, blocks in fields().items() if key != "title"}
        self.assertEqual(reader(data).read().signature, baseline.signature)

    def test_missing_text_low_confidence_and_nonfinite_scores_rejected(self):
        for name in ("question", "label_A", "label_B", "label_C",
                     "option_A", "option_B", "option_C"):
            original = fields()[name][0]
            for blocks in ([], [replace(original, score=.79)], [replace(original, score=float("nan"))]):
                with self.subTest(name=name, blocks=blocks), self.assertRaises(ReadError):
                    reader({**fields(), name: blocks}).read()

    def test_partial_fourth_option_and_gaps_rejected(self):
        fourth = fields(4)
        for override in ({"label_D": []}, {"option_D": []},
                         {"label_D": [replace(fourth["label_D"][0], score=.2)]},
                         {"option_D": [replace(fourth["option_D"][0], score=.2)]},
                         {"label_C": [], "option_C": []}):
            with self.subTest(override=override), self.assertRaises(ReadError):
                reader({**fourth, **override}).read()

    def test_wrong_or_spatially_misaligned_labels_rejected(self):
        for letter in "ABC":
            original = fields()[f"label_{letter}"][0]
            for block in (replace(original, text="D"), replace(original, box=(0, 0, 20, 20))):
                with self.subTest(letter=letter, block=block), self.assertRaises(ReadError):
                    reader({**fields(), f"label_{letter}": [block]}).read()

    def test_duplicate_options_rejected(self):
        block = replace(fields()["option_B"][0], text="张九龄")
        with self.assertRaises(ReadError):
            reader({**fields(), "option_B": [block]}).read()

    def test_instance_is_bound_to_question_and_device_not_task_title(self):
        with patch("ring_quiz.game_day", return_value="2026-11-16"):
            first = reader().read()
            self.assertEqual(first.signature, reader().read().signature)
            self.assertEqual(first.question.instance_id,
                             digest(["2026-11-16", SCOPE, "offline-device", STEM,
                                     sorted(("张九龄", "杜牧", "李白"))]))
            changed = {**fields(), "question": [replace(fields()["question"][0], text="不同的题干?")]}
            self.assertNotEqual(first.question.instance_id, reader(changed).read().question.instance_id)
            self.assertEqual(first.signature, reader(fields(title="其他活动")).read().signature)
            for other in (reader(device="another-device"), reader(scope="other:ring200"), reader(changed)):
                with self.assertRaises(ReadError):
                    assert_same(first, other.read())
        with patch("ring_quiz.game_day", return_value="2026-11-17"):
            self.assertNotEqual(first.question.instance_id, reader().read().question.instance_id)

    def test_multiline_text_joined_and_width_normalized_context_is_stable(self):
        option = fields()["option_A"][0]
        data = {**fields(title="２００环任务链（９１／２００）"), "option_A": [
            replace(option, text="张九", box=(207, 268, 40, 18)),
            replace(option, text="龄", box=(207, 290, 20, 18))]}
        self.assertEqual(dict(reader(data).read().question.options)["A"], "张九龄")
        self.assertEqual(reader(data).read().signature, reader().read().signature)

    def test_layout_scales_buttons_and_rejects_emulator_chrome(self):
        layout = Layout.load(ROOT / "config/ring.layout.json")
        scaled = {name: [replace(block, box=tuple(v * 2 for v in block.box)) for block in blocks]
                  for name, blocks in fields().items()}
        observed = reader(scaled, size=(1668, 940)).read()
        self.assertEqual(observed.buttons, {k: rect for k, rect in layout.scaled(1668, 940, "buttons").items()
                                          if k in "ABC"})
        with self.assertRaises(ReadError):
            reader(size=(879, 510)).read()

    def test_answer_once_rejects_screen_change_without_input(self):
        changed = {**fields(), "question": [replace(fields()["question"][0], text="另一道题?")]}
        source = reader(frames=[NS(shape=(470, 834, 3), fields=fields()),
                                NS(shape=(470, 834, 3), fields=changed)])
        store = QuizStore(":memory:")
        try:
            with self.assertRaises(ReadError):
                answer_once(source, source.session, store, random_answer=True, execute=True,
                            layout_confirmed=True, sleep=lambda _: None)
            source.session.click.assert_not_called()
        finally:
            store.close()

    def test_answer_once_blocks_repeat_after_reader_and_store_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "quiz.sqlite3"
            source = reader()
            store = QuizStore(database)
            try:
                result = answer_once(source, source.session, store, random_answer=True, execute=True,
                                     layout_confirmed=True, receipt_dir=Path(temp), sleep=lambda _: None)
                self.assertTrue(result["click_sent"])
                self.assertIn(result["decision"]["answer_text"], ("张九龄", "杜牧", "李白"))
                source.session.click.assert_called_once()
            finally:
                store.close()
            restarted = reader()
            store = QuizStore(database)
            try:
                with self.assertRaises(QuizError):
                    answer_once(restarted, restarted.session, store, random_answer=True, execute=True,
                                layout_confirmed=True, receipt_dir=Path(temp), sleep=lambda _: None)
                restarted.session.click.assert_not_called()
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
