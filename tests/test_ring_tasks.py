"""Synthetic calibrated screen replays; never connect, purchase or call AI."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from daily_tasks import DailyStopped, DailyView
from local_vision import VisionOptions
from quiz_core import QuizError
from quiz_vision import TextBlock
from ring_tasks import MAX_SCROLLS, RingRunner, monster_dialog, quiz_visible, ring_tracker
from test_daily_tasks import assert_clicks_in_boxes


def screen(kind="world", count=91, scale=1):
    blocks = []
    if kind in ("world", "world_other", "quiz"):
        blocks = [TextBlock("日常", .99, (695, 82, 30, 18)),
                  TextBlock("活动", .99, (768, 82, 30, 18)),
                  TextBlock(f"200环任务链（{count}/200）", .99, (680, 246, 145, 18))]
    if kind == "world_other":
        blocks = [*blocks, TextBlock("其他任务（1/3）", .99, (680, 112, 145, 18))]
    if kind == "quiz":
        blocks = [*blocks, TextBlock("A", .99, (182, 277, 20, 20)),
                  TextBlock("57秒", .99, (615, 216, 40, 18))]
    if kind == "monster":
        blocks = [TextBlock("夺魄妖王", .993687, (631, 175, 60, 18)),
                  TextBlock("看你往哪里跑。", .999671, (607, 292, 102, 18)),
                  TextBlock("再见", .999819, (643, 340, 37, 20))]
    if kind in ("weekly", "weekly_top", "daily"):
        blocks = [TextBlock("本日经验", .99, (139, 66, 70, 18)),
                  TextBlock("日常", .99, (722, 98, 28, 20)),
                  TextBlock("周月常", .99, (500, 68, 68, 20))]
        if kind == "weekly":
            blocks = [*blocks, TextBlock("200环任务链", .99, (262, 282, 115, 18)),
                      TextBlock("前往", .99, (421, 316, 42, 20))]
    return DailyView(np.zeros((470 * scale, 834 * scale, 3), dtype=np.uint8),
                     [TextBlock(b.text, b.score, tuple(v * scale for v in b.box)) for b in blocks])


class RingParsingTests(unittest.TestCase):
    def test_monster_dialog_requires_known_npc_and_both_exact_options(self):
        self.assertEqual(monster_dialog(screen("monster")), (607, 292, 102, 18))
        self.assertEqual(monster_dialog(screen("monster", scale=2)), (607, 292, 102, 18))
        unknown = screen("monster")
        unknown.blocks = [TextBlock("其他NPC", b.score, b.box) if b.text == "夺魄妖王" else b
                          for b in unknown.blocks]
        self.assertIsNone(monster_dialog(unknown))
        for missing in ("看你往哪里跑。", "再见"):
            current = screen("monster")
            current.blocks = [b for b in current.blocks if b.text != missing]
            with self.subTest(missing=missing), self.assertRaises(QuizError):
                monster_dialog(current)

    def test_tracker_is_below_original_daily_roi_and_scales(self):
        for scale in (1, 2):
            self.assertEqual(ring_tracker(screen(scale=scale)), (91, (680, 246, 145, 18)))

    def test_only_first_tracker_item_can_be_used_for_direct_entry(self):
        current = screen()
        current.blocks = [*current.blocks, TextBlock("其他任务（1/3）", .99, (680, 112, 145, 18))]
        self.assertIsNone(ring_tracker(current))
        current.blocks = [b for b in current.blocks if not b.text.startswith("其他任务")]
        self.assertEqual(ring_tracker(current)[0], 91)

    def test_split_title_and_count_are_joined(self):
        current = screen()
        current.blocks = [TextBlock("200环任务链", .99, (680, 247, 90, 18)),
                          TextBlock("（91/200）", .99, (771, 246, 54, 18))]
        self.assertEqual(ring_tracker(current)[0], 91)

    def test_live_ring143_missing_closing_bracket_does_not_abort_tracking(self):
        current = screen()
        current.blocks = [TextBlock("200环任务链（143/200", .972119,
                                   (683.88, 110.7111, 126.66375, 13.57778)),
                          TextBlock("回复千里眼", .984577, (683.88, 128.4667, 61.5075, 14.1))]
        count, box = ring_tracker(current)
        self.assertEqual(count, 143)
        for actual, expected in zip(box, current.blocks[0].box):
            self.assertAlmostEqual(actual, expected)

    def test_malformed_or_partial_counts_remain_rejected(self):
        for text in ("200环任务链(143/20", "200环任务链(143/2000", "200环任务链(0/200",
                     "200环任务链(201/200", "200环任务链(143/200)其他", "200环任务链143/200"):
            current = screen()
            current.blocks = [TextBlock(text, .99, (680, 112, 145, 18))]
            with self.subTest(text=text), self.assertRaises(QuizError):
                ring_tracker(current)

    def test_invalid_counts_fail_closed_and_missing_is_not_completion(self):
        for count in (0, 201):
            with self.assertRaisesRegex(QuizError, "进度无法确认"):
                ring_tracker(screen(count=count))
        self.assertIsNone(ring_tracker(screen("battle")))
        self.assertEqual(ring_tracker(screen(count=200))[0], 200)

    def test_quiz_detection_catches_partial_options(self):
        self.assertTrue(quiz_visible(screen("quiz")))
        self.assertFalse(quiz_visible(screen()))


class RingRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = MagicMock()
        self.runner = RingRunner(self.session, Path(self.tmp.name), lambda: False,
                                 vision_options=VisionOptions(mode="full"))
        self.now = time.monotonic()
        self.monotonic = patch("time.monotonic", side_effect=lambda: self.now)
        self.monotonic.start()
        self.addCleanup(self.monotonic.stop)
        self.runner.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        self.runner.save = MagicMock()
        self.runner.save_metrics = MagicMock()

    def feed(self, *scenes):
        remaining = iter(scenes)
        last = scenes[-1]
        def read():
            nonlocal last
            last = next(remaining, last)
            kind, count = last if isinstance(last, tuple) else (last, 91)
            self.runner.view = screen(kind, count)
            return self.runner.view
        self.runner.read = read

    def test_monster_start_clicks_challenge_once_without_activity_or_quiz(self):
        self.feed("monster", "monster", "world", "world", "battle")
        self.runner.answer_quiz = MagicMock()
        with patch.object(self.runner, "open_ring") as activity:
            self.runner.run()
        activity.assert_not_called()
        self.runner.answer_quiz.assert_not_called()
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], (607, 292, 102, 18))
        self.session.swipe.assert_not_called()

    def test_monster_relocates_after_pause_and_uses_only_new_button(self):
        current = screen("monster", scale=2)
        current.blocks = [TextBlock(b.text, b.score, (560, 305, b.box[2], b.box[3]))
                          if b.text == "看你往哪里跑。" else b for b in current.blocks]
        with patch.object(self.runner, "read", side_effect=[current, screen("battle", scale=2)]), \
                patch("ring_tasks.random.uniform", return_value=.7) as delay:
            self.runner.start_monster_battle(screen("monster", scale=2))
        delay.assert_called_once_with(.3, 1.0)
        assert_clicks_in_boxes(self, [self.session.click.call_args.args], (1120, 610, 204, 36))

    def test_monster_dialog_stays_open_does_not_retry(self):
        self.feed("monster")
        with self.assertRaisesRegex(QuizError, "未重复点击"):
            self.runner.start_monster_battle(screen("monster"))
        self.session.click.assert_called_once()

    def test_changed_dialog_or_stop_before_battle_has_no_input(self):
        self.feed("world")
        with self.assertRaisesRegex(QuizError, "对话在等待期间变化"):
            self.runner.start_monster_battle(screen("monster"))
        self.session.click.assert_not_called()
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.runner.start_monster_battle(screen("monster"))
        self.session.click.assert_not_called()

    def test_first_ring_enters_directly_without_activity_navigation(self):
        self.feed("world", "world", "world", "battle")
        with patch.object(self.runner, "open_ring") as activity, patch("ring_tasks.random.uniform", return_value=.6) as delay:
            self.runner.run()
        activity.assert_not_called()
        delay.assert_called_once_with(.3, 1.0)
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], (680, 246, 145, 18))
        self.session.swipe.assert_not_called()

    def test_first_other_task_falls_back_to_activity_even_if_ring_is_lower(self):
        self.feed("world_other", "weekly_top", "weekly", "world", "world", "world", "battle")
        self.runner.run()
        boxes = [(8, 108, 42, 42), (500, 68, 68, 20), (421, 316, 42, 20), (680, 246, 145, 18)]
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], *boxes)
        self.session.swipe.assert_not_called()

    def test_first_ring_click_relocates_after_pause(self):
        initial = screen(scale=2)
        fresh = screen(scale=2)
        fresh.blocks = [TextBlock(b.text, b.score, (b.box[0], 120, b.box[2], b.box[3]))
                        if b.text.startswith("200环") else b for b in fresh.blocks]
        with patch.object(self.runner, "read", return_value=fresh) as read, \
                patch("ring_tasks.random.uniform", return_value=.4):
            self.runner.tap_first_ring(initial, ring_tracker(initial))
        read.assert_called_once()
        assert_clicks_in_boxes(self, [self.session.click.call_args.args], (1360, 240, 290, 36))

    def test_changed_first_item_quiz_or_stopped_during_pause_has_no_click(self):
        for kind in ("world_other", "quiz", "battle"):
            with self.subTest(kind=kind):
                self.feed(kind)
                with self.assertRaisesRegex(QuizError, "页面已变化"):
                    self.runner.tap_first_ring(screen(), ring_tracker(screen()))
        self.session.click.assert_not_called()
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.runner.tap_first_ring(screen(), ring_tracker(screen()))
        self.session.click.assert_not_called()

    def test_waits_in_battle_and_clicks_each_new_ring_only_once(self):
        self.feed("world", "world", "world", "battle", "battle", ("world", 92), ("world", 92), ("world", 92))
        result = self.runner.run()
        self.assertEqual(result["reason"], "needs_attention")
        self.assertEqual(result["progress"], 92)
        self.assertEqual(self.session.click.call_count, 2)
        self.session.swipe.assert_not_called()

    def test_pathfinding_never_receives_tracker_click(self):
        def read():
            current = screen()
            current.blocks = [*current.blocks, TextBlock("自动寻路中…", .99, (330, 370, 120, 20))]
            self.runner.view = current
            return current
        self.runner.read = read
        self.runner.run()
        self.session.click.assert_not_called()

    def test_final_ring_or_missing_tracker_is_not_reported_completed(self):
        for scenes in ((("world", 200), "battle"), ("world", "battle")):
            with self.subTest(scenes=scenes):
                self.feed(*scenes)
                self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.session.click.assert_not_called()

    def test_weekly_bottom_card_uses_only_its_go_button(self):
        self.feed("world")
        self.runner.open_ring(screen("weekly"))
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], (421, 316, 42, 20))
        self.session.swipe.assert_not_called()

    def test_world_opens_activity_then_weekly_then_ring(self):
        self.feed("weekly_top", "weekly", "world")
        self.runner.open_ring(screen("world"))
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list],
                               (8, 108, 42, 42), (500, 68, 68, 20), (421, 316, 42, 20))
        self.session.swipe.assert_not_called()

    def test_weekly_scroll_is_bounded_and_never_claims_completion(self):
        self.feed("weekly_top")
        with self.assertRaisesRegex(QuizError, "仍未找到"):
            self.runner.open_ring(screen("weekly_top"))
        self.assertEqual(self.session.swipe.call_count, MAX_SCROLLS)
        self.assertEqual(self.session.swipe.call_args.args, (600, 325, 600, 140, 400))
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], (500, 68, 68, 20))

    def test_scroll_stale_reused_or_wrong_scene_is_rejected(self):
        current = screen("weekly_top", scale=2)
        self.runner.scroll(current)
        self.session.swipe.assert_called_once_with(1200, 650, 1200, 280, 400)
        with self.assertRaises(QuizError):
            self.runner.scroll(current)
        stale = screen("weekly_top")
        stale.captured_at = self.now - 9.9
        with self.assertRaises(QuizError):
            self.runner.scroll(stale)
        with self.assertRaises(QuizError):
            self.runner.scroll(screen("world"))
        self.assertEqual(self.session.swipe.call_count, 1)

    def test_quiz_calls_answer_handler_then_waits_without_restarting_same_ring(self):
        self.feed("quiz", "world")
        self.runner.answer_quiz = MagicMock(return_value={"click_sent": True})
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.runner.answer_quiz.assert_called_once_with()
        self.session.click.assert_not_called()

    def test_no_answer_pauses_without_tracker_or_choice_click(self):
        self.feed("quiz")
        self.runner.answer_quiz = lambda: {"click_sent": False, "decision": {"reason": "未命中题库"}}
        with self.assertRaisesRegex(QuizError, "未命中题库"):
            self.runner.run()
        self.session.click.assert_not_called()

    def test_ring_click_refresh_tolerates_only_missing_closing_bracket(self):
        initial = screen(count=143)
        fresh = screen(count=143)
        fresh.blocks = [TextBlock("200环任务链（143/200", .972119,
                                 (683.88, 110.7111, 126.66375, 13.57778)),
                        *[b for b in fresh.blocks if not b.text.startswith("200环")]]
        with patch.object(self.runner, "read", return_value=fresh):
            self.runner.tap_first_ring(initial, ring_tracker(initial))
        assert_clicks_in_boxes(self, [self.session.click.call_args.args], fresh.blocks[0].box)

    def test_progress_regression_is_not_clicked(self):
        self.feed("world", ("world", 90))
        with self.assertRaisesRegex(QuizError, "进度倒退"):
            self.runner.run()
        self.session.click.assert_not_called()

    def test_cancellation_before_input_reaches_runner(self):
        self.feed("world", "world")
        self.runner.on_result = lambda result: setattr(self.runner, "should_stop", lambda: True)
        self.assertEqual(self.runner.run()["reason"], "stopped")
        self.session.click.assert_not_called()
        self.session.swipe.assert_not_called()


class SwipeAdapterTests(unittest.TestCase):
    def setUp(self):
        from maa_adapter import MaaSession
        self.session = MaaSession.__new__(MaaSession)
        self.session.execute = True
        self.session.cfg = {"timeout_seconds": 1}
        self.session.controller = MagicMock()
        self.session.controller.post_swipe.return_value = MagicMock(done=True, succeeded=True)

    def test_swipe_requires_explicit_input_permission(self):
        self.session.execute = False
        with self.assertRaises(QuizError):
            self.session.swipe(600, 325, 600, 140, 400)
        self.session.controller.post_swipe.assert_not_called()

    def test_valid_swipe_uses_five_native_arguments(self):
        self.session.swipe(600, 325, 600, 140, 400)
        self.session.controller.post_swipe.assert_called_once_with(600, 325, 600, 140, 400)

    def test_invalid_swipe_or_native_failure_does_not_retry(self):
        for values in ((-1, 1, 2, 3, 400), (0, 1, 2, 3, 0), (0, 1, 2, 3, True)):
            with self.assertRaises(QuizError):
                self.session.swipe(*values)
        self.session.controller.post_swipe.assert_not_called()
        self.session.controller.post_swipe.return_value.succeeded = False
        with self.assertRaises(QuizError):
            self.session.swipe(600, 325, 600, 140, 400)
        self.session.controller.post_swipe.assert_called_once()


class RingBackendTests(unittest.TestCase):
    def test_backend_passes_quiz_policy_and_always_disables_input(self):
        import gui_backend
        with tempfile.TemporaryDirectory() as folder:
            backend = gui_backend.DeviceBackend()
            backend.session = MagicMock(execute=False)
            backend.cfg = {"runtime_dir": Path(folder), "database": Path(folder) / "quiz.sqlite3",
                           "account_scope": "offline-account", "layout": Path(gui_backend.ROOT) / "config/dali.layout.json"}
            def run():
                callback = runner.call_args.kwargs["answer_quiz"]
                self.assertTrue(backend.session.execute)
                self.assertTrue(callback()["click_sent"])
                return {"reason": "stopped"}
            with patch.object(gui_backend, "RingRunner") as runner, \
                    patch.object(gui_backend, "UnifiedQuizReader") as reader, \
                    patch.object(gui_backend, "answer_once", return_value={"click_sent": True}) as answer, \
                    patch.object(gui_backend, "OpenAIProvider") as provider:
                runner.return_value.run.side_effect = run
                result = backend.auto_ring(should_stop=lambda: False, random_answer=True,
                                           use_ai=True, model="", allow_trial=True)
                self.assertEqual(result, {"reason": "stopped"})
                reader.assert_called_once_with(backend.session, unittest.mock.ANY, "offline-account")
                self.assertEqual(answer.call_args.kwargs["random_percentage"], 100)
                self.assertIsNone(answer.call_args.kwargs["provider"])
                provider.assert_not_called()
                self.assertFalse(backend.session.execute)
                runner.return_value.run.side_effect = QuizError("offline failure")
                with self.assertRaises(QuizError):
                    backend.auto_ring(should_stop=lambda: False)
                self.assertFalse(backend.session.execute)


if __name__ == "__main__":
    unittest.main()
