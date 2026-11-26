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
from quiz_vision import ReadError, TextBlock
from ring_tasks import MAX_SCROLLS, STALL_SECONDS, RingRunner, daily_tracker_selected, monster_dialog, quiz_visible, ring_tracker
from test_daily_tasks import assert_clicks_in_boxes


def screen(kind="world", count=91, scale=1):
    blocks = []
    if kind in ("world", "world_other", "daily_other", "quiz"):
        blocks = [TextBlock("日常", .99, (695, 82, 30, 18)),
                  TextBlock("活动", .99, (768, 82, 30, 18)),
                  TextBlock(f"200环任务链（{count}/200）", .99, (680, 246, 145, 18))]
    if kind in ("world_other", "daily_other"):
        blocks = [*blocks, TextBlock("其他任务（1/3）", .99, (680, 112, 145, 18))]
    if kind == "daily_other":
        blocks = [b for b in blocks if not b.text.startswith("200环任务链")]
    if kind == "quiz":
        blocks = [*blocks, TextBlock("A", .99, (182, 277, 20, 20)),
                  TextBlock("57秒", .99, (615, 216, 40, 18))]
    if kind == "monster":
        blocks = [TextBlock("夺魄妖王", .993687, (631, 175, 60, 18)),
                  TextBlock("看你往哪里跑。", .999671, (607, 292, 102, 18)),
                  TextBlock("再见", .999819, (643, 340, 37, 20))]
    if kind == "illusion":
        blocks = [TextBlock("斗志幻影", .993712, (631, 175, 60, 18)),
                  TextBlock("切磋一番", .99723, (628, 289, 67, 23)),
                  TextBlock("再见", .999819, (643, 340, 37, 20))]
    if kind in ("failure", "wrong_npc", "other_dialog"):
        blocks = [TextBlock("诗词鬼" if kind != "wrong_npc" else "其他NPC", .99, (480, 294, 49, 18)),
                  TextBlock("咋样，通不过我的考验吧" if kind != "other_dialog" else "答得不错", .99,
                            (71, 335, 173, 19))]
    if kind in ("weekly", "weekly_top", "daily"):
        blocks = [TextBlock("本日经验", .99, (139, 66, 70, 18)),
                  TextBlock("日常", .99, (722, 98, 28, 20)),
                  TextBlock("周月常", .99, (500, 68, 68, 20))]
        if kind == "weekly":
            blocks = [*blocks, TextBlock("200环任务链", .99, (262, 282, 115, 18)),
                      TextBlock("环数：91/200", .99, (312, 310, 82, 14)),
                      TextBlock("已领取：1/1", .99, (312, 328, 76, 14)),
                      TextBlock("前往", .99, (421, 316, 42, 20))]
    frame = np.zeros((470 * scale, 834 * scale, 3), dtype=np.uint8)
    if kind in ("world", "world_other", "daily_other", "quiz"):
        frame[83 * scale:86 * scale, 688 * scale:743 * scale] = (200, 220, 235)
    return DailyView(frame,
                     [TextBlock(b.text, b.score, tuple(v * scale for v in b.box)) for b in blocks])


def events_screen(*, first_ring=False, scale=1):
    current = screen("world" if first_ring else "world_other", scale=scale)
    frame = np.zeros_like(current.frame)
    frame[83 * scale:86 * scale, 760 * scale:815 * scale] = (200, 220, 235)
    return DailyView(frame, [TextBlock(b.text, b.score, tuple(v * scale for v in b.box)) for b in current.blocks])


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

    def test_illusion_has_its_own_exact_action_and_rejects_crossed_pair(self):
        for scale in (1, 2):
            self.assertEqual(monster_dialog(screen("illusion", scale=scale)), (628, 289, 67, 23))
        crossed = screen("illusion")
        crossed.blocks = [TextBlock("看你往哪里跑。", b.score, b.box) if b.text == "切磋一番" else b
                          for b in crossed.blocks]
        with self.assertRaises(QuizError):
            monster_dialog(crossed)

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

    def test_current_count_does_not_depend_on_fixed_total_or_closing_bracket(self):
        for text in ("200环任务链(180/", "200环任务链(180/2", "200环任务链(180/20",
                     "200环任务链(180/200", "200环任务链(180/200)"):
            current = screen()
            current.blocks = [TextBlock(text, .99, (680, 112, 145, 18))]
            with self.subTest(text=text):
                self.assertEqual(ring_tracker(current), (180, (680, 112, 145, 18)))

    def test_hidden_or_ambiguous_current_count_is_not_guessed(self):
        for text in ("200环任务链", "200环任务链(", "200环任务链(18", "200环任务链(/200)",
                     "200环任务链(1?0/200)", "200环任务链143/200"):
            current = screen()
            current.blocks = [TextBlock(text, .99, (680, 112, 145, 18))]
            with self.subTest(text=text):
                self.assertIsNone(ring_tracker(current)[0])

    def test_invalid_counts_fail_closed_and_missing_is_not_completion(self):
        for count in (0, 201, "0180"):
            with self.assertRaisesRegex(QuizError, "进度无法确认"):
                ring_tracker(screen(count=count))
        self.assertIsNone(ring_tracker(screen("battle")))
        self.assertEqual(ring_tracker(screen(count=200))[0], 200)

    def test_daily_tab_selection_uses_calibrated_background_not_title_text_alone(self):
        for scale in (1, 2):
            self.assertTrue(daily_tracker_selected(screen(scale=scale)))
            self.assertFalse(daily_tracker_selected(events_screen(scale=scale)))
        ambiguous = screen()
        ambiguous.frame = np.zeros_like(ambiguous.frame)
        self.assertIsNone(daily_tracker_selected(ambiguous))

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

    def test_illusion_start_reuses_battle_flow_and_clicks_once(self):
        self.feed("illusion", "illusion", "world", "world", "battle")
        self.runner.answer_quiz = MagicMock()
        with patch.object(self.runner, "open_ring") as activity:
            self.runner.run()
        activity.assert_not_called()
        self.runner.answer_quiz.assert_not_called()
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], (628, 289, 67, 23))

    def test_illusion_changed_npc_during_pause_prevents_battle_click(self):
        self.feed("monster")
        with self.assertRaisesRegex(QuizError, "对话在等待期间变化"):
            self.runner.start_monster_battle(screen("illusion"))
        self.session.click.assert_not_called()

    def test_illusion_open_dialog_is_not_clicked_twice(self):
        self.feed("illusion")
        with self.assertRaisesRegex(QuizError, "未重复点击"):
            self.runner.start_monster_battle(screen("illusion"))
        self.session.click.assert_called_once()

    def test_illusion_stop_during_pause_does_not_click(self):
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.runner.start_monster_battle(screen("illusion"))
        self.session.click.assert_not_called()

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
        self.feed("world_other", "weekly_top", "weekly", "weekly", "world", "world", "world", "battle")
        self.runner.run()
        boxes = [(8, 108, 42, 42), (500, 68, 68, 20), (421, 316, 42, 20)]
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], *boxes)
        self.session.swipe.assert_not_called()

    def test_start_selects_daily_before_first_ring_or_activity_fallback(self):
        for first_ring in (True, False):
            with self.subTest(first_ring=first_ring):
                frames = iter((events_screen(first_ring=first_ring), events_screen(first_ring=first_ring),
                               screen("world" if first_ring else "world_other"), screen(), screen(), screen("battle")))
                self.runner.read = lambda: next(frames, screen("battle"))
                self.session.click.reset_mock()
                with patch.object(self.runner, "open_ring", return_value=screen()) as activity:
                    self.runner.run()
                clicks = [call.args for call in self.session.click.call_args_list]
                if first_ring:
                    activity.assert_not_called()
                    assert_clicks_in_boxes(self, clicks, (695, 82, 30, 18), (680, 246, 145, 18))
                else:
                    activity.assert_called_once()
                    assert_clicks_in_boxes(self, clicks, (695, 82, 30, 18))

    def test_daily_tab_switch_rechecks_page_and_waits_for_selection_once(self):
        self.runner.on_quiz_retry = MagicMock()
        for fresh in (screen("quiz"), screen("monster"), screen("failure"), screen()):
            with patch.object(self.runner, "read", return_value=fresh):
                self.runner.select_daily_tracker(events_screen())
        self.session.click.assert_not_called()
        self.feed("world", "world")
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.runner.select_daily_tracker(events_screen())
        self.session.click.assert_not_called()

    def test_ambiguous_tab_or_switch_timeout_never_clicks_other_task(self):
        ambiguous = screen()
        ambiguous.frame = np.zeros_like(ambiguous.frame)
        with self.assertRaisesRegex(QuizError, "页签"):
            self.runner.select_daily_tracker(ambiguous)
        self.session.click.assert_not_called()
        self.runner.read = lambda: events_screen()
        with self.assertRaisesRegex(QuizError, "未重复点击"):
            self.runner.select_daily_tracker(events_screen())
        self.session.click.assert_called_once()

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

    def test_ring_progress_advance_during_click_pause_is_not_an_error(self):
        self.feed(("world", 172))
        initial = screen(count=171)
        current, clicked = self.runner.tap_first_ring(initial, ring_tracker(initial))
        self.assertFalse(clicked)
        self.assertEqual(ring_tracker(current)[0], 172)
        self.session.click.assert_not_called()

    def test_running_ring_continues_when_game_advances_during_recheck(self):
        self.feed(("world", 171), ("world", 171), ("world", 172), ("quiz", 172))
        def answer():
            self.runner.should_stop = lambda: True
            return {"click_sent": True}
        self.runner.answer_quiz = MagicMock(side_effect=answer)
        result = self.runner.run()
        self.assertEqual(result["reason"], "stopped")
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_quiz_appearing_during_recheck_is_answered_without_tracker_click(self):
        self.feed("world", "world", "quiz")
        def answer():
            self.runner.should_stop = lambda: True
            return {"click_sent": True}
        self.runner.answer_quiz = MagicMock(side_effect=answer)
        self.assertEqual(self.runner.run()["reason"], "stopped")
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_recheck_still_rejects_regression_and_resolution_change(self):
        for fresh in (screen(count=90), screen(scale=2)):
            with self.subTest():
                with patch.object(self.runner, "read", return_value=fresh), self.assertRaises(QuizError):
                    self.runner.tap_first_ring(screen(), ring_tracker(screen()))
        self.session.click.assert_not_called()

    def test_pathfinding_start_during_recheck_skips_only_current_click(self):
        fresh = screen()
        fresh.blocks = [*fresh.blocks, TextBlock("自动寻路中…", .99, (330, 370, 120, 20))]
        with patch.object(self.runner, "read", return_value=fresh):
            _, clicked = self.runner.tap_first_ring(screen(), ring_tracker(screen()))
        self.assertFalse(clicked)
        self.session.click.assert_not_called()

    def test_changed_first_item_quiz_or_stopped_during_pause_has_no_click(self):
        for kind in ("world_other", "quiz", "battle"):
            with self.subTest(kind=kind):
                self.feed(kind)
                current, clicked = self.runner.tap_first_ring(screen(), ring_tracker(screen()))
                self.assertFalse(clicked)
                self.assertIsNotNone(current)
        self.session.click.assert_not_called()
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.runner.tap_first_ring(screen(), ring_tracker(screen()))
        self.session.click.assert_not_called()

    def test_progress_changes_reset_stall_timer_without_new_clicks(self):
        def read():
            count = 91 + int((self.now - started) // 150)
            self.runner.view = screen(count=count)
            return self.runner.view
        started = self.now
        self.runner.read = read
        self.runner.should_stop = lambda: self.now - started >= 460
        self.assertEqual(self.runner.run()["reason"], "stopped")
        self.session.click.assert_called_once()
        self.runner.save.assert_not_called()

    def test_suspected_purchase_or_team_stall_warns_without_extra_input(self):
        self.feed("world")
        result = self.runner.run()
        self.assertEqual(result["reason"], "needs_attention")
        self.assertIn("技能书购买或组队", result["message"])
        self.session.click.assert_called_once()
        self.runner.save.assert_called_once()

    def test_occluded_live_ring180_does_not_abort_monitoring_or_guess_count(self):
        hidden = screen(count=180)
        hidden.blocks = [TextBlock("200环任务链（180/", .944834,
                                   (683.88, 110.7111, 104.77125, 14.1)),
                         TextBlock("打败蟠桃凤凰", .982341, (684.40125, 130.0333, 71.9325, 12.53333))]
        frames = iter((screen(count=179), screen(count=179), screen(count=179), hidden, screen("quiz", count=180)))
        self.runner.read = lambda: next(frames)
        def answer():
            self.runner.should_stop = lambda: True
            return {"click_sent": True}
        self.runner.answer_quiz = MagicMock(side_effect=answer)
        result = self.runner.run()
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual(result["progress"], 180)
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_called_once()

    def test_unreadable_current_count_at_start_waits_without_opening_activity(self):
        hidden = screen(count=180)
        hidden.blocks = [TextBlock("200环任务链（18", .99, b.box)
                         if b.text.startswith("200环") else b for b in hidden.blocks]
        frames = iter((hidden, hidden, screen("quiz", count=180)))
        self.runner.read = lambda: next(frames)
        self.runner.open_ring = MagicMock(side_effect=AssertionError("遮挡时不应打开活动"))
        def answer():
            self.runner.should_stop = lambda: True
            return {"click_sent": True}
        self.runner.answer_quiz = MagicMock(side_effect=answer)
        result = self.runner.run()
        self.assertEqual(result, {"reason": "stopped", "progress": 180})
        self.runner.open_ring.assert_not_called()
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_long_occlusion_does_not_count_as_confirmed_stall_and_remains_stoppable(self):
        hidden = screen(count=180)
        hidden.blocks = [TextBlock("200环任务链（", .99, b.box)
                         if b.text.startswith("200环") else b for b in hidden.blocks]
        started = self.now
        self.runner.read = lambda: hidden
        self.runner.should_stop = lambda: self.now - started >= 400
        result = self.runner.run()
        self.assertEqual(result, {"reason": "stopped", "progress": None})
        self.runner.save.assert_not_called()
        self.session.click.assert_not_called()

    def test_clear_count_after_occlusion_gets_new_stall_window_without_extra_entry(self):
        hidden = screen()
        hidden.blocks = [TextBlock("200环任务链（9", .99, b.box)
                         if b.text.startswith("200环") else b for b in hidden.blocks]
        started = self.now
        def read():
            return hidden if 10 <= self.now - started < 220 else screen()
        self.runner.read = read
        result = self.runner.run()
        self.assertEqual(result["reason"], "needs_attention")
        self.assertEqual(result["progress"], 91)
        self.assertGreaterEqual(self.now - started, 390)
        self.session.click.assert_called_once()

    def test_occluded_title_after_activity_entry_is_waited_on_without_another_click(self):
        hidden = screen()
        hidden.blocks = [TextBlock("200环任务链（9", .99, b.box)
                         if b.text.startswith("200环") else b for b in hidden.blocks]
        started = self.now
        self.runner.should_stop = lambda: self.now - started >= 15
        frames = iter((screen("weekly"), hidden))
        self.runner.read = lambda: next(frames, hidden)
        self.runner.open_ring(screen("weekly"))
        self.assertEqual(self.runner.run(), {"reason": "stopped", "progress": None})
        self.session.click.assert_called_once()

    def test_unreadable_tracker_cannot_be_used_by_click_entry_directly(self):
        hidden = screen()
        hidden.blocks = [TextBlock("200环任务链（9", .99, b.box)
                         if b.text.startswith("200环") else b for b in hidden.blocks]
        current, clicked = self.runner.tap_first_ring(hidden, ring_tracker(hidden))
        self.assertIs(current, hidden)
        self.assertFalse(clicked)
        self.session.click.assert_not_called()

    def test_occlusion_during_click_recheck_skips_input_and_reaches_quiz(self):
        hidden = screen()
        hidden.blocks = [TextBlock("200环任务链（9", .99, b.box)
                         if b.text.startswith("200环") else b for b in hidden.blocks]
        frames = iter((screen(), screen(), hidden, screen("quiz")))
        self.runner.read = lambda: next(frames)
        def answer():
            self.runner.should_stop = lambda: True
            return {"click_sent": True}
        self.runner.answer_quiz = MagicMock(side_effect=answer)
        self.assertEqual(self.runner.run()["reason"], "stopped")
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_normal_ring_progress_never_reclicks_tracker_after_entry(self):
        self.feed("world", "world", "world", "battle", "battle", ("world", 92), ("world", 92), ("world", 92))
        result = self.runner.run()
        self.assertEqual(result["reason"], "needs_attention")
        self.assertEqual(result["progress"], 92)
        self.assertEqual(self.session.click.call_count, 1)
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

    def test_ring200_completes_immediately_without_input_or_manual_check(self):
        for kind in ("world", "quiz"):
            with self.subTest(kind=kind):
                self.feed((kind, 200))
                self.runner.answer_quiz = MagicMock()
                result = self.runner.run()
                self.assertEqual(result["reason"], "completed")
                self.assertEqual(result["progress"], 200)
                self.assertIn("已完成", result["message"])
                self.runner.answer_quiz.assert_not_called()
                self.runner.save.assert_not_called()
        self.session.click.assert_not_called()
        self.session.swipe.assert_not_called()

    def test_ring_advances_to200_then_completes_without_another_entry_click(self):
        self.feed(("world", 199), ("world", 199), ("world", 199), ("world", 200))
        result = self.runner.run()
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["progress"], 200)
        self.session.click.assert_called_once()
        self.runner.save.assert_not_called()

    def test_ring200_during_click_recheck_completes_without_old_target_input(self):
        self.feed(("world", 199), ("world", 199), ("world", 200))
        result = self.runner.run()
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["progress"], 200)
        self.session.click.assert_not_called()
        self.runner.save.assert_not_called()

    def test_daily_tracker_disappearing_after_ring199_completes_without_more_input(self):
        self.feed(("world", 199), ("world", 199), ("world", 199), "daily_other", "daily_other")
        result = self.runner.run()
        self.assertEqual((result["reason"], result["progress"]), ("completed", 200))
        self.session.click.assert_called_once()
        self.runner.save.assert_not_called()

    def test_temporary_disappearance_or_wrong_tab_after_ring199_is_not_completion(self):
        frames = iter((screen(count=199), screen(count=199), screen(count=199), screen("daily_other"),
                       events_screen(), events_screen(), screen("battle")))
        self.runner.read = lambda: next(frames, screen("battle"))
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.session.click.assert_called_once()

    def test_missing_tracker_is_not_reported_completed(self):
        self.feed("world", "battle")
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.session.click.assert_not_called()

    def test_weekly_bottom_card_uses_only_its_go_button(self):
        self.feed("weekly", "world")
        self.runner.open_ring(screen("weekly"))
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], (421, 316, 42, 20))
        self.session.swipe.assert_not_called()

    def test_world_opens_activity_then_weekly_then_ring(self):
        self.feed("weekly_top", "weekly", "weekly", "world")
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

    def test_roi_ring_click_continues_polling_and_reaches_quiz_without_old_view(self):
        captures = 0
        stopped = False
        def capture():
            nonlocal captures
            captures += 1
            frame = screen().frame.copy()
            frame[0, 0, 0] = 1 if captures > 3 else 0
            return frame
        def ocr(frame, roi, **kwargs):
            return screen("quiz" if frame[0, 0, 0] else "world").blocks
        runner = RingRunner(self.session, Path(self.tmp.name), lambda: stopped,
                            vision_options=VisionOptions(mode="roi"))
        runner.sleep = self.runner.sleep
        runner.save = MagicMock()
        runner.save_metrics = MagicMock()
        self.session.capture.side_effect = capture
        self.session.ocr.side_effect = ocr
        def answer():
            nonlocal stopped
            stopped = True
            return {"click_sent": True}
        runner.answer_quiz = MagicMock(side_effect=answer)
        result = runner.run()
        self.assertEqual(result["reason"], "stopped")
        runner.answer_quiz.assert_called_once()
        self.session.click.assert_called_once()
        self.assertEqual(captures, 4)

    def test_quiz_calls_answer_handler_then_waits_without_restarting_same_ring(self):
        self.feed("quiz", "world")
        self.runner.answer_quiz = MagicMock(return_value={"click_sent": True})
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.runner.answer_quiz.assert_called_once_with()
        self.session.click.assert_not_called()

    def test_transient_quiz_validation_failure_recaptures_without_tracker_click(self):
        self.feed("quiz", "quiz", ("world", 200))
        self.runner.answer_quiz = MagicMock(side_effect=[ReadError("截图已过期"), {"click_sent": True}])
        with patch.object(self.runner, "sleep", wraps=self.runner.sleep) as wait, \
                patch("ring_tasks.random.uniform", return_value=1.5) as pause:
            self.assertEqual(self.runner.run()["reason"], "completed")
        pause.assert_called_once_with(1.0, 2.0)
        wait.assert_called_once_with(1.5)
        self.assertEqual(self.runner.answer_quiz.call_count, 2)
        self.session.click.assert_not_called()
        self.session.swipe.assert_not_called()

    def test_persistent_quiz_validation_failure_times_out_without_resetting_deadline(self):
        self.feed("quiz")
        started = self.now
        limit = int(STALL_SECONDS / 1.5) + 1
        def fail():
            self.assertLessEqual(self.runner.answer_quiz.call_count, limit)
            raise ReadError("Question/layout changed or screenshot is stale; no click performed.")
        self.runner.answer_quiz = MagicMock(side_effect=fail)
        with patch("ring_tasks.random.uniform", return_value=1.5), \
                self.assertRaisesRegex(QuizError, "答题.*超时"):
            self.runner.run()
        self.assertEqual(self.runner.answer_quiz.call_count, limit)
        self.assertEqual(self.now - started, STALL_SECONDS)
        self.session.click.assert_not_called()

    def test_quiz_validation_retry_wait_remains_stoppable(self):
        self.feed("quiz")
        self.runner.answer_quiz = MagicMock(side_effect=ReadError("题目变化"))
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        self.assertEqual(self.runner.run()["reason"], "stopped")
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_quiz_validation_failure_returns_changed_page_to_monitoring(self):
        self.feed("quiz", ("world", 200))
        self.runner.answer_quiz = MagicMock(side_effect=ReadError("题目变化"))
        self.assertEqual(self.runner.run()["reason"], "completed")
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_quiz_submission_errors_and_open_submitted_popup_do_not_retry(self):
        self.feed("quiz")
        self.runner.answer_quiz = MagicMock(side_effect=QuizError("Submission guard rejected this view"))
        with self.assertRaisesRegex(QuizError, "Submission guard rejected"):
            self.runner.run()
        self.runner.answer_quiz.assert_called_once()
        self.runner.answer_quiz = MagicMock(return_value={"click_sent": True})
        with self.assertRaisesRegex(QuizError, "未重复提交"):
            self.runner.run()
        self.runner.answer_quiz.assert_called_once()
        self.session.click.assert_not_called()

    def test_failed_quiz_closes_dialog_once_and_answers_again(self):
        self.feed("quiz", "failure", "failure", "quiz", "battle")
        self.runner.answer_quiz = MagicMock(return_value={"click_sent": True})
        self.runner.on_quiz_retry = MagicMock()
        self.assertEqual(self.runner.run()["reason"], "needs_attention")
        self.assertEqual(self.runner.answer_quiz.call_count, 2)
        self.runner.on_quiz_retry.assert_called_once_with()
        assert_clicks_in_boxes(self, [self.session.click.call_args.args], (260, 395, 290, 55))

    def test_failure_returns_to_world_resumes_task_once_before_next_quiz(self):
        self.feed("quiz", "failure", "failure", "world", "world", "world", "quiz", "battle")
        self.runner.answer_quiz = MagicMock(return_value={"click_sent": True})
        self.runner.run()
        self.assertEqual(self.runner.answer_quiz.call_count, 2)
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list],
                               (260, 395, 290, 55), (680, 246, 145, 18))

    def test_failure_dialog_at_start_is_closed_before_answering_or_opening_activity(self):
        self.feed("failure", "failure", "quiz", "battle")
        self.runner.answer_quiz = MagicMock(return_value={"click_sent": True})
        self.runner.on_quiz_retry = MagicMock()
        with patch.object(self.runner, "open_ring") as activity:
            self.runner.run()
        activity.assert_not_called()
        self.runner.answer_quiz.assert_called_once()
        self.runner.on_quiz_retry.assert_called_once()
        self.session.click.assert_called_once()

    def test_failure_dismissal_rechecks_same_dialog_and_keeps_stop_protection(self):
        for kind in ("quiz", "world", "wrong_npc"):
            with self.subTest(kind=kind):
                self.feed(kind)
                view = self.runner.dismiss_quiz_failure(screen("failure"))
                self.assertIsNotNone(view)
        self.session.click.assert_not_called()
        self.runner.sleep = lambda _: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.runner.dismiss_quiz_failure(screen("failure"))
        self.session.click.assert_not_called()

    def test_failure_dismissal_ignores_speech_change_during_pause(self):
        self.feed("other_dialog", "quiz")
        self.runner.on_quiz_retry = MagicMock()
        self.runner.dismiss_quiz_failure(screen("failure"))
        self.session.click.assert_called_once()
        self.runner.on_quiz_retry.assert_called_once_with()

    def test_failure_dialog_that_stays_open_is_not_clicked_repeatedly_or_rearmed(self):
        self.feed("failure")
        self.runner.on_quiz_retry = MagicMock()
        with self.assertRaisesRegex(QuizError, "未重复点击"):
            self.runner.dismiss_quiz_failure(screen("failure"))
        self.session.click.assert_called_once()
        self.runner.on_quiz_retry.assert_not_called()

    def test_failure_dismissal_has_variable_stoppable_pause_and_random_interior_points(self):
        points, pauses = [], []
        for _ in range(12):
            self.feed("failure", "quiz")
            def sleep(seconds):
                pauses.append(seconds)
                self.now += seconds
            self.runner.sleep = sleep
            self.runner.dismiss_quiz_failure(screen("failure"))
            points.append(self.session.click.call_args.args)
        self.assertTrue(all(.3 <= pause <= 1 for pause in pauses))
        self.assertGreater(len(set(pauses)), 1)
        self.assertGreater(len(set(points)), 1)
        assert_clicks_in_boxes(self, points, *[(260, 395, 290, 55)] * 12)

    def test_ring_entry_recheck_does_not_click_event_tab_content(self):
        with patch.object(self.runner, "read", return_value=events_screen(first_ring=True)):
            _, clicked = self.runner.tap_first_ring(screen(), ring_tracker(screen()))
        self.assertFalse(clicked)
        self.session.click.assert_not_called()

    def test_failed_quiz_dismissal_rejects_stale_image_and_resolution_change(self):
        self.feed("failure")
        stale = screen("failure")
        stale.captured_at = self.now - 11
        with self.assertRaises(QuizError):
            self.runner.dismiss_quiz_failure(stale)
        with patch.object(self.runner, "read", return_value=screen("failure", scale=2)), self.assertRaises(QuizError):
            self.runner.dismiss_quiz_failure(screen("failure"))
        self.session.click.assert_not_called()

    def test_no_answer_pauses_without_tracker_or_choice_click(self):
        self.feed("quiz")
        self.runner.answer_quiz = lambda: {"click_sent": False, "decision": {"reason": "未命中题库"}}
        with self.assertRaisesRegex(QuizError, "未命中题库"):
            self.runner.run()
        self.session.click.assert_not_called()

    def test_ring_click_refresh_accepts_current_count_with_hidden_total(self):
        initial = screen(count=143)
        fresh = screen(count=143)
        fresh.blocks = [TextBlock("200环任务链（143/", .972119,
                                 (683.88, 110.7111, 104.77125, 13.57778)),
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
