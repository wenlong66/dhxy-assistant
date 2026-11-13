"""Deterministic vision tests; no model, device, account, network or API key."""
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from daily_tasks import DailyRunner, DailyView, LocalDailyView, TABLE
from local_vision import RegionOCR, VisionMetrics, VisionOptions, color_fraction, scaled_roi
from quiz_core import QuizError
from quiz_vision import TextBlock
from test_daily_tasks import Clock, FIXTURES, ReplaySession, view


class RegionCacheTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((470, 834, 3), dtype=np.uint8)
        self.frame[10:30, 10:40] = (10, 90, 200)
        self.blocks = [TextBlock("测试", .99, (10, 10, 30, 20))]
        self.session = SimpleNamespace(ocr=Mock(return_value=self.blocks))
        self.metrics = VisionMetrics()
        self.cache = RegionOCR(self.session, VisionOptions(), self.metrics)
        self.now = 100.0
        self.clock_patch = patch("local_vision.time.monotonic", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.roi = (0, 0, 50, 40)

    def read(self):
        return self.cache.recognize(self.frame, self.roi, self.now)

    def test_identical_new_frame_reuses_confident_ocr(self):
        self.read()
        self.now += .2
        self.frame = self.frame.copy()
        self.assertEqual(self.read(), self.blocks)
        self.session.ocr.assert_called_once()
        self.assertEqual(self.metrics.counts["ocr_cache_hits"], 1)

    def test_even_one_pixel_change_forces_ocr(self):
        self.read()
        self.frame[20, 20, 0] += 1
        self.read()
        self.assertEqual(self.session.ocr.call_count, 2)

    def test_change_outside_region_does_not_force_ocr(self):
        self.read()
        self.frame[300, 300] = 100
        self.read()
        self.assertEqual(self.session.ocr.call_count, 1)

    def test_ttl_is_not_extended_by_cache_hits(self):
        self.read()
        for _ in range(4):
            self.now += .2
            self.read()
        self.now += .3
        self.read()
        self.assertEqual(self.session.ocr.call_count, 2)

    def test_cache_off_and_manual_invalidation(self):
        self.read()
        self.cache.invalidate()
        self.read()
        self.cache.options = VisionOptions(cache_seconds=0)
        self.read()
        self.assertEqual(self.session.ocr.call_count, 3)

    def test_clock_backwards_and_geometry_change_invalidate(self):
        self.read()
        self.now -= 1
        self.read()
        self.frame = np.pad(self.frame, ((0, 1), (0, 1), (0, 0)))
        self.read()
        self.assertEqual(self.session.ocr.call_count, 3)

    def test_empty_low_score_and_uniform_frames_are_not_cached(self):
        for blocks, uniform in (([], False), ([TextBlock("测试", .5, (1, 1, 3, 3))], False),
                                (self.blocks, True)):
            with self.subTest(blocks=blocks, uniform=uniform):
                self.cache.invalidate()
                self.session.ocr.reset_mock()
                self.session.ocr.return_value = blocks
                if uniform:
                    self.frame[:] = 0
                self.read()
                self.read()
                self.assertEqual(self.session.ocr.call_count, 2)

    def test_low_confidence_text_is_returned_not_silently_discarded(self):
        self.session.ocr.return_value = [TextBlock("不", .2, (1, 1, 3, 3))]
        self.assertEqual(self.read()[0].text, "不")
        self.assertEqual(self.read()[0].score, .2)

    def test_capture_start_age_is_used_not_recognition_finish(self):
        self.cache.recognize(self.frame, self.roi, self.now - 5)
        self.read()
        self.assertEqual(self.session.ocr.call_count, 2)

    def test_invalid_roi_is_rejected_before_ocr(self):
        for roi in ((-1, 0, 10, 10), (0, 0, 0, 10), (800, 0, 100, 10)):
            with self.subTest(roi=roi), self.assertRaises(QuizError):
                self.cache.recognize(self.frame, roi, self.now)
        self.session.ocr.assert_not_called()

    def test_ocr_failure_is_measured_but_never_cached(self):
        self.session.ocr.side_effect = QuizError("offline error")
        for _ in range(2):
            with self.assertRaises(QuizError):
                self.read()
        self.assertEqual(self.metrics.counts["ocr_calls"], 2)
        self.assertFalse(self.cache.entries)

    def test_stop_is_checked_on_cache_hits_too(self):
        self.read()
        self.cache.check_stop = Mock(side_effect=QuizError("stopped"))
        with self.assertRaisesRegex(QuizError, "stopped"):
            self.read()
        self.session.ocr.assert_called_once()

    def test_lru_is_bounded(self):
        for i in range(50):
            self.cache.recognize(self.frame, (0, 0, 50 + i, 40), self.now)
        self.assertEqual(len(self.cache.entries), 32)

    def test_metrics_are_bounded_and_contain_no_text(self):
        for _ in range(600):
            self.metrics.duration("test", time.perf_counter())
        report = self.metrics.report()
        self.assertEqual(report["timings"]["test"]["calls"], 600)
        self.assertEqual(len(self.metrics.samples["test"]), 512)
        self.assertNotIn("测试", json.dumps(report, ensure_ascii=False))


class LocalDailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frame = np.zeros((470, 834, 3), dtype=np.uint8)
        blocks = [TextBlock(b["text"], b["score"], tuple(b["box"])) for b in FIXTURES["initial"]]
        self.session = SimpleNamespace(capture=Mock(return_value=self.frame),
                                       ocr=Mock(return_value=blocks), click=Mock())
        self.runner = DailyRunner(self.session, Path(self.tmp.name), lambda: False)

    def test_progress_reuses_frame_without_extra_capture(self):
        progress = []
        self.runner.on_result = progress.append
        current = self.runner.read()
        self.runner.emit("first")
        self.runner.emit("second")
        self.assertIs(progress[0]["frame"], current.frame)
        self.assertNotIn("frame", progress[1])
        self.session.capture.assert_called_once()

    def test_read_is_capture_only_until_region_is_requested(self):
        current = self.runner.read()
        self.session.capture.assert_called_once()
        self.session.ocr.assert_not_called()
        self.assertIsInstance(current, LocalDailyView)
        self.assertTrue(current.sweep_panel)
        self.assertEqual(self.session.ocr.call_count, 2)
        self.assertTrue(current.sweep_panel)
        self.assertEqual(self.session.ocr.call_count, 2)
        self.session.capture.assert_called_once()

    def test_nested_card_reads_share_one_table_recognition(self):
        current = self.runner.read()
        actual = current.cards()
        self.assertEqual(actual, view("initial").cards())
        # Two scene anchors, one popup guard, and the table; not per-card OCR.
        self.assertEqual(self.session.ocr.call_count, 4)
        self.assertLess(self.runner.metrics.counts["ocr_pixels"], 834 * 470 * .5)
        self.assertTrue(all(call.args[1] != (0, 0, 834, 470) for call in self.session.ocr.call_args_list))

    def test_full_mode_remains_available_for_comparison(self):
        runner = DailyRunner(self.session, Path(self.tmp.name), lambda: False,
                             vision_options=VisionOptions(mode="full"))
        current = runner.read()
        self.assertNotIsInstance(current, LocalDailyView)
        self.assertEqual(current.cards(), view("initial").cards())
        self.assertEqual(self.session.ocr.call_args.args[1], (0, 0, 834, 470))

    def test_roi_results_keep_full_image_coordinates_at_double_scale(self):
        large = view("initial", 2)
        self.session.capture.return_value = large.frame
        self.session.ocr.return_value = [TextBlock(b.text, b.score, tuple(v * 2 for v in b.box))
                                         for b in large.blocks]
        current = self.runner.read()
        self.assertEqual(current.cards(), view("initial").cards())
        for call in self.session.ocr.call_args_list:
            x, y, w, h = call.args[1]
            self.assertLessEqual(x + w, 1668)
            self.assertLessEqual(y + h, 940)

    def test_old_view_is_invalid_after_click_even_if_input_fails(self):
        current = self.runner.read()
        self.assertTrue(current.sweep_panel)
        self.session.click.side_effect = QuizError("delivery unknown")
        with self.assertRaisesRegex(QuizError, "delivery unknown"):
            self.runner.tap(current, (10, 10, 20, 20))
        with self.assertRaises(QuizError):
            self.runner.tap(current, (10, 10, 20, 20))
        with self.assertRaises(QuizError):
            current.text(TABLE)
        self.assertFalse(self.runner.reader.entries)
        self.session.click.assert_called_once()

    def test_valid_click_requires_a_new_frame_for_next_click(self):
        current = self.runner.read()
        self.runner.tap(current, (10, 10, 20, 20))
        with self.assertRaises(QuizError):
            self.runner.tap(current, (10, 10, 20, 20))
        self.runner.tap(self.runner.read(), (10, 10, 20, 20))
        self.assertEqual(self.session.click.call_count, 2)

    def test_invalid_click_bounds_are_rejected(self):
        for box in ((-1, 0, 10, 10), (800, 450, 100, 100), (0, 0, 0, 10), None):
            with self.subTest(box=box), self.assertRaises(QuizError):
                self.runner.tap(self.runner.read(), box)
        self.session.click.assert_not_called()

    def test_capture_time_counts_towards_staleness(self):
        clock = Clock()
        def capture():
            clock.now += 11
            return self.frame
        self.session.capture.side_effect = capture
        with patch("daily_tasks.time.monotonic", clock.monotonic):
            current = self.runner.read()
            with self.assertRaisesRegex(QuizError, "过期"):
                self.runner.tap(current, (10, 10, 20, 20))
        self.session.click.assert_not_called()

    def test_diagnostics_mark_partial_ocr_and_record_regions(self):
        current = self.runner.read()
        self.assertTrue(current.sweep_panel)
        self.runner.save()
        data = json.loads((self.runner.folder / "ocr.json").read_text())
        self.assertTrue(data["partial_ocr"])
        self.assertEqual(len(data["regions"]), 2)
        perf = json.loads((self.runner.folder / "performance.json").read_text())
        self.assertEqual(perf["capture_scope"], "full_device_frame")
        self.assertEqual(perf["counters"]["frames"], 1)
        self.assertEqual(perf["counters"]["ocr_calls"], 2)

    def test_stop_still_writes_metrics_without_input(self):
        self.runner.should_stop = lambda: True
        self.assertEqual(self.runner.run(["帮派任务"])["reason"], "stopped")
        self.assertTrue((self.runner.folder / "performance.json").exists())
        self.session.click.assert_not_called()

    def test_wrong_aspect_is_rejected_before_ocr(self):
        self.session.capture.return_value = np.zeros((470, 1000, 3), dtype=np.uint8)
        with self.assertRaises(QuizError):
            self.runner.read()
        self.session.ocr.assert_not_called()

    def test_early_completion_is_hint_then_list_verification(self):
        clock = Clock()
        session = ReplaySession(clock)
        session.active, session.scene, session.finish_at = "guild", "active", 6
        runner = DailyRunner(session, Path(self.tmp.name), lambda: False)
        with patch("daily_tasks.time.monotonic", clock.monotonic), patch("daily_tasks.time.sleep", clock.sleep):
            runner.wait_task("帮派任务")
        self.assertLess(clock.now, 60)
        self.assertFalse(any(scene == "dialog" for scene, _, _ in session.clicks))

    def test_missing_tracker_alone_never_triggers_early_completion(self):
        clock = Clock()
        runner = DailyRunner(self.session, Path(self.tmp.name), lambda: False)
        with patch("daily_tasks.time.monotonic", clock.monotonic), patch("daily_tasks.time.sleep", clock.sleep), \
             patch.object(runner, "read", side_effect=lambda: view("five_end")), \
             patch.object(runner, "stable_list", side_effect=lambda: (view("finished"), view("finished").cards())):
            runner.wait_task("五环任务")
        self.assertGreaterEqual(clock.now, 180)
        self.session.click.assert_not_called()

    def test_treasure_keeps_fixed_five_minutes_despite_early_probe(self):
        clock = Clock()
        runner = DailyRunner(self.session, Path(self.tmp.name), lambda: False,
                             vision_options=VisionOptions(early_probe=True))
        finished = view("finished")
        with patch("daily_tasks.time.monotonic", clock.monotonic), patch("daily_tasks.time.sleep", clock.sleep), \
             patch.object(runner, "read", return_value=view("school_end")) as read, \
             patch.object(runner, "stable_list", return_value=(finished, finished.cards())):
            runner.wait_task("宝图任务")
        self.assertEqual(clock.now, 300)
        read.assert_not_called()
        self.session.click.assert_not_called()


class GeometryAndConfigTests(unittest.TestCase):
    def test_roi_padding_is_clamped_not_stretched(self):
        self.assertEqual(scaled_roi((0, 0, 10, 10), 1668, 940, padding=4), (0, 0, 28, 28))
        self.assertEqual(scaled_roi((824, 460, 10, 10), 834, 470, padding=4), (820, 456, 14, 14))
        with self.assertRaises(QuizError):
            scaled_roi((800, 460, 100, 10), 834, 470)

    def test_color_range_uses_bgr_and_pixel_fraction(self):
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        frame[:5] = (180, 200, 50)
        self.assertEqual(color_fraction(frame, (0, 0, 10, 10), (170, 190, 40), (190, 210, 60)), .5)
        with self.assertRaises(QuizError):
            color_fraction(frame, (0, 0, 10, 10), (256, 0, 0), (255, 255, 255))

    def test_invalid_options_rejected(self):
        for options in ({"mode": "typo"}, {"cache_seconds": -1}, {"cache_seconds": float("nan")},
                        {"poll_seconds": 0}, {"probe_seconds": 0}, {"early_probe": "yes"}):
            with self.subTest(options=options), self.assertRaises(QuizError):
                VisionOptions(**options)

    def test_environment_can_disable_cache_and_early_probe(self):
        with patch.dict(os.environ, {"DHXY_VISION_MODE": "full", "DHXY_CACHE_SECONDS": "0", "DHXY_EARLY_PROBE": "0"}):
            opts = VisionOptions.from_env()
        self.assertEqual(opts.mode, "full")
        self.assertFalse(opts.early_probe)
        self.assertEqual(opts.cache_seconds, 0)


@unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
class GuiFrameReuseTests(unittest.TestCase):
    def test_daily_progress_does_not_capture_an_extra_frame(self):
        import gui_app
        done = threading.Event()
        events = []
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        def emit(kind, value):
            events.append((kind, value))
            if kind in {"done", "error"}:
                done.set()
        class Backend:
            session = None
            def auto_daily(self, **kwargs):
                kwargs["on_result"]({"message": "progress", "frame": frame})
                return {"reason": "completed"}
            def capture(self):
                raise AssertionError("Progress must reuse the supplied frame")
            def shutdown(self):
                pass
        worker = gui_app.DeviceWorker(SimpleNamespace(message=SimpleNamespace(emit=emit)), backend_factory=Backend)
        try:
            worker.preview_enabled = True
            self.assertTrue(worker.submit("auto_daily", selected=["帮派任务"]))
            self.assertTrue(done.wait(3))
        finally:
            worker.shutdown()
            worker.thread.join(3)
        self.assertFalse(any(kind == "error" for kind, _ in events), events)
        self.assertIn(("daily_progress", {"message": "progress"}), events)
        self.assertTrue(any(kind == "preview" and payload["png"].startswith(b"\x89PNG") for kind, payload in events))


if __name__ == "__main__":
    unittest.main()
