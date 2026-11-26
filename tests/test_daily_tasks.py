"""Replay sanitized Maa OCR from the user's screenshots; never connect to a device."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from daily_tasks import Card, DailyRunner, DailyStopped, DailyView, PICKUPS, TOTALS, WAIT_SECONDS, choose_task
from gui_backend import DeviceBackend
from quiz_core import QuizError
from quiz_vision import TextBlock

FIXTURES = json.loads((Path(__file__).parent / "fixtures/daily_ocr.json").read_text(encoding="utf-8"))
# These historical OCR replays contain only the original five daily tasks.
LEGACY_TASKS = {"帮派任务", "五环任务", "师门任务", "宝图任务", "卓越竞技场"}


def view(name, scale=1):
    frame = np.zeros((470 * scale, 834 * scale, 3), dtype=np.uint8)
    blocks = [TextBlock(b["text"], b["score"], tuple(v * scale for v in b["box"])) for b in FIXTURES[name]]
    return DailyView(frame, blocks)


def assert_clicks_in_boxes(case, points, *boxes, scale=1):
    case.assertEqual(len(points), len(boxes))
    for (px, py), (x, y, w, h) in zip(points, boxes):
        case.assertTrue(x * scale <= px < (x + w) * scale and y * scale <= py < (y + h) * scale,
                        f"{(px, py)} outside {(x, y, w, h)} at scale {scale}")


def dali_view(action="前往", done=False):
    current = view("post_arena_clean")
    current.blocks = [TextBlock(action, b.score, b.box)
                      if b.text == "前注" and b.box[0] < 480 and b.box[1] < 130 else b for b in current.blocks]
    if done:
        current.blocks = [TextBlock("日次数：10/10", b.score, b.box)
                          if b.text == "日次数：0/10" and b.box[1] < 130 else b for b in current.blocks]
    return current


def broom_guide(scale=1):
    current = view("weekly_activity", scale)
    current.blocks.extend([TextBlock("用了金扫帚，", .99, (212, 416, 85, 16)),
                           TextBlock("日常不用愁～", .99, (303, 416, 86, 16))])
    return current


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

    def test_daily_dali_card_is_included_in_list_and_can_be_selected(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                cards = view("post_arena_clean", scale).cards()
                dali = next(c for c in cards if c.name == "大理寺答题")
                self.assertEqual((dali.current, dali.total, dali.action), (0, 10, "前往"))
                self.assertEqual(choose_task(cards, {"大理寺答题"}), dali)

    def test_daily_dali_sweep_priority_changes_at_three_remaining_tasks(self):
        cards = [Card("帮派任务", 0, 20, "前往", (0, 0, 1, 1)),
                 Card("大理寺答题", 0, 10, "立即完成", (0, 0, 1, 1)),
                 Card("宝图任务", 0, 15, "立即完成", (0, 0, 1, 1)),
                 Card("卓越竞技场", 0, 3, "立即完成", (0, 0, 1, 1))]
        self.assertEqual(choose_task(cards, set(TOTALS)).name, "卓越竞技场")
        self.assertEqual(choose_task(cards, {"帮派任务", "宝图任务", "大理寺答题"}).name, "大理寺答题")
        self.assertEqual(choose_task(cards, {"帮派任务", "大理寺答题"}).name, "大理寺答题")

    def test_dali_is_first_with_up_to_three_pending_selected_tasks(self):
        others = [Card("卓越竞技场", 0, 3, "立即完成", (0, 0, 1, 1)),
                  Card("宝图任务", 0, 15, "立即完成", (0, 0, 1, 1)),
                  Card("帮派任务", 0, 20, "前往", (0, 0, 1, 1))]
        for action in ("前往", "立即完成"):
            dali = Card("大理寺答题", 0, 10, action, (0, 0, 1, 1))
            for remaining in (1, 2, 3, 4):
                with self.subTest(action=action, remaining=remaining):
                    cards = [*others[:remaining - 1], dali]
                    selected = {c.name for c in cards}
                    expected = dali if remaining <= 3 else others[0]
                    self.assertEqual(choose_task(cards, selected), expected)

    def test_dali_priority_does_not_count_completed_or_unselected_tasks(self):
        cards = [Card("帮派任务", 20, 20, "", None),
                 Card("师门任务", 0, 20, "前往", (0, 0, 1, 1)),
                 Card("五环任务", 0, 1, "前往", (0, 0, 1, 1)),
                 Card("宝图任务", 0, 15, "立即完成", (0, 0, 1, 1)),
                 Card("大理寺答题", 0, 10, "前往", (0, 0, 1, 1))]
        selected = set(TOTALS) - {"师门任务"}
        self.assertEqual(choose_task(cards, selected).name, "大理寺答题")
        self.assertEqual(choose_task(cards, selected - {"大理寺答题"}).name, "宝图任务")
        completed_dali = [*cards[:-1], Card("大理寺答题", 10, 10, "前往", (0, 0, 1, 1))]
        self.assertEqual(choose_task(completed_dali, selected).name, "宝图任务")

    def test_completed_arena_with_go_button_is_skipped(self):
        arena = next(c for c in view("finished").cards() if c.name == "卓越竞技场")
        self.assertTrue(arena.done)
        self.assertIsNone(arena.button)
        self.assertIsNone(choose_task(view("finished").cards(), {"卓越竞技场"}))

    def test_arena_without_a_unique_sweep_button_has_no_click_target(self):
        for texts in ((), ("前往",), ("前往", "立即完成")):
            with self.subTest(texts=texts):
                current = view("finished")
                button = (424, 98, 43, 22)
                current.blocks = [TextBlock("日次数：0/3", b.score, b.box) if b.text == "日次数：3/3" else b
                                  for b in current.blocks if b.box != button]
                current.blocks = [*current.blocks, *(TextBlock(text, .99, button) for text in texts)]
                arena = next(c for c in current.cards() if c.name == "卓越竞技场")
                self.assertEqual(arena, Card("卓越竞技场", 0, 3, "仅金扫帚", None))
                self.assertFalse(arena.done)

    def test_unselected_tasks_are_never_chosen(self):
        self.assertEqual(choose_task(view("initial").cards(), {"五环任务"}).name, "五环任务")

    def test_five_ring_keeps_current_list_priority(self):
        cards = view("after_guild").cards()
        self.assertEqual(choose_task(cards, set(TOTALS)).name, "五环任务")
        self.assertEqual(choose_task(cards, {"五环任务", "宝图任务"}).name, "五环任务")
        self.assertEqual(choose_task(cards, {"五环任务", "卓越竞技场"}).name, "五环任务")

    def test_field_demon_card_is_not_an_automatic_task(self):
        demon = Card("野外封妖", 0, 5, "前往", (422, 98, 43, 22))
        cards = [demon, *view("after_arena").cards()]
        self.assertEqual(choose_task(cards, set(TOTALS)).name, "帮派任务")
        for tasks in (TOTALS, PICKUPS, WAIT_SECONDS):
            self.assertNotIn(demon.name, tasks)

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

    def test_partially_visible_bottom_card_is_completed(self):
        for name in ("帮派任务", "师门任务"):
            for scale in (1, 2):
                with self.subTest(name=name, scale=scale):
                    current = view("after_arena", scale)
                    current.blocks = [TextBlock(b.text, b.score, (b.box[0], 310, b.box[2], b.box[3]))
                                      if b.text == name else b for b in current.blocks]
                    card = next(c for c in current.cards() if c.name == name)
                    self.assertTrue(card.done)
                    self.assertIsNone(card.current)
                    self.assertIsNone(card.button)
                    self.assertEqual(card.action, "")

    def test_live_completed_guild_at_bottom_keeps_other_tasks_available(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                cards = view("completed_guild_bottom", scale).cards()
                guild = next(c for c in cards if c.name == "帮派任务")
                self.assertTrue(guild.done)
                self.assertIsNone(guild.current)
                self.assertEqual(choose_task(cards, set(TOTALS)).name, "卓越竞技场")
                self.assertEqual(choose_task(cards, {"帮派任务", "师门任务", "五环任务"}).name, "五环任务")

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

    def test_guild_announcements_and_unrelated_counts_are_not_task_progress(self):
        for blocks in (
            [TextBlock("备战-帮派悬赏", .99, (682, 109, 110, 16)),
             TextBlock("锄头18/100", .99, (682, 148, 100, 16))],
            [TextBlock("帮派任务", .99, (682, 109, 80, 16)),
             TextBlock("其他活动1/10", .99, (682, 148, 100, 16))],
            [TextBlock("帮派悬赏（1/10）", .99, (682, 109, 120, 16))],
        ):
            with self.subTest(text=[b.text for b in blocks]):
                current = DailyView(view("five_end").frame, blocks)
                self.assertIsNone(current.tracking())

    def test_task_progress_fragments_on_the_same_row_are_recognized(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                current = view("guild_active", scale)
                current.blocks = [TextBlock("帮派", .99, (682, 109, 28, 15)),
                                  TextBlock("教训武官（1/10）", .99, (711, 110, 110, 15))]
                self.assertEqual(current.tracking(), "帮派任务")

    def test_inactive_or_invalid_tracker_counts_are_not_started_tasks(self):
        for text in ("帮派教训武官（0/10）", "帮派教训武官（11/10）", "帮派教训武官（1/0）",
                     "帮派教训武官1/10"):
            with self.subTest(text=text):
                current = view("guild_active")
                current.blocks = [TextBlock(text, .99, (682, 109, 145, 15))]
                self.assertIsNone(current.tracking())

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
            self.list_name = {"guild": "after_guild",
                              "five": "finished" if self.list_name == "five_ready" else "new_broom",
                              "school": "finished" if self.list_name == "school_ready" else "after_school"}[self.active]
        name = self.list_name if self.scene == "list" else (
            f"{self.active}_{self.scene}" if self.scene in ("dialog", "active", "end") else self.scene)
        if name == "after_school":
            current = view("new_broom")
            current.blocks = [TextBlock({"日次数：0/20": "日次数：20/20", "日次数：1/1": "日次数：0/1"}.get(b.text, b.text),
                                        b.score, b.box) for b in current.blocks]
            current.blocks = [*current.blocks, TextBlock("前注", 0.99, (423, 258, 41, 21))]
            return current
        if name in ("school_ready", "five_ready"):
            current = view("finished")
            left, total = (483, 20) if name == "school_ready" else (250, 1)
            current.blocks = [TextBlock(f"日次数：0/{total}", b.score, b.box)
                              if b.text == f"日次数：{total}/{total}" and left < b.box[0] < left + 229 and 160 < b.box[1] < 190
                              else b for b in current.blocks]
            current.blocks = [*current.blocks, TextBlock("前注", 0.99, (left + 170, 180, 46, 18))]
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
                self.list_name = "five_ready" if self.list_name == "after_school" else "school_ready"
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

    def test_dali_becomes_first_at_three_pending_tasks_without_counting_five_ring_as_done(self):
        tasks = ("五环任务", "帮派任务", "师门任务", "大理寺答题")
        for ready in (False, True):
            with self.subTest(five_ring_ready=ready):
                completed = ()
                self.runner.five_ring_ready = ready

                def stable():
                    cards = [Card(name, TOTALS[name] if name in completed else 0, TOTALS[name],
                                  "" if name in completed else "前往", (420, 100, 40, 20)) for name in tasks]
                    return view("finished"), cards

                def pickup(current, card):
                    nonlocal completed
                    completed = (*completed, card.name)
                    return True

                with patch.object(self.runner, "read", return_value=view("finished")), \
                        patch.object(self.runner, "stable_list", side_effect=stable), \
                        patch.object(self.runner, "pickup", side_effect=pickup):
                    result = self.runner.run(tasks)
                self.assertEqual(result["reason"], "completed")
                expected = (("五环任务", "大理寺答题", "帮派任务", "师门任务") if ready else
                            ("帮派任务", "大理寺答题", "师门任务", "五环任务"))
                self.assertEqual(completed, expected)

    def test_five_ring_rechecked_after_other_task_then_runs_before_remaining_tasks(self):
        completed, pickups = [], []
        initial = view("initial")
        def stable():
            self.runner.five_ring_ready = bool(completed)
            cards = [Card(name, TOTALS[name] if name in completed else 0, TOTALS[name],
                          "" if name in completed else "前往", (420, 100, 40, 20))
                     for name in ("五环任务", "帮派任务", "师门任务")]
            return view("initial"), cards
        def pickup(current, card):
            nonlocal completed, pickups
            pickups = [*pickups, card.name]
            completed = [*completed, card.name]
            return True
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", side_effect=stable), \
                patch.object(self.runner, "pickup", side_effect=pickup):
            result = self.runner.run(["五环任务", "帮派任务", "师门任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(pickups, ["帮派任务", "五环任务", "师门任务"])

    def test_only_five_ring_remaining_runs_without_extra_activity_check(self):
        self.runner.five_ring_ready = False
        with patch.object(self.runner, "pickup", wraps=self.runner.pickup) as pickup:
            result = self.runner.run(["五环任务"])
        self.assertEqual(result["reason"], "completed")
        pickup.assert_called_once()
        self.assertFalse(any(scene == "list" and y > 430 for scene, x, y in self.session.clicks))

    def test_arena_without_broom_does_not_block_last_five_ring(self):
        self.session.list_name = "five_ready"
        original = self.session.current_view

        def current_view():
            current = original()
            current.blocks = [TextBlock("日次数：0/3", b.score, b.box) if b.text == "日次数：3/3" else b
                              for b in current.blocks]
            return current

        with patch.object(self.session, "current_view", side_effect=current_view):
            result = self.runner.run(["卓越竞技场", "五环任务"])
        self.assertEqual(result["completed"], ["五环任务"])
        self.assertEqual(result["pending"], ["卓越竞技场"])
        assert_clicks_in_boxes(self, [(x, y) for scene, x, y in self.session.clicks if scene == "list"],
                               (420, 180, 46, 18))

    def test_activity_bar_threshold_scales_and_requires_activity_page(self):
        for scale in (1, 2):
            for ready in (True, False):
                with self.subTest(scale=scale, ready=ready):
                    current = view("weekly_activity", scale)
                    current.blocks = [*current.blocks, TextBlock("活跃度", .99, (251, 377, 45, 18)),
                                      TextBlock("50", .99, (365, 414, 18, 16))]
                    current.frame[416*scale:424*scale, 375*scale:383*scale] = (151, 177, 50) if ready else (115, 139, 171)
                    self.assertEqual(current.five_ring_available(), ready)
        with self.assertRaisesRegex(QuizError, "50 刻度"):
            view("weekly_activity").five_ring_available()
        with self.assertRaisesRegex(QuizError, "普通活动"):
            view("initial").five_ring_available()

    def test_activity_checked_during_broom_entry_without_extra_return(self):
        activity = view("weekly_activity")
        activity.blocks = [*activity.blocks, TextBlock("活跃度", .99, (251, 377, 45, 18)),
                           TextBlock("50", .99, (365, 414, 18, 16))]
        activity.frame[416:424, 375:383] = (151, 177, 50)
        sweep = view("initial")
        with patch.object(self.runner, "read", side_effect=[activity, sweep]), \
                patch.object(self.session, "click") as click:
            self.assertIs(self.runner.open_list(), sweep)
        self.assertTrue(self.runner.five_ring_ready)
        assert_clicks_in_boxes(self, [click.call_args.args], (713, 405, 40, 30))
        click.assert_called_once()

    def test_full_screenshot_flow_new_broom_popup_and_completion(self):
        with patch.object(self.runner, "pickup", wraps=self.runner.pickup) as pickup:
            result = self.runner.run(LEGACY_TASKS)
        self.assertEqual([call.args[1].name for call in pickup.call_args_list], ["帮派任务", "师门任务", "五环任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(set(result["completed"]), LEGACY_TASKS)
        self.assertEqual(result["pending"], [])
        self.assertGreaterEqual(self.clock.now, sum(WAIT_SECONDS[name] for name in ("帮派任务", "五环任务", "师门任务")))
        self.assertEqual(sum(scene == "dialog" for scene, _, _ in self.session.clicks), 3)
        self.assertEqual(sum(scene == "dig_popup" for scene, _, _ in self.session.clicks), 1)

    def test_task_interval_varies_between_completed_tasks_and_refreshes_click_views(self):
        delays = iter((10, 180))
        reward_delays = iter((8, 15))
        def random_delay(low, high):
            if (low, high) == (8, 15):
                return next(reward_delays)
            return next(delays) if (low, high) == (10, 180) else 0.25
        with patch("daily_tasks.random.uniform", side_effect=random_delay) as uniform, \
                patch.object(self.runner, "sleep", wraps=self.runner.sleep) as sleep:
            result = self.runner.run(LEGACY_TASKS)
        self.assertEqual(result["reason"], "completed")
        self.assertEqual([call.args[0] for call in sleep.call_args_list if call.args[0] >= 8], [8, 10, 180, 15])
        self.assertEqual(sum(call.args == (10, 180) for call in uniform.call_args_list), 2)
        self.assertEqual(sum(call.args == (8, 15) for call in uniform.call_args_list), 2)

    def test_document_keeps_long_interval_and_quiz_uses_reward_interval(self):
        for name in ("宝图任务", "大理寺答题"):
            with self.subTest(name=name):
                runner = DailyRunner(self.session, Path(self.tmp.name), lambda: False, task_interval_max=18)
                pickups = []
                def current_list():
                    current = view("finished")
                    pending = [Card(task, 0, TOTALS[task], "前往", (420, 100, 40, 20))
                               for task in (name, "师门任务") if task not in pickups]
                    return current, pending + [Card(task, TOTALS[task], TOTALS[task], "", None) for task in pickups]
                def pickup(current, card):
                    nonlocal pickups
                    pickups = [*pickups, card.name]
                    return True
                with patch.object(runner, "read", side_effect=lambda: view("finished")), \
                        patch.object(runner, "stable_list", side_effect=current_list) as stable, \
                        patch.object(runner, "pickup", side_effect=pickup), \
                        patch("daily_tasks.random.uniform", return_value=15) as uniform, \
                        patch.object(runner, "sleep", wraps=runner.sleep) as sleep:
                    result = runner.run([name, "师门任务"])
                self.assertEqual(result["reason"], "completed")
                expected = (10, 18) if name == "宝图任务" else (8, 15)
                uniform.assert_called_once_with(*expected)
                self.assertIn(unittest.mock.call(15), sleep.call_args_list)
                self.assertEqual(stable.call_count, 6)

    def test_stop_during_task_interval_prevents_next_pickup(self):
        stopped = []
        self.runner.should_stop = lambda: bool(stopped) and self.clock.now >= stopped[0]
        original_sleep = self.runner.sleep
        def sleep(seconds):
            nonlocal stopped
            if seconds >= 10:
                stopped = [self.clock.now + 1]
            original_sleep(seconds)
        with patch.object(self.runner, "sleep", side_effect=sleep), \
                patch("daily_tasks.random.uniform", side_effect=lambda low, high: 10 if low == 10 else 0.25):
            result = self.runner.run(LEGACY_TASKS)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual([scene for scene, _, _ in self.session.clicks],
                         ["list", "list", "dialog", "end", "five_end", "activity"])
        self.assertEqual(self.session.active, "guild")

    def test_stop_during_reward_interval_prevents_next_task(self):
        deadline = []
        self.runner.should_stop = lambda: bool(deadline) and self.clock.now >= deadline[0]
        original_sleep = self.runner.sleep
        def sleep(seconds):
            nonlocal deadline
            if seconds == 8:
                deadline = [self.clock.now + 1]
            original_sleep(seconds)
        def delay(low, high):
            return 8 if (low, high) == (8, 15) else 0.25
        with patch.object(self.runner, "sleep", side_effect=sleep), \
                patch("daily_tasks.random.uniform", side_effect=delay):
            result = self.runner.run(LEGACY_TASKS)
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual([scene for scene, _, _ in self.session.clicks], ["list"])
        self.assertEqual(self.session.list_name, "after_arena")

    def test_resumed_task_completion_waits_before_next_daily(self):
        with patch.object(self.runner, "read", return_value=view("guild_active")), \
                patch.object(self.runner, "wait_task") as wait, \
                patch.object(self.runner, "stable_list", side_effect=[
                    (view("after_guild"), view("after_guild").cards()),
                    (view("finished"), view("finished").cards())]), \
                patch("daily_tasks.random.uniform", return_value=10) as uniform, \
                patch.object(self.runner, "tap") as tap:
            result = self.runner.run(["帮派任务", "师门任务"])
        self.assertEqual(result["reason"], "completed")
        wait.assert_called_once_with("帮派任务")
        uniform.assert_called_once_with(10, 180)
        self.assertAlmostEqual(self.clock.now, 10, delta=0.002)
        tap.assert_not_called()

    def test_no_task_interval_when_all_done_or_only_unexecutable_tasks_remain(self):
        for selected in (["帮派任务"], ["帮派任务", "卓越竞技场"]):
            with self.subTest(selected=selected):
                current = view("after_guild")
                cards = [Card(c.name, 0, c.total, "前往", c.button) if c.name == "卓越竞技场" else c
                         for c in current.cards()]
                with patch.object(self.runner, "read", return_value=view("guild_active")), \
                        patch.object(self.runner, "wait_task"), \
                        patch.object(self.runner, "stable_list", return_value=(current, cards)), \
                        patch("daily_tasks.random.uniform") as uniform:
                    result = self.runner.run(selected)
                self.assertEqual(result["reason"], "completed" if len(selected) == 1 else "needs_attention")
                uniform.assert_not_called()

    def test_task_interval_validation_rejects_invalid_maximum(self):
        for maximum in (-1, 9, True, float("nan"), float("inf"), "300", None):
            with self.subTest(maximum=maximum), self.assertRaisesRegex(QuizError, "间隔"):
                DailyRunner(self.session, Path(self.tmp.name), lambda: False, task_interval_max=maximum)
        self.assertEqual(self.session.clicks, [])

    def test_completed_bottom_card_does_not_block_list_stability_or_task_completion(self):
        with patch.object(self.runner, "read", side_effect=lambda: view("completed_guild_bottom")):
            result = self.runner.run(["帮派任务"])
            _, cards = self.runner.stable_list()
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["completed"], ["帮派任务"])
        self.assertEqual(choose_task(cards, set(TOTALS)).name, "卓越竞技场")
        self.assertEqual(self.session.clicks, [])
        self.assertLess(self.clock.now, 1)

    def test_open_activity_and_broom_from_main_screenshot(self):
        for scale in (1, 2):
            with self.subTest(scale=scale), patch.object(
                self.runner, "read", side_effect=[view("main_entry", scale), view("weekly_activity", scale), view("initial", scale)]
            ), patch.object(self.session, "click") as click:
                result = self.runner.open_list()
                self.assertTrue(result.sweep_panel)
                assert_clicks_in_boxes(self, [call.args for call in click.call_args_list],
                                       (8, 108, 42, 42), (713, 405, 40, 30), scale=scale)

    def test_ordinary_taps_vary_in_position_and_timing(self):
        box = (800.25, 450.25, 33.75, 19.75)
        times = []
        with patch("daily_tasks.random.uniform", side_effect=[0.15, 0.35]), \
                patch("quiz_vision.random.randint", side_effect=lambda low, high: (low, high)[len(times)]), \
                patch.object(self.session, "click", side_effect=lambda *_: times.append(self.clock.now)) as click:
            self.runner.tap(view("initial", scale=2), box)
            self.runner.tap(view("initial", scale=2), box)
        points = [call.args for call in click.call_args_list]
        assert_clicks_in_boxes(self, points, box, box, scale=2)
        self.assertNotEqual(*points)
        self.assertAlmostEqual(times[0], 0.15)
        self.assertAlmostEqual(times[1] - times[0], 0.35, delta=0.002)

    def test_stop_or_stale_frame_during_tap_pause_prevents_click(self):
        with patch("daily_tasks.random.uniform", return_value=0.30):
            current = view("initial")
            self.clock.now = 9.9
            with self.assertRaisesRegex(QuizError, "过期"):
                self.runner.tap(current, (713, 405, 40, 30))
            current = view("initial")
            deadline = self.clock.now + 0.1
            self.runner.should_stop = lambda: self.clock.now >= deadline
            with self.assertRaises(DailyStopped):
                self.runner.tap(current, (713, 405, 40, 30))
        self.assertEqual(self.session.clicks, [])

    def test_dig_popup_cancel_has_no_added_pause(self):
        popup = view("dig_popup")
        with patch.object(self.runner, "read", side_effect=[popup, view("initial")]), \
                patch.object(self.session, "click") as click:
            self.assertTrue(self.runner.open_list().sweep_panel)
        assert_clicks_in_boxes(self, [call.args for call in click.call_args_list],
                               popup.find("取消", (250, 280, 160, 50)))
        self.assertEqual(self.clock.now, 0)

    def test_weekly_activity_goes_directly_to_broom(self):
        self.assertTrue(view("weekly_activity").activity_panel)
        with patch.object(self.runner, "read", side_effect=[view("weekly_activity"), view("initial")]), \
                patch.object(self.session, "click") as click:
            self.assertTrue(self.runner.open_list().sweep_panel)
        assert_clicks_in_boxes(self, [call.args for call in click.call_args_list], (713, 405, 40, 30))

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
        assert_clicks_in_boxes(self, clicks, (8, 108, 42, 42), (8, 108, 42, 42), (713, 405, 40, 30))

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
        assert_clicks_in_boxes(self, clicks, (8, 108, 42, 42), (713, 405, 40, 30))

    def test_reward_toasts_are_retried_before_reading_clean_list(self):
        frames = iter([view("guild_count_without_day"), view("post_arena_clean"), view("post_arena_clean")])
        with patch.object(self.runner, "open_list", return_value=next(frames)), \
                patch.object(self.runner, "read", side_effect=lambda: next(frames)):
            _, cards = self.runner.stable_list()
        self.assertTrue(next(c for c in cards if c.name == "帮派任务").done)
        self.assertEqual(choose_task(cards, set(TOTALS)).name, "大理寺答题")
        self.assertEqual(self.session.clicks, [])

    def test_broom_guide_is_closed_before_opening_list_and_running_tasks(self):
        original_view, original_click = self.session.current_view, self.session.click
        self.session.scene = "broom_guide"
        self.session.current_view = lambda: broom_guide() if self.session.scene == "broom_guide" else original_view()
        def click(x, y):
            if self.session.scene == "broom_guide":
                self.assertTrue(200 <= x <= 670 and 410 <= y <= 450)
                self.session.clicks.append((self.session.scene, x, y))
                self.session.scene = "activity"
            else:
                original_click(x, y)
        self.session.click = click
        result = self.runner.run(LEGACY_TASKS)
        self.assertEqual(result["reason"], "completed")
        self.assertEqual([s for s, _, _ in self.session.clicks[:2]], ["broom_guide", "activity"])
        self.assertEqual(sum(s == "broom_guide" for s, _, _ in self.session.clicks), 1)
        self.assertEqual(sum(s == "dialog" for s, _, _ in self.session.clicks), 3)

    def test_broom_guide_during_stability_check_reopens_and_rechecks_list(self):
        for scale in (1, 2):
            with self.subTest(scale=scale), patch.object(self.runner, "read", side_effect=[
                view("initial", scale), broom_guide(scale), broom_guide(scale),
                view("weekly_activity", scale), view("initial", scale), view("initial", scale)
            ]), patch.object(self.session, "click") as click:
                _, cards = self.runner.stable_list()
                self.assertEqual(choose_task(cards, set(TOTALS)).name, "卓越竞技场")
                assert_clicks_in_boxes(self, [call.args for call in click.call_args_list],
                                       (400, 420, 20, 10), (713, 405, 40, 30), scale=scale)

    def test_broom_guide_after_entry_click_is_handled(self):
        with patch.object(self.runner, "read", side_effect=[view("weekly_activity"), broom_guide(),
                          view("initial")]), patch.object(self.session, "click") as click:
            self.assertTrue(self.runner.open_list().sweep_panel)
        self.assertEqual(click.call_count, 2)
        assert_clicks_in_boxes(self, [click.call_args_list[0].args], (713, 405, 40, 30))

    def test_broom_guide_not_closed_pauses_without_repeated_click(self):
        with patch.object(self.runner, "read", side_effect=lambda: broom_guide()), \
                patch.object(self.session, "click") as click:
            with self.assertRaisesRegex(QuizError, "引导.*未.*关闭"):
                self.runner.open_list()
        click.assert_called_once()

    def test_stop_from_broom_guide_message_prevents_dismissal_click(self):
        stopped = []
        self.runner.should_stop = lambda: bool(stopped)
        self.runner.on_result = lambda result: stopped.append(True)
        with patch.object(self.runner, "read", side_effect=lambda: broom_guide()), \
                patch.object(self.session, "click") as click:
            self.assertEqual(self.runner.run(LEGACY_TASKS)["reason"], "stopped")
        click.assert_not_called()

    def test_unrelated_broom_dialog_does_not_authorize_a_click(self):
        current = view("guild_dialog")
        current.blocks.append(TextBlock("金扫帚不足，是否购买？", .99, (212, 416, 180, 16)))
        with patch.object(self.runner, "read", return_value=current), patch.object(self.session, "click") as click:
            with self.assertRaises(QuizError):
                self.runner.open_list()
        click.assert_not_called()

    def test_cancellation_from_progress_prevents_sweep_click(self):
        stopped = []
        self.runner.should_stop = lambda: bool(stopped)
        self.runner.on_result = lambda result: stopped.append(True)
        self.assertEqual(self.runner.run(LEGACY_TASKS)["reason"], "stopped")
        self.assertEqual(self.session.clicks, [])

    def test_already_completed_has_no_clicks(self):
        self.session.list_name = "finished"
        self.assertEqual(self.runner.run(LEGACY_TASKS)["reason"], "completed")
        self.assertEqual(self.session.clicks, [])

    def test_normal_treasure_uses_estimate_as_fallback_without_completion_hint(self):
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

    def test_active_treasure_checks_early_without_picking_up_again(self):
        finished = view("finished")
        with patch.object(self.runner, "read", side_effect=[view("treasure_active"), view("dig_popup")]), \
                patch.object(self.runner, "stable_list", return_value=(finished, finished.cards())), \
                patch.object(self.runner, "tap") as tap:
            result = self.runner.run(["宝图任务"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(self.clock.now, 5)
        tap.assert_not_called()

    def test_daily_dali_uses_broom_without_starting_random_quiz(self):
        initial, finished = dali_view("立即完成"), dali_view(done=True)
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", side_effect=[(initial, initial.cards()),
                    (finished, finished.cards()), (finished, finished.cards())]), \
                patch.object(self.runner, "tap") as tap, patch.object(self.runner, "answer_quiz") as answer:
            result = self.runner.run(["大理寺答题"])
        self.assertEqual(result, {"reason": "completed", "completed": ["大理寺答题"], "pending": []})
        card = next(c for c in initial.cards() if c.name == "大理寺答题")
        tap.assert_called_once_with(initial, card.button)
        answer.assert_not_called()

    def test_daily_dali_failed_sweep_does_not_retry_or_answer(self):
        initial = dali_view("立即完成")
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", return_value=(initial, initial.cards())), \
                patch.object(self.runner, "tap") as tap, patch.object(self.runner, "answer_quiz") as answer:
            with self.assertRaisesRegex(QuizError, "扫帚操作后未确认大理寺答题完成"):
                self.runner.run(["大理寺答题"])
        tap.assert_called_once()
        answer.assert_not_called()

    def test_daily_dali_without_broom_answers_then_verifies_list(self):
        initial, finished = dali_view(), dali_view(done=True)
        dialog = view("dali_dialog")
        progress = []
        self.runner.on_result = progress.append
        def answer(report):
            report({"question_number": 10, "question_total": 10})
            return {"reason": "completed", "submitted": 10}
        with patch.object(self.runner, "read", side_effect=[initial, dialog]), \
                patch.object(self.runner, "stable_list", side_effect=[(initial, initial.cards()),
                    (finished, finished.cards()), (finished, finished.cards())]), \
                patch.object(self.session, "click") as click, \
                patch.object(self.runner, "answer_quiz", side_effect=answer) as quiz, \
                patch("daily_tasks.random.uniform", return_value=.3):
            result = self.runner.run(["大理寺答题"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(click.call_count, 2)
        assert_clicks_in_boxes(self, [click.call_args.args], dialog.find("我要参加有奖问答", (530, 160, 280, 300)))
        quiz.assert_called_once()
        self.assertTrue(any("已提交 10/10 题" in p["message"] for p in progress))
        self.assertLess(self.clock.now, 2)

    def test_daily_dali_resumes_unfinished_quiz(self):
        initial, dialog = dali_view(), view("dali_resume_dialog")
        card = next(c for c in initial.cards() if c.name == "大理寺答题")
        with patch.object(self.runner, "read", return_value=dialog), \
                patch.object(self.session, "click") as click, \
                patch.object(self.runner, "answer_quiz", return_value={"reason": "completed", "submitted": 6}) as quiz:
            self.assertTrue(self.runner.pickup(initial, card))
        assert_clicks_in_boxes(self, [call.args for call in click.call_args_list],
                               card.button, dialog.find("大理寺答题（未完成）", (530, 160, 280, 300)))
        quiz.assert_called_once()

    def test_daily_dali_waits_for_both_the_official_and_the_quiz_option(self):
        initial = dali_view()
        card = next(c for c in initial.cards() if c.name == "大理寺答题")
        for missing in ("大理寺官员", "我要参加有奖问答"):
            with self.subTest(missing=missing):
                dialog = view("dali_dialog")
                dialog.blocks = [b for b in dialog.blocks if b.text != missing]
                with patch.object(self.runner, "read", return_value=dialog), \
                        patch.object(self.runner, "tap") as tap, patch.object(self.runner, "answer_quiz") as quiz:
                    with self.assertRaisesRegex(QuizError, "等待大理寺答题领取对话超时"):
                        self.runner.pickup(initial, card)
                tap.assert_called_once_with(initial, card.button)
                quiz.assert_not_called()

    def test_daily_dali_random_answers_are_not_completion_without_list_confirmation(self):
        initial = dali_view()
        dialog = view("dali_dialog")
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", return_value=(initial, initial.cards())), \
                patch.object(self.runner, "until", return_value=dialog), patch.object(self.runner, "tap") as tap, \
                patch.object(self.runner, "answer_quiz", return_value={"reason": "completed", "submitted": 10}) as quiz:
            with self.assertRaisesRegex(QuizError, "答题后未确认大理寺答题完成"):
                self.runner.run(["大理寺答题"])
        self.assertEqual(tap.call_count, 2)
        quiz.assert_called_once()

    def test_daily_dali_stopped_random_quiz_prevents_further_navigation(self):
        initial = dali_view()
        dialog = view("dali_dialog")
        with patch.object(self.runner, "read", return_value=initial), \
                patch.object(self.runner, "stable_list", return_value=(initial, initial.cards())) as stable, \
                patch.object(self.runner, "until", return_value=dialog), patch.object(self.runner, "tap") as tap, \
                patch.object(self.runner, "answer_quiz", return_value={"reason": "stopped", "submitted": 1}):
            self.assertEqual(self.runner.run(["大理寺答题"])["reason"], "stopped")
        self.assertEqual(tap.call_count, 2)
        stable.assert_called_once()

    def test_daily_dali_unselected_card_is_not_clicked(self):
        with patch.object(self.runner, "read", side_effect=lambda: dali_view("立即完成")), \
                patch.object(self.runner, "answer_quiz") as answer:
            self.assertEqual(self.runner.run(["帮派任务"])["reason"], "completed")
        answer.assert_not_called()
        self.assertEqual(self.session.clicks, [])

    def test_daily_dali_missing_selected_title_is_not_reported_completed(self):
        self.session.list_name = "finished"
        with self.assertRaisesRegex(QuizError, "大理寺答题.*未.*识别|未.*识别.*大理寺答题"):
            self.runner.run(["大理寺答题"])
        self.assertEqual(self.session.clicks, [])

    def test_unreadable_arena_button_does_not_block_school_pickup(self):
        self.session.list_name = "school_ready"
        original = self.session.current_view

        def current_view():
            current = original()
            if self.session.scene == "list":
                current.blocks = [TextBlock("日次数：0/3", b.score, b.box) if b.text == "日次数：3/3" else b
                                  for b in current.blocks if b.box != (424, 98, 43, 22)]
                current.frame[98:120, 424:467] = (180, 200, 50)
            return current

        with patch.object(self.session, "current_view", side_effect=current_view):
            result = self.runner.run(["卓越竞技场", "师门任务"])
        self.assertEqual(result["reason"], "needs_attention")
        self.assertEqual(result["completed"], ["师门任务"])
        self.assertEqual(result["pending"], ["卓越竞技场"])
        assert_clicks_in_boxes(self, [(x, y) for scene, x, y in self.session.clicks if scene == "list"],
                               (653, 180, 46, 18))

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
        self.runner.should_stop = lambda: self.session.scene == "active" and self.clock.now >= 5
        self.session.list_name = "after_arena"
        result = self.runner.run(["帮派任务"])
        self.assertEqual(result["reason"], "stopped")
        self.assertEqual([scene for scene, _, _ in self.session.clicks], ["list", "dialog"])

    def test_guild_announcement_does_not_enter_wait_on_startup(self):
        current, finished = view("five_end"), view("finished")
        current.blocks = [*current.blocks,
                          TextBlock("备战-帮派悬赏", .99, (682, 109, 110, 16)),
                          TextBlock("锄头18/100", .99, (682, 148, 100, 16))]
        with patch.object(self.runner, "read", return_value=current), \
                patch.object(self.runner, "stable_list", return_value=(finished, finished.cards())), \
                patch.object(self.runner, "wait_task") as wait:
            result = self.runner.run(LEGACY_TASKS)
        self.assertEqual(result["reason"], "completed")
        wait.assert_not_called()
        self.assertEqual(self.session.clicks, [])

    def test_guild_announcement_during_pickup_does_not_skip_npc_option(self):
        original = self.session.current_view
        self.session.list_name = "after_arena"
        def current():
            result = original()
            if self.session.scene == "dialog":
                result.blocks = [*result.blocks,
                                 TextBlock("备战-帮派悬赏", .99, (682, 109, 110, 16)),
                                 TextBlock("锄头18/100", .99, (682, 148, 100, 16))]
            return result
        self.session.current_view = current
        initial = view("after_arena")
        card = next(c for c in initial.cards() if c.name == "帮派任务")
        self.assertFalse(self.runner.pickup(initial, card))
        self.assertEqual(self.session.scene, "active")
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
