"""Offline activity-card and NPC replays for claiming a new 200-ring chain."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from daily_tasks import DailyStopped, DailyView
from quiz_core import QuizError
from quiz_vision import TextBlock
from ring_tasks import RingRunner
from test_daily_tasks import assert_clicks_in_boxes
from test_ring_tasks import screen


GO = (425, 238, 37, 17)
CLAIM = (553, 293, 220, 20)


def card(claimed="0/1", progress=0, *, dx=0, dy=0):
    base = screen("weekly_top")
    blocks = [TextBlock(text, .99, (x + dx, y + dy, w, h)) for text, (x, y, w, h) in (
        ("200环任务链", (261, 203, 87, 16)), ("环数：" + str(progress) + "/200", (312, 231, 82, 14)),
        ("已领取：" + claimed, (312, 248, 76, 13)), ("前注", GO),
    )]
    return DailyView(base.frame, [*base.blocks, *blocks])


def npc(name="护园大将", option="有什么可以帮助你的（领取200环）", *, dy=0):
    base = screen("battle")
    x, y, w, h = CLAIM
    return DailyView(base.frame, [TextBlock(name, .99, (634, 179, 59, 15)),
                                 TextBlock(option, .99, (x, y + dy, w, h)),
                                 TextBlock("我只是看看", .99, (626, 344, 75, 16))])


class RingClaimTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = time.monotonic()
        clock = patch("time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.session = MagicMock()
        self.runner = RingRunner(self.session, Path(folder.name), lambda: False)
        self.runner.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        self.runner.save = MagicMock()
        self.runner.save_metrics = MagicMock()

    def feed(self, *views):
        frames = iter(views)
        self.runner.read = lambda: next(frames, views[-1])

    def test_available_quota_claims_once_at_fresh_positions_and_starts_monitoring(self):
        initial, fresh = card("1/2"), card("1/2", dy=8)
        self.feed(initial, fresh, screen("battle"), npc(), npc(dy=4), screen(count=1), screen(count=200))
        self.assertEqual(self.runner.run()["reason"], "completed")
        assert_clicks_in_boxes(self, [c.args for c in self.session.click.call_args_list],
                               (GO[0], GO[1] + 8, *GO[2:]), (CLAIM[0], CLAIM[1] + 4, *CLAIM[2:]))
        self.session.swipe.assert_not_called()

    def test_start_without_daily_tracker_finds_and_claims_new_round_in_activity(self):
        self.feed(screen("daily_other"), screen("weekly_top"), card(), card(), npc(), npc(), screen(count=200))
        result = self.runner.run()
        self.assertEqual((result["reason"], result["progress"]), ("completed", 200))
        assert_clicks_in_boxes(self, [c.args for c in self.session.click.call_args_list],
                               (8, 108, 42, 42), (500, 68, 68, 20), GO, CLAIM)

    def test_no_new_claim_when_full_unreadable_or_invalid(self):
        for count in ("1/1", "0/0", "2/1", "0/", "0/1O", ""):
            with self.subTest(count=count), self.assertRaises(QuizError):
                self.runner.open_ring(card(count))
        self.session.click.assert_not_called()

    def test_exhausted_quota_can_resume_confirmed_existing_chain_but_not_claim(self):
        current = card("1/1", progress=81)
        self.feed(current, screen(count=81))
        self.runner.open_ring(current)
        assert_clicks_in_boxes(self, [c.args for c in self.session.click.call_args_list], GO)

    def test_completed_card_with_no_quota_does_not_start_another_round(self):
        with self.assertRaisesRegex(QuizError, "没有可领取的新任务"):
            self.runner.open_ring(card("1/1", progress=200))
        self.session.click.assert_not_called()

    def test_exhausted_quota_cannot_claim_even_if_npc_appears(self):
        current = card("1/1", progress=81)
        self.feed(current, npc())
        with self.assertRaisesRegex(QuizError, "未重复点击"):
            self.runner.open_ring(current)
        assert_clicks_in_boxes(self, [c.args for c in self.session.click.call_args_list], GO)

    def test_only_the_matching_cards_claim_counter_is_used(self):
        current = card("")
        unrelated = TextBlock("已领取：0/1", .99, (544, 248, 76, 13))
        with self.assertRaises(QuizError):
            self.runner.open_ring(DailyView(current.frame, [*current.blocks, unrelated]))
        self.session.click.assert_not_called()

    def test_quota_or_page_change_after_pause_does_not_click_go(self):
        for fresh in (card("1/1"), card("0/"), screen("world"), screen("weekly_top")):
            with self.subTest(), self.assertRaises(QuizError):
                self.feed(fresh)
                self.runner.open_ring(card())
        self.session.click.assert_not_called()

    def test_changed_npc_text_or_unconfirmed_claim_never_clicks_again(self):
        for post_go, check in ((npc(), npc("其他NPC")), (npc(), npc(option="领取200环")), (npc(), npc())):
            with self.subTest(), self.assertRaises(QuizError):
                self.session.click.reset_mock()
                self.feed(card(), post_go, check)
                self.runner.open_ring(card())
            self.assertEqual(self.session.click.call_count, 2 if post_go.blocks == check.blocks else 1)

    def test_stop_or_stale_refresh_prevents_navigation_input(self):
        current = card()
        with patch.object(self.runner, "sleep", side_effect=DailyStopped):
            with self.assertRaises(DailyStopped):
                self.runner.open_ring(current)
        self.feed(DailyView(current.frame, current.blocks, self.now - 11))
        with self.assertRaisesRegex(QuizError, "过期"):
            self.runner.open_ring(current)
        self.session.click.assert_not_called()

    def test_claim_input_pauses_and_randomizes_inside_both_targets(self):
        points, pauses = [], []
        def sleep(seconds):
            pauses.append(seconds)
            self.now += seconds
        self.runner.sleep = sleep
        for _ in range(8):
            self.session.click.reset_mock()
            self.feed(card(), npc(), npc(), screen(count=1))
            self.runner.open_ring(card())
            clicks = [c.args for c in self.session.click.call_args_list]
            assert_clicks_in_boxes(self, clicks, GO, CLAIM)
            points.append(tuple(clicks))
        self.assertEqual(len(pauses), 16)
        self.assertTrue(all(.3 <= seconds <= 1 for seconds in pauses))
        self.assertGreater(len(set(pauses)), 1)
        self.assertGreater(len({pair[0] for pair in points}), 1)
        self.assertGreater(len({pair[1] for pair in points}), 1)
