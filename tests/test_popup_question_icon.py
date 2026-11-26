"""Real question-decoration crop and sanitized OCR; no device input or AI."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

from maa_adapter import ROOT
from quiz_core import QuizError, QuizStore
from quiz_vision import (Layout, ReadError, TextBlock, answer_once, assert_same,
                         read_for_answer, record_revealed_feedback)
from ring_quiz import RingQuizReader, UnifiedQuizReader, question_submission_image_token

FIXTURES = Path(__file__).parent / "fixtures"
REPLAY = json.loads((FIXTURES / "popup_question_icon_ocr.json").read_text(encoding="utf-8"))
FIELDS = {name: [TextBlock(b["text"], b["score"], tuple(b["box"])) for b in blocks]
          for name, blocks in REPLAY["fields"].items()}
STEM = FIELDS["question"][1].text


def source(fields=None, *, unified=False, decoration=True):
    fields = FIELDS if fields is None else fields
    width, height = REPLAY["size"]
    layout = Layout.load(ROOT / "config/ring.layout.json")
    regions = layout.scaled(width, height)
    x, y, w, h = regions["question"]
    crop = np.asarray(Image.open(FIXTURES / "popup_question_icon.png").convert("RGB"))[:, :, ::-1]
    frame = np.pad(crop, ((y, height - y - h), (x, width - x - w), (0, 0)))
    if not decoration:
        frame = np.zeros_like(frame)
    def ocr(image, roi, **kwargs):
        name = next((name for name, rect in regions.items() if rect == roi), None)
        if name is not None:
            return fields.get(name, [])
        left, top, width, height = roi
        return [b for b in fields.get("question", []) if left <= b.box[0] and top <= b.box[1]
                and b.box[0] + b.box[2] <= left + width and b.box[1] + b.box[3] <= top + height]
    session = NS(address="offline-question-icon", capture=lambda: frame, ocr=ocr, click=MagicMock())
    reader = (UnifiedQuizReader(session, Layout.load(ROOT / "config/dali.layout.json"), "offline-account")
              if unified else RingQuizReader(session, "offline-account"))
    return reader, session


class PopupQuestionIconTests(unittest.TestCase):
    def test_real_decoration_is_not_part_of_question_in_either_reader(self):
        for unified in (False, True):
            with self.subTest(unified=unified):
                reader, _ = source(unified=unified)
                observed = reader.read()
                self.assertEqual(observed.question.stem, STEM)
                self.assertEqual(set(observed.buttons), set("ABC"))
                self.assertEqual(dict(observed.question.options)["C"], "只要墨尔本翡翠")

    def test_overlapping_low_confidence_detections_are_rerecognized_as_one_line(self):
        stem = "我想要怒放的生命 就象矗立在彩虹之颠 就象穿行璀璨的-"
        recorded = [(stem, .982248, (394, 303, 730, 35)),
                    ("マ", .587504, (363, 307, 43, 26)),
                    ("—", .355617, (1113, 309, 46, 23))]
        width, height = REPLAY["size"]
        blocks = [TextBlock(text, score, (round(x * width / 1600), round(y * height / 900),
                                         round(w * width / 1600), round(h * height / 900)))
                  for text, score, (x, y, w, h) in recorded]
        reader, session = source({**FIELDS, "question": blocks}, unified=True)
        original_ocr = session.ocr
        regions = {**Layout.load(ROOT / "config/dali.layout.json").scaled(width, height),
                   **{f"popup_{name}": roi for name, roi in
                      Layout.load(ROOT / "config/ring.layout.json").scaled(width, height).items()}}
        retried = []
        def ocr(frame, roi, **kwargs):
            if kwargs.get("single_line") and roi not in regions.values():
                retried.append(roi)
                self.assertTrue(all(roi[0] <= b.box[0] and roi[1] <= b.box[1]
                                    and b.box[0] + b.box[2] <= roi[0] + roi[2]
                                    and b.box[1] + b.box[3] <= roi[1] + roi[3] for b in blocks))
                return [TextBlock(stem, .98, roi)]
            return original_ocr(frame, roi, **kwargs)
        session.ocr = ocr
        observed = reader.read()
        self.assertEqual(observed.question.stem, stem)
        self.assertTrue(observed.question.is_readable)
        self.assertEqual(len(retried), 1)
        session.click.assert_not_called()

    def test_line_reread_cannot_invent_numeral_from_trailing_dash(self):
        stem = "我想要怒放的生命 就象矗立在彩虹之颠 就象穿行璀璨的"
        initial = {**FIELDS, "question": [replace(FIELDS["question"][1], text=stem)]}
        changed = {**initial, "question": [replace(FIELDS["question"][1], text=stem + "-"),
                                            TextBlock("", 0., (630, 155, 19, 20))]}
        reader, session = source(initial, unified=True)
        session.capture = MagicMock(wraps=session.capture)
        original_ocr = session.ocr
        known_rois = {*Layout.load(ROOT / "config/dali.layout.json").scaled(*REPLAY["size"]).values(),
                      *Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"]).values()}
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])
        original_frame = session.capture()
        yy, xx = np.indices(original_frame.shape[:2])
        jittered = np.where(((xx == 300) & (yy == 190))[..., None], 0, original_frame).astype(np.uint8)
        session.capture.reset_mock()
        session.capture.side_effect = lambda: original_frame if session.capture.call_count == 1 else jittered
        def ocr(frame, roi, **kwargs):
            if kwargs.get("single_line") and roi not in known_rois:
                return [TextBlock(stem + "一", .986985, roi)]
            if session.capture.call_count >= 2:
                name = next((name for name, rect in regions.items() if rect == roi), None)
                if name is not None:
                    return changed.get(name, [])
            return original_ocr(frame, roi, **kwargs)
        session.ocr = ocr
        with tempfile.TemporaryDirectory() as folder:
            store = QuizStore(":memory:")
            provider = MagicMock()
            try:
                result = answer_once(reader, session, store, provider=provider, random_percentage=0,
                                     execute=True, layout_confirmed=True, receipt_dir=Path(folder),
                                     sleep=lambda _: None)
                self.assertTrue(result["click_sent"])
                self.assertEqual(result["decision"]["origin"], "random_unreadable")
                session.click.assert_called_once()
                provider.answer.assert_not_called()
                with self.assertRaises(QuizError):
                    answer_once(reader, session, store, provider=provider, random_percentage=0,
                                execute=True, layout_confirmed=True, receipt_dir=Path(folder), sleep=lambda _: None)
                session.click.assert_called_once()
                store.feedback(result["decision"]["attempt_id"], "correct", "离线回放")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
            finally:
                store.close()

    def test_line_reread_keeps_weak_digits_and_negations_in_the_input(self):
        for prefix in ("1", "不"):
            for score in (.99, .2):
                with self.subTest(prefix=prefix, score=score):
                    weak = TextBlock(prefix, .2, (210, 155, 4, 20))
                    reader, session = source({**FIELDS, "question": [weak, FIELDS["question"][1]]})
                    original_ocr = session.ocr
                    def ocr(frame, roi, **kwargs):
                        if kwargs.get("single_line") and roi[0] == weak.box[0]:
                            self.assertLessEqual(roi[1], weak.box[1])
                            self.assertGreaterEqual(roi[0] + roi[2], weak.box[0] + weak.box[2])
                            return [TextBlock(prefix + STEM, score, roi)]
                        return original_ocr(frame, roi, **kwargs)
                    session.ocr = ocr
                    if score < .8:
                        with self.assertRaises(ReadError):
                            reader.read()
                    else:
                        observed = reader.read()
                        self.assertEqual(observed.question.stem, prefix + STEM)
                        self.assertTrue(observed.question.is_readable)
                    session.click.assert_not_called()

    def test_without_colored_icon_low_confidence_digit_is_not_discarded(self):
        reader, session = source(decoration=False)
        with self.assertRaisesRegex(ReadError, "question"):
            reader.read()
        session.click.assert_not_called()

    def test_low_confidence_real_text_or_partial_icon_overlap_still_fails(self):
        noise, question = FIELDS["question"]
        for blocks in ([noise, replace(question, score=.79)],
                       [replace(noise, box=(200, 152, 20, 16)), question],
                       [noise, replace(question, text="不", box=(220, 155, 16, 20), score=.2)],
                       [noise]):
            with self.subTest(blocks=blocks), self.assertRaisesRegex(ReadError, "question"):
                source({**FIELDS, "question": blocks})[0].read()

    def test_icon_detection_changes_do_not_change_question_identity(self):
        noisy = source()[0].read()
        clean = source({**FIELDS, "question": [FIELDS["question"][1]]})[0].read()
        assert_same(noisy, clean)
        changed = source({**FIELDS, "question": [replace(FIELDS["question"][1], text="不是同一道题？")]})[0].read()
        with self.assertRaises(ReadError):
            assert_same(noisy, changed)

    def test_unified_submission_stays_one_click_and_retains_duplicate_guard(self):
        reader, session = source(unified=True)
        with tempfile.TemporaryDirectory() as folder:
            store = QuizStore(Path(folder) / "quiz.sqlite3")
            try:
                def submit():
                    return answer_once(reader, session, store, random_percentage=100, execute=True,
                                       layout_confirmed=True, receipt_dir=Path(folder), sleep=lambda _: None)
                self.assertTrue(submit()["click_sent"])
                with self.assertRaises(QuizError):
                    submit()
                session.click.assert_called_once()
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
            finally:
                store.close()

    def test_unreadable_question_uses_random_without_ai_learning_or_duplicate_clicks(self):
        for blocks in ([], [replace(FIELDS["question"][1], score=.2)]):
            reader, session = source({**FIELDS, "question": blocks}, unified=True)
            provider = MagicMock()
            with self.subTest(blocks=blocks), tempfile.TemporaryDirectory() as folder:
                store = QuizStore(Path(folder) / "quiz.sqlite3")
                try:
                    options = dict(provider=provider, random_percentage=0, execute=True,
                                   layout_confirmed=True, receipt_dir=Path(folder), sleep=lambda _: None)
                    with patch("quiz_core.random.choice", return_value=("A", "只要天边的明月")) as choose:
                        result = answer_once(reader, session, store, **options)
                        self.assertTrue(result["click_sent"])
                        self.assertEqual(result["decision"]["origin"], "random_unreadable")
                        with self.assertRaises(QuizError):
                            answer_once(reader, session, store, **options)
                        choose.assert_called_once()
                    session.click.assert_called_once()
                    provider.answer.assert_not_called()
                    store.feedback(result["decision"]["attempt_id"], "correct", "离线手动反馈", source="manual")
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
                    snapshot = store.db.execute("SELECT stem FROM attempts LIMIT 1").fetchone()[0]
                    self.assertEqual(snapshot, "")
                finally:
                    store.close()

    def test_readability_changes_after_reader_restart_do_not_allow_another_click(self):
        bad = {**FIELDS, "question": []}
        for first_fields, second_fields in ((FIELDS, bad), (bad, FIELDS)):
            with self.subTest(first_readable=first_fields is FIELDS), tempfile.TemporaryDirectory() as folder:
                store = QuizStore(Path(folder) / "quiz.sqlite3")
                try:
                    options = dict(random_percentage=100, execute=True, layout_confirmed=True,
                                   receipt_dir=Path(folder), sleep=lambda _: None)
                    first_reader, first_session = source(first_fields, unified=True)
                    self.assertTrue(answer_once(first_reader, first_session, store, **options)["click_sent"])
                    second_reader, second_session = source(second_fields, unified=True)
                    with self.assertRaises(QuizError):
                        answer_once(second_reader, second_session, store, **options)
                    first_session.click.assert_called_once()
                    second_session.click.assert_not_called()
                finally:
                    store.close()

    def test_late_question_ocr_failure_keeps_choice_but_disables_learning(self):
        reader, session = source(unified=True)
        session.capture = MagicMock(wraps=session.capture)
        original_ocr = session.ocr
        question_roi = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])["question"]
        def ocr(frame, roi, **kwargs):
            if roi == question_roi and session.capture.call_count >= 3:
                return []
            return original_ocr(frame, roi, **kwargs)
        session.ocr = ocr
        with tempfile.TemporaryDirectory() as folder:
            store = QuizStore(Path(folder) / "quiz.sqlite3")
            try:
                with patch("quiz_core.random.choice", return_value=("A", "只要天边的明月")) as choose:
                    result = answer_once(reader, session, store, random_percentage=100, execute=True,
                                         layout_confirmed=True, receipt_dir=Path(folder), sleep=lambda _: None)
                    choose.assert_called_once()
                self.assertEqual(result["decision"]["origin"], "random_unreadable")
                self.assertEqual(result["decision"]["answer_text"], "只要天边的明月")
                self.assertEqual(session.capture.call_count, 4)
                session.click.assert_called_once()
                store.feedback(result["decision"]["attempt_id"], "correct", "离线反馈")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
            finally:
                store.close()

    def test_ocr_identity_drift_on_same_pixels_falls_back_to_one_random_click(self):
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])
        changed = {**FIELDS,
                   "question": [replace(FIELDS["question"][1], text="整段题干识别结果发生明显偏差")],
                   "option_A": [replace(FIELDS["option_A"][0], text="无法稳定识别的歌词")]}
        for changed_at in (2, 3, 4):
            with self.subTest(changed_at=changed_at), tempfile.TemporaryDirectory() as folder:
                reader, session = source(unified=True)
                session.capture = MagicMock(wraps=session.capture)
                original_ocr = session.ocr
                def ocr(frame, roi, **kwargs):
                    if session.capture.call_count >= changed_at:
                        name = next((name for name, rect in regions.items() if rect == roi), None)
                        if name is not None:
                            return changed.get(name, [])
                    return original_ocr(frame, roi, **kwargs)
                session.ocr = ocr
                provider = MagicMock()
                pauses = []
                store = QuizStore(":memory:")
                try:
                    with patch("quiz_core.random.choice", return_value=("B", FIELDS["option_B"][0].text)) as choose, \
                            patch("quiz_vision.random.uniform", return_value=.6):
                        result = answer_once(reader, session, store, provider=provider,
                                             random_percentage=0 if changed_at == 2 else 100,
                                             execute=True, layout_confirmed=True, receipt_dir=Path(folder),
                                             sleep=pauses.append)
                    self.assertTrue(result["click_sent"])
                    self.assertEqual(result["decision"]["origin"], "random_unreadable")
                    self.assertEqual(result["decision"]["label"], "B")
                    choose.assert_called_once()
                    provider.answer.assert_not_called()
                    self.assertAlmostEqual(sum(pauses[1:]), .6)
                    self.assertEqual(session.capture.call_count, 4)
                    session.click.assert_called_once()
                    with self.assertRaises(QuizError):
                        answer_once(reader, session, store, execute=True, layout_confirmed=True,
                                    random_percentage=100, receipt_dir=Path(folder), sleep=lambda _: None)
                    session.click.assert_called_once()
                    store.feedback(result["decision"]["attempt_id"], "correct", "离线反馈")
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                finally:
                    store.close()

    def test_missing_and_recovered_stem_with_same_options_does_not_retry_forever(self):
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])
        for first_readable in (False, True):
            with self.subTest(first_readable=first_readable), tempfile.TemporaryDirectory() as folder:
                reader, session = source(unified=True)
                original = session.capture()
                yy, xx = np.indices(original.shape[:2])
                changed = np.where(((xx > 300) & (xx < 340) & (yy > 175) & (yy < 195))[..., None],
                                   0, original).astype(np.uint8)
                def capture():
                    return original if session.capture.call_count % 2 else changed
                session.capture = MagicMock(side_effect=capture)
                original_ocr = session.ocr
                def ocr(frame, roi, **kwargs):
                    readable = (session.capture.call_count % 2 == 1) == first_readable
                    if roi == regions["question"] and not readable:
                        return []
                    return original_ocr(frame, roi, **kwargs)
                session.ocr = ocr
                provider = MagicMock()
                store = QuizStore(":memory:")
                try:
                    with patch("quiz_core.random.choice", return_value=("B", FIELDS["option_B"][0].text)) as choose:
                        result = answer_once(reader, session, store, provider=provider, random_percentage=0,
                                             execute=True, layout_confirmed=True, receipt_dir=Path(folder),
                                             sleep=lambda _: None)
                    self.assertTrue(result["click_sent"])
                    self.assertEqual(result["decision"]["origin"], "random_unreadable")
                    self.assertEqual(session.capture.call_count, 4)
                    choose.assert_called_once()
                    session.click.assert_called_once()
                    provider.answer.assert_not_called()
                    with self.assertRaises(QuizError):
                        answer_once(reader, session, store, provider=provider, random_percentage=0,
                                    execute=True, layout_confirmed=True, receipt_dir=Path(folder),
                                    sleep=lambda _: None)
                    session.click.assert_called_once()
                    provider.answer.assert_not_called()
                finally:
                    store.close()

    def test_random_drift_fallback_rejects_stale_resized_and_changed_option_frames(self):
        first = source()[0].read()
        drift = replace(first, question=replace(first.question, stem="题干识别结果改变"))
        x, y, w, h = first.option_rois["A"]
        yy, xx = np.indices(first.frame.shape[:2])
        changed = np.where(((xx >= x) & (xx < x + w) & (yy >= y) & (yy < y + h))[..., None],
                           255, first.frame).astype(np.uint8)
        cases = {"stale": replace(drift, captured_at=-1),
                 "resized": replace(drift, size=(1660, 932)),
                 "options": replace(drift, frame=changed, question=replace(drift.question,
                                     options=(("A", "完全不同的选项"), *first.question.options[1:])))}
        for name, current in cases.items():
            with self.subTest(name=name):
                reader = NS(read=MagicMock(side_effect=[first, current]))
                session = NS(click=MagicMock())
                provider = MagicMock()
                store = QuizStore(":memory:")
                try:
                    with self.assertRaises(ReadError):
                        answer_once(reader, session, store, provider=provider, execute=True,
                                    layout_confirmed=True, sleep=lambda _: None)
                    session.click.assert_not_called()
                    provider.answer.assert_not_called()
                finally:
                    store.close()

    def test_recovered_question_keeps_preview_identity_and_blocks_another_click(self):
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])
        for preview in (False, True):
            with self.subTest(preview=preview), tempfile.TemporaryDirectory() as folder:
                reader, session = source(unified=True)
                original = session.capture()
                yy, xx = np.indices(original.shape[:2])
                changed = np.where(((xx > 300) & (xx < 340) & (yy > 175) & (yy < 195))[..., None],
                                   0, original).astype(np.uint8)
                session.capture = MagicMock(side_effect=lambda: original if session.capture.call_count == 1 else changed)
                original_ocr = session.ocr
                def ocr(frame, roi, **kwargs):
                    if roi == regions["question"] and session.capture.call_count == 1:
                        return []
                    return original_ocr(frame, roi, **kwargs)
                session.ocr = ocr
                expected = read_for_answer(reader).question.view_token if preview else None
                provider = MagicMock()
                store = QuizStore(":memory:")
                try:
                    options = dict(provider=provider, random_percentage=100, execute=True,
                                   layout_confirmed=True, expected_view_token=expected,
                                   receipt_dir=Path(folder), sleep=lambda _: None)
                    result = answer_once(reader, session, store, **options)
                    self.assertTrue(result["click_sent"])
                    with self.assertRaises(QuizError):
                        answer_once(reader, session, store, **options)
                    session.click.assert_called_once()
                    provider.answer.assert_not_called()
                finally:
                    store.close()

    def test_line_reread_cannot_change_known_numbers_or_negations(self):
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])
        blocks = [replace(FIELDS["question"][1], text="不需要40级"), TextBlock("—", .2, (630, 155, 10, 20))]
        reader, session = source({**FIELDS, "question": blocks})
        original_ocr = session.ocr
        def ocr(frame, roi, **kwargs):
            if kwargs.get("single_line") and roi not in regions.values():
                return [TextBlock("需要4级", .99, roi)]
            return original_ocr(frame, roi, **kwargs)
        session.ocr = ocr
        with self.assertRaises(ReadError):
            reader.read()
        session.click.assert_not_called()

    def test_unknown_question_characters_are_masked_and_never_learned(self):
        for blocks in ([replace(FIELDS["question"][1], text="不需要40级"),
                        TextBlock("マ", .2, (630, 155, 10, 20))],
                       [replace(FIELDS["question"][1], text="不需要40级�")], []):
            with self.subTest(blocks=blocks), tempfile.TemporaryDirectory() as folder:
                reader, session = source({**FIELDS, "question": blocks}, unified=True)
                observed = read_for_answer(reader)
                self.assertIn("*", observed.question.stem)
                self.assertNotIn("マ", observed.question.stem)
                self.assertNotIn("�", observed.question.stem)
                if blocks:
                    self.assertIn("不需要40级", observed.question.stem)
                self.assertFalse(observed.question.is_readable)
                provider = MagicMock()
                store = QuizStore(":memory:")
                try:
                    result = answer_once(reader, session, store, provider=provider, execute=True,
                                         layout_confirmed=True, receipt_dir=Path(folder), sleep=lambda _: None)
                    session.click.assert_called_once()
                    provider.answer.assert_not_called()
                    store.feedback(result["decision"]["attempt_id"], "correct", "离线反馈")
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                    self.assertEqual(store.db.execute("SELECT stem FROM attempts LIMIT 1").fetchone()[0], "")
                finally:
                    store.close()

    def test_restart_ignores_background_noise_but_distinguishes_new_question_text(self):
        for change in ("background", "text"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                first_reader, first_session = source({**FIELDS, "question": []}, unified=True)
                store = QuizStore(Path(folder) / "quiz.sqlite3")
                options = dict(random_percentage=100, execute=True, layout_confirmed=True,
                               receipt_dir=Path(folder), sleep=lambda _: None)
                try:
                    self.assertTrue(answer_once(first_reader, first_session, store, **options)["click_sent"])
                    data = FIELDS if change == "background" else {
                        **FIELDS, "question": [replace(FIELDS["question"][1], text="完全不同的问题？")]}
                    second_reader, second_session = source(data, unified=True)
                    frame = second_session.capture()
                    yy, xx = np.indices(frame.shape[:2])
                    affected = ((xx == 300) & (yy == 190) if change == "background" else
                                (xx > 300) & (xx < 340) & (yy > 155) & (yy < 175))
                    changed = np.where(affected[..., None], 0, frame).astype(np.uint8)
                    second_session.capture = lambda: changed
                    if change == "background":
                        with self.assertRaises(QuizError):
                            answer_once(second_reader, second_session, store, **options)
                        second_session.click.assert_not_called()
                    else:
                        self.assertTrue(answer_once(second_reader, second_session, store, **options)["click_sent"])
                        second_session.click.assert_called_once()
                    first_session.click.assert_called_once()
                finally:
                    store.close()

    def test_submission_fingerprint_keeps_thin_number_strokes_but_removes_isolated_noise(self):
        frame = np.full((466, 830, 3), 255, dtype=np.uint8)
        yy, xx = np.indices(frame.shape[:2])
        one = np.where(((yy == 170) & (xx >= 300) & (xx < 330))[..., None], 0, frame).astype(np.uint8)
        two = np.where(((yy == 176) & (xx >= 300) & (xx < 330))[..., None], 0, one).astype(np.uint8)
        noisy = np.where(((yy == 190) & (xx == 300))[..., None], 0, one).astype(np.uint8)
        roi = Layout.load(ROOT / "config/ring.layout.json").scaled(830, 466)["question"]
        icon_roi = (184, 145, 24, 32)
        original = question_submission_image_token(one, roi, icon_roi)
        self.assertNotEqual(original, question_submission_image_token(two, roi, icon_roi))
        self.assertEqual(original, question_submission_image_token(noisy, roi, icon_roi))

    def test_unreadable_question_supports_two_and_four_options(self):
        regions = Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"])
        for count in (2, 4):
            data = {**FIELDS, "question": []}
            data = {**data, **{f"{kind}_{label}": [] for kind in ("label", "option")
                              for label in "CD" if label not in "ABCD"[:count]}}
            if count == 4:
                data = {**data, "label_D": [TextBlock("D", .99, regions["label_D"])],
                        "option_D": [TextBlock("另一段歌词", .99, regions["option_D"])]}
            reader, session = source(data, unified=True)
            label = "ABCD"[count - 1]
            answer = data[f"option_{label}"][0].text
            with self.subTest(count=count), tempfile.TemporaryDirectory() as folder:
                store = QuizStore(":memory:")
                try:
                    with patch("quiz_core.random.choice", return_value=(label, answer)):
                        result = answer_once(reader, session, store, execute=True, layout_confirmed=True,
                                             receipt_dir=Path(folder), sleep=lambda _: None)
                    self.assertEqual(result["decision"]["label"], label)
                    session.click.assert_called_once()
                finally:
                    store.close()

    def test_unreadable_question_still_requires_option_labels(self):
        reader, session = source({**FIELDS, "question": [], "label_A": []}, unified=True)
        store = QuizStore(":memory:")
        try:
            with self.assertRaises(ReadError):
                answer_once(reader, session, store, random_percentage=100, execute=True,
                            layout_confirmed=True, sleep=lambda _: None)
            session.click.assert_not_called()
        finally:
            store.close()

    def test_unreadable_option_uses_shared_popup_fallback(self):
        reader, session = source({**FIELDS, "option_B": []}, unified=True)
        frame = session.capture().copy()
        for x, y, w, h in Layout.load(ROOT / "config/ring.layout.json").scaled(*REPLAY["size"], group="buttons").values():
            frame[y:y+h, x:x+w] = (220, 240, 250)
        session.capture = lambda: frame
        with tempfile.TemporaryDirectory() as folder:
            store = QuizStore(":memory:")
            try:
                with patch("quiz_core.random.choice", side_effect=lambda options: options[1]):
                    result = answer_once(reader, session, store, random_percentage=100, execute=True,
                                         layout_confirmed=True, sleep=lambda _: None, receipt_dir=Path(folder))
                self.assertEqual((result["decision"]["label"], result["decision"]["origin"]),
                                 ("B", "random_unreadable"))
                session.click.assert_called_once()
            finally:
                store.close()

    def test_masked_question_pixels_do_not_block_random_answer_and_pause_can_stop(self):
        for stop in (False, True):
            reader, session = source({**FIELDS, "question": []}, unified=True)
            original = session.capture()
            yy, xx = np.indices(original.shape[:2])
            changed = np.where(((xx > 300) & (xx < 340) & (yy > 175) & (yy < 195))[..., None],
                               0, original).astype(np.uint8)
            is_waiting = False
            def sleep(seconds):
                nonlocal is_waiting
                if seconds != .25:
                    is_waiting = True
            session.capture = lambda: changed if is_waiting and not stop else original
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as folder:
                store = QuizStore(":memory:")
                try:
                    options = dict(random_percentage=100, execute=True, layout_confirmed=True,
                                   receipt_dir=Path(folder), sleep=sleep, should_stop=lambda: stop and is_waiting)
                    if stop:
                        with self.assertRaises(QuizError):
                            answer_once(reader, session, store, **options)
                        session.click.assert_not_called()
                    else:
                        self.assertTrue(answer_once(reader, session, store, **options)["click_sent"])
                        with self.assertRaises(QuizError):
                            answer_once(reader, session, store, **options)
                        session.click.assert_called_once()
                finally:
                    store.close()

    def test_feedback_ignores_only_decoration_and_checks_the_complete_question_area(self):
        reader, session = source()
        observed = reader.read()
        self.assertEqual(observed.question_roi,
                         Layout.load(ROOT / "config/ring.layout.json").scaled(*observed.size)["question"])
        feedback_frame = observed.frame.copy()
        session.capture = lambda: feedback_frame
        original_ocr = session.ocr
        for changed in (False, True):
            def ocr(image, roi, **kwargs):
                blocks = original_ocr(image, roi, **kwargs)
                x, y, w, h = roi
                if changed and x <= 640 and y <= 180 and x + w >= 652 and y + h >= 195:
                    return [*blocks, TextBlock("不", .99, (640, 180, 12, 15))]
                return blocks
            session.ocr = ocr
            store = MagicMock()
            with self.subTest(changed=changed), patch("quiz_vision.correct_choice", side_effect=lambda frame, box:
                       frame is feedback_frame and box == observed.buttons["A"]):
                result = record_revealed_feedback(session, store, observed, "attempt", "B",
                                                  sleep=lambda _: None, require_active=lambda: None)
                if changed:
                    self.assertEqual(result, "awaiting_manual_feedback")
                    store.learn_revealed_answer.assert_not_called()
                else:
                    self.assertEqual(result, "wrong")
                    store.learn_revealed_answer.assert_called_once_with(
                        observed.question, "只要天边的明月", "同一题目出现唯一正确对勾：选项A。")


if __name__ == "__main__":
    unittest.main()
