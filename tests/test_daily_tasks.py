"""Replay sanitized Maa OCR from the user's screenshots; never connect to a device."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from daily_tasks import DailyRunner, DailyView, PICKUPS, TOTALS, WAIT_SECONDS, choose_task
from gui_backend import DeviceBackend
from quiz_core import QuizError
from quiz_vision import TextBlock

FIXTURES = json.loads((Path(__file__).parent / "fixtures/daily_ocr.json").read_text(encoding="utf-8"))


def view(name, scale=1):
    frame = np.zeros((470 * scale, 834 * scale, 3), dtype=np.uint8)
    blocks = [TextBlock(b["text"], b["score"], tuple(v * scale for v in b["box"])) for b in FIXTURES[name]]
    return DailyView(frame, blocks)


class DailyParsingTests(unittest.TestCase):
    def test_live_completed_guild_ocr_without_day_prefix(self):
        with patch.dict("daily_tasks.TOTALS", {"帮派任务": 20}, clear=True):
            card = view("guild_count_without_day").cards()[0]
        self.assertTrue(card.done)
        self.assertEqual(card.current, 20)

    def test_no_pickup_button_completes_even_without_readable_count(self):
        current = view("guild_count_without_day")
        current.blocks = [b for b in current.blocks if b.text != "次数：20/20"]
        with patch.dict("daily_tasks.TOTALS", {"帮派任务": 20}, clear=True):
            cards = current.cards()
        guild = cards[0]
        self.assertTrue(guild.done)
        self.assertIsNone(guild.current)
        self.assertIsNone(choose_task(cards, {"帮派任务"}))

    def test_real_ocr_priority_reordering_and_new_broom(self):
        expected = {"initial": "卓越竞技场", "after_arena": "帮派任务",
                    "after_guild": "五环任务", "new_broom": "宝图任务", "finished": None}
        for name, task in expected.items():
            for scale in (1, 2):
                with self.subTest(frame=name, scale=scale):
                    chosen = choose_task(view(name, scale).cards(), set(TOTALS))
                    self.assertEqual(chosen.name if chosen else None, task)

    def test_completed_arena_with_go_button_is_skipped(self):
        arena = next(c for c in view("finished").cards() if c.name == "卓越竞技场")
        self.assertTrue(arena.done)
        self.assertIsNotNone(arena.button)
        self.assertIsNone(choose_task(view("finished").cards(), {"卓越竞技场"}))

    def test_unselected_tasks_are_never_chosen(self):
        self.assertEqual(choose_task(view("initial").cards(), {"五环任务"}).name, "五环任务")

    def test_unreadable_counts_do_not_block_a_recognized_pickup_button(self):
        for replacement in ("次数未识别", "日次数：0/15", "日次数：21/20"):
            current = view("after_arena")
            current.blocks = [TextBlock(replacement, b.score, b.box) if b.text == "日次数：0/20" else b
                              for b in current.blocks]
            guild = next(c for c in current.cards() if c.name == "帮派任务")
            self.assertFalse(guild.done)
            self.assertIsNone(guild.current)
            self.assertEqual(guild.action, "前往")

    def test_missing_task_title_is_not_completion(self):
        current = view("finished")
        current.blocks = [b for b in current.blocks if b.text != "师门任务"]
        with self.assertRaises(QuizError):
            current.cards()

    def test_visible_teal_button_with_missed_ocr_is_not_completion(self):
        current = view("after_arena")
        current.blocks = [b for b in current.blocks if not (b.text == "前注" and b.box[0] < 480 and b.box[1] < 130)]
        current.frame[90:125, 420:465] = (180, 200, 50)
        with self.assertRaisesRegex(QuizError, "按钮无法确认"):
            current.cards()

    def test_partially_visible_card_is_not_completion(self):
        current = view("finished")
        current.blocks = [TextBlock(b.text, b.score, (b.box[0], 310, b.box[2], b.box[3]))
                          if b.text == "师门任务" else b for b in current.blocks]
        with self.assertRaisesRegex(QuizError, "未完整显示"):
            current.cards()

    def test_real_dialogs_progress_and_popup(self):
        for name, frame in (("帮派任务", "guild_dialog"), ("五环任务", "five_dialog"), ("师门任务", "school_dialog")):
            npc, option = PICKUPS[name]
            current = view(frame)
            self.assertIn(npc, current.text((530, 160, 280, 300)))
            self.assertIsNotNone(current.find(option, (530, 160, 280, 300)))
        for name, frame in (("帮派任务", "guild_active"), ("五环任务", "five_active"), ("师门任务", "school_active")):
            self.assertEqual(view(frame).tracking(), name)
        self.assertEqual(view("treasure_active").tracking(), "宝图任务")
        treasure = view("treasure_dialog")
        self.assertNotEqual(treasure.find("宝图任务", (530, 160, 280, 300)),
                            treasure.find("宝图任务（文牒通关）", (530, 160, 280, 300)))
        self.assertTrue(view("dig_popup").dig_popup)
        with self.assertRaises(QuizError):
            view("dig_popup").cards()
        for frame in ("guild_end", "five_end", "school_end"):
            self.assertIsNone(view(frame).tracking())

    def test_wrong_aspect_ratio_is_rejected(self):
        with self.assertRaises(QuizError):
            DailyView(np.zeros((470, 700, 3), dtype=np.uint8), [])


class Clock:
    now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)


class ReplaySession:
    def __init__(self, clock):
        self.clock = clock
        self.scene = "list"
        self.list_name = "initial"
        self.active = None
        self.finish_at = None
        self.clicks = []
        self.execute = True

    def current_view(self):
        if self.scene == "active" and self.clock.now >= self.finish_at:
            self.scene = "end"
            self.list_name = {"guild": "after_guild", "five": "new_broom", "school": "finished"}[self.active]
        name = self.list_name if self.scene == "list" else (
            f"{self.active}_{self.scene}" if self.scene in ("dialog", "active", "end") else self.scene)
        if name == "school_ready":
            current = view("finished")
            current.blocks = [TextBlock("日次数：0/20", b.score, b.box)
                              if b.text == "日次数：20/20" and b.box[0] > 480 and 160 < b.box[1] < 190
                              else b for b in current.blocks]
            current.blocks.append(TextBlock("前注", 0.99, (653, 180, 46, 18)))
            return current
        return view(name)

    def capture(self):
        return self.current_view().frame

    def ocr(self, frame, roi):
        return self.current_view().blocks

    def click(self, x, y):
        if not self.execute:
            raise AssertionError("Input was disabled")
        self.clicks.append((self.scene, x, y))
        current = self.current_view()
        if self.scene == "list":
            if 710 <= x <= 738 and 42 <= y <= 70:
                self.scene = "active"
                return
            candidates = [c for c in current.cards() if c.button and c.button[0] <= x <= c.button[0] + c.button[2]
                          and c.button[1] <= y <= c.button[1] + c.button[3]]
            if len(candidates) != 1:
                raise AssertionError(f"Unexpected task click: {x}, {y}")
            card = candidates[0]
            if card.name == "卓越竞技场":
                self.list_name = "after_arena"
            elif card.name == "宝图任务":
                self.list_name = "school_ready"
                self.scene = "dig_popup"
            else:
                self.active = {"帮派任务": "guild", "五环任务": "five", "师门任务": "school"}[card.name]
                self.scene = "dialog"
        elif self.scene == "dialog":
            option = PICKUPS[{"guild": "帮派任务", "five": "五环任务", "school": "师门任务"}[self.active]][1]
            box = current.find(option, (530, 160, 280, 300))
            if not (box[0] <= x <= box[0] + box[2] and box[1] <= y <= box[1] + box[3]):
                raise AssertionError("Wrong NPC option")
            self.scene = "active"
            self.finish_at = self.clock.now + WAIT_SECONDS[{"guild": "帮派任务", "five": "五环任务", "school": "师门任务"}[self.active]]
        elif self.scene == "dig_popup":
            if not (250 < x < 410 and 280 < y < 330):
                raise AssertionError("Must cancel rather than confirm digging")
            self.scene = "list"
        elif self.scene == "end" and self.active != "five":
            self.scene = "five_end"
        elif self.scene in ("five_end", "end"):
            if not (8 <= x <= 50 and 108 <= y <= 150):
                raise AssertionError("Wrong activity entry")
            self.scene = "activity"
        elif self.scene == "activity":
            if not (713 <= x <= 753 and 405 <= y <= 435):
                raise AssertionError("Wrong broom entry")
            self.scene = "list"
        else:
            raise AssertionError(f"Unexpected input during {self.scene}")


class DailyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.session = ReplaySession(self.clock)
        self.runner = DailyRunner(self.session, Path(self.tmp.name), lambda: False)
        self.patches = [patch("daily_tasks.time.monotonic", self.clock.monotonic),
                        patch("daily_tasks.time.sleep", self.clock.sleep)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_full_screenshot_flow_new_broom_popup_and_completion(self):
        result = self.runner.run(list(TOTALS))
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(set(result["completed"]), set(TOTALS))
        self.assertEqual(result["pending"], [])
        self.assertGreaterEqual(self.clock.now, sum(WAIT_SECONDS[name] for name in ("帮派任务", "五环任务", "师门任务")))
        self.assertEqual(sum(scene == "dialog" for scene, _, _ in self.session.clicks), 3)
        self.assertEqual(sum(scene == "dig_popup" for scene, _, _ in self.session.clicks), 1)

    def test_open_activity_and_broom_from_main_screenshot(self):
        for scale in (1, 2):
            with self.subTest(scale=scale), patch.object(
                self.runner, "read", side_effect=[view("main_entry", scale), view("weekly_activity", scale), view("initial", scale)]
            ), patch.object(self.session, "click") as click:
                result = self.runner.open_list()
                self.assertTrue(result.sweep_panel)
                self.assertEqual(click.call_args_list, [unittest.mock.call(29 * scale, 129 * scale),
                                                       unittest.mock.call(733 * scale, 420 * scale)])

    def test_weekly_activity_goes_directly_to_broom(self):
        self.assertTrue(view("weekly_activity").activity_panel)
        with patch.object(self.runner, "read", side_effect=[view("weekly_activity"), view("initial")]), \
                patch.object(self.session, "click") as click:
            self.assertTrue(self.runner.open_list().sweep_panel)
        click.assert_called_once_with(733, 420)

    def test_existing_daily_list_does_not_click_entries_again(self):
        with patch.object(self.runner, "read", return_value=view("initial")), \
                patch.object(self.session, "click") as click:
            self.assertTrue(self.runner.open_list().sweep_panel)
        click.assert_not_called()

    def test_activity_entry_is_retried_after_first_click_does_not_open(self):
        clicks = []
        def current():
            if len(clicks) < 2:
                return view("main_entry")
            return view("weekly_activity") if len(clicks) == 2 else view("initial")
        with patch.object(self.runner, "read", side_effect=current), \
                patch.object(self.session, "click", side_effect=lambda x, y: clicks.append((x, y))):
            self.assertTrue(self.runner.open_list().sweep_panel)
        self.assertEqual(clicks, [(29, 129), (29, 129), (733, 420)])

    def test_broom_entry_is_retried_and_stops_after_three_attempts(self):
        with patch.object(self.runner, "read", side_effect=lambda: view("weekly_activity")), \
                patch.object(self.session, "click") as click:
            with self.assertRaisesRegex(QuizError, "重试 3 次"):
                self.runner.open_list()
        self.assertEqual(click.call_count, 3)

    def test_late_activity_open_is_not_clicked_closed_again(self):
        clicks = []
        def current():
            if not clicks or self.clock.now < 5:
                return view("main_entry")
            return view("weekly_activity") if len(clicks) == 1 else view("initial")
        with patch.object(self.runner, "read", side_effect=current), \
                patch.object(self.session, "click", side_effect=lambda x, y: clicks.append((x, y))):
            self.assertTrue(self.runner.open_list().sweep_panel)
        self.assertEqual(clicks, [(29, 129), (733, 420)])

    def test_reward_toasts_are_retried_before_reading_clean_list(self):
        frames = iter([view("guild_count_without_day"), view("post_arena_clean"), view("post_arena_clean")])
        with patch.object(self.runner, "open_list", return_value=next(frames)), \
                patch.object(self.runner, "read", side_effect=lambda: next(frames)):
            _, cards = self.runner.stable_list()
        self.assertTrue(next(c for c in cards if c.name == "帮派任务").done)
        self.assertEqual(choose_task(cards, set(TOTALS)).name, "师门任务")
        self.assertEqual(self.session.clicks, [])

    def test_cancellation_from_progress_prevents_sweep_click(self):
        stopped = []
        self.runner.should_stop = lambda: bool(stopped)
        self.runner.on_result = lambda result: stopped.append(True)
        self.assertEqual(self.runner.run(list(TOTALS))["reason"], "stopped")
        self.assertEqual(self.session.clicks, [])

    def test_already_completed_has_no_clicks(self):
        self.session.list_name = "finished"
        self.assertEqual(self.runner.run(list(TOTALS))["reason"], "completed")
        self.assertEqual(self.session.clicks, [])

    def test_normal_treasure_pickup_checks_completion_after_fixed_five_minutes(self):
        dialog = view("treasure_dialog")
        dialog.blocks = [b for b in dialog.blocks if "文牒" not in b.text]
        checks = []
        def list_view():
            checks.append(self.clock.now)
            current = view("after_arena" if self.clock.now < 300 else "finished")
            return current, current.cards()
        with patch.object(self.runner, "read", return_value=view("after_arena")), \
                patch.object(self.runner, "stable_list", side_effect=list_view), \
                patch.object(self.runner, "until", side_effect=[dialog, view("treasure_active")]), \
                patch.object(self.runner, "tap") as tap:
            result = self.runner.run(["宝图任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(checks, [0, 300, 300])
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(tap.call_args.args[1], dialog.find("宝图任务", (530, 160, 280, 300)))

    def test_treasure_document_has_priority_and_verifies_without_five_minute_wait(self):
        initial, finished = view("after_arena"), view("finished")
        dialog = view("treasure_dialog")
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", side_effect=[(initial, initial.cards()),
                    (finished, finished.cards()), (finished, finished.cards())]), \
                patch.object(self.runner, "until", return_value=dialog), \
                patch.object(self.runner, "tap") as tap:
            result = self.runner.run(["宝图任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertLess(self.clock.now, 1)
        self.assertEqual(tap.call_count, 2)
        self.assertEqual(tap.call_args.args[1], dialog.find("宝图任务（文牒通关）", (530, 160, 280, 300)))

    def test_treasure_document_without_completion_does_not_repeat(self):
        initial = view("after_arena")
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", return_value=(initial, initial.cards())), \
                patch.object(self.runner, "until", return_value=view("treasure_dialog")), \
                patch.object(self.runner, "tap") as tap:
            with self.assertRaisesRegex(QuizError, "文牒通关后未确认"):
                self.runner.run(["宝图任务"])
        self.assertEqual(tap.call_count, 2)

    def test_active_treasure_waits_five_minutes_without_picking_up_again(self):
        finished = view("finished")
        with patch.object(self.runner, "read", return_value=view("treasure_active")), \
                patch.object(self.runner, "stable_list", return_value=(finished, finished.cards())), \
                patch.object(self.runner, "tap") as tap:
            result = self.runner.run(["宝图任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(self.clock.now, 300)
        tap.assert_not_called()

    def test_no_broom_does_not_claim_arena_completed(self):
        self.session.list_name = "after_arena"
        original = self.session.current_view
        def incomplete():
            current = original()
            current.blocks = [TextBlock("日次数：0/3", b.score, b.box) if b.text == "日次数：3/3" else b for b in current.blocks]
            return current
        self.session.current_view = incomplete
        result = self.runner.run(["卓越竞技场"])
        self.assertEqual(result["reason"], "needs_attention")
        self.assertEqual(result["pending"], ["卓越竞技场"])
        self.assertEqual(self.session.clicks, [])

    def test_wait_timeout_never_picks_next_task_or_repeats_pickup(self):
        self.session.list_name = "after_arena"
        # Keep the list incomplete after pickup despite the estimate and grace period.
        def stuck_list():
            self.session.scene = "list"
            return self.session.current_view()
        self.runner.open_list = stuck_list
        with self.assertRaisesRegex(QuizError, "额外 5 分钟"):
            self.runner.run(["帮派任务", "五环任务"])
        self.assertEqual(sum(scene == "dialog" for scene, _, _ in self.session.clicks), 1)
        self.assertEqual(self.session.active, "guild")

    def test_backend_disables_input_even_on_error(self):
        backend = DeviceBackend()
        backend.session = self.session
        backend.cfg = {"runtime_dir": Path(self.tmp.name)}
        with patch("gui_backend.DailyRunner.run", side_effect=QuizError("failure")):
            with self.assertRaises(QuizError):
                backend.auto_daily(selected=list(TOTALS), should_stop=lambda: False)
        self.assertFalse(self.session.execute)

    def test_cancellation_during_wait_has_no_later_click(self):
        self.runner.should_stop = lambda: self.clock.now >= 1
        self.session.list_name = "after_arena"
        result = self.runner.run(["帮派任务"])
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual([scene for scene, _, _ in self.session.clicks], ["list", "dialog"])

    def test_active_task_is_resumed_without_receiving_again(self):
        self.session.active = "five"
        self.session.scene = "active"
        self.session.finish_at = WAIT_SECONDS["五环任务"]
        result = self.runner.run(["五环任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertFalse(any(scene == "dialog" for scene, _, _ in self.session.clicks))

    def test_unknown_scene_and_stale_frame_never_click(self):
        with patch.object(self.runner, "read", return_value=view("guild_dialog")):
            with self.assertRaisesRegex(QuizError, "主界面"):
                self.runner.open_list()
        current = view("after_arena")
        self.clock.now += 11
        with self.assertRaisesRegex(QuizError, "过期"):
            self.runner.tap(current, (0, 0, 1, 1))
        self.assertEqual(self.session.clicks, [])


if __name__ == "__main__":
    unittest.main()
