"""Replay only the sanitized artwork crop; no account data or device input."""
import itertools
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

from daily_tasks import DailyStopped, DailyView
from puzzle_tasks import (BOARD, TILE_BOXES, PuzzleSolver, corner_order, next_swap,
                          puzzle_state, puzzle_tiles, puzzle_visible, same_tiles)
from quiz_core import QuizError
from quiz_vision import TextBlock
from ring_tasks import RingRunner
from test_ring_tasks import screen
from test_daily_tasks import assert_clicks_in_boxes

ART = np.asarray(Image.open(Path(__file__).parent / "fixtures/puzzle_board.png").convert("RGB"))[:, :, ::-1].copy()
RAW = tuple(ART[row * 159:(row + 1) * 159, col * 158:(col + 1) * 158]
            for row in range(2) for col in range(2))
SOLVED = (RAW[0], RAW[3], RAW[2], RAW[1])
PORTRAIT = np.asarray(Image.open(Path(__file__).parent / "fixtures/puzzle_portrait_board.png").convert("RGB"))[:, :, ::-1].copy()
PORTRAIT_RAW = tuple(PORTRAIT[row * 159:(row + 1) * 159, col * 158:(col + 1) * 158]
                     for row in range(2) for col in range(2))
PORTRAIT_SOLVED = (PORTRAIT_RAW[2], PORTRAIT_RAW[0], PORTRAIT_RAW[3], PORTRAIT_RAW[1])
PALACE = np.asarray(Image.open(Path(__file__).parent / "fixtures/puzzle_palace_board.png").convert("RGB"))[:, :, ::-1].copy()
PALACE_RAW = tuple(PALACE[row * 159:(row + 1) * 159, col * 158:(col + 1) * 158]
                   for row in range(2) for col in range(2))
PALACE_SOLVED = (PALACE_RAW[1], PALACE_RAW[2], PALACE_RAW[3], PALACE_RAW[0])
ARTWORKS = (("landscape", SOLVED), ("portrait", PORTRAIT_SOLVED), ("palace", PALACE_SOLVED))


def puzzle(order=(0, 3, 2, 1), used=0, remaining=None, seconds=115, *, artwork=SOLVED):
    remaining = 10 - used if remaining is None else remaining
    frame = np.zeros((470, 834, 3), dtype=np.uint8)
    x, y, w, h = BOARD
    tiles = tuple(artwork[index] for index in order)
    frame[y:y + h, x:x + w] = np.concatenate((np.concatenate(tiles[:2], axis=1),
                                            np.concatenate(tiles[2:], axis=1)), axis=0)
    blocks = [TextBlock("当前进度", .99, (195, 178, 80, 18)),
              TextBlock("第1关/共1关", .99, (180, 210, 95, 18)),
              TextBlock("拼图", .99, (205, 240, 40, 18)),
              TextBlock(f"已操作：{used}次 剩余：{remaining}次", .99, (135, 272, 175, 18)),
              TextBlock("操作规则", .99, (194, 315, 80, 18)),
              TextBlock(f"{seconds}秒", .99, (302, 122, 48, 18))]
    return DailyView(frame, blocks)


class PuzzleParsingTests(unittest.TestCase):
    def test_real_artwork_has_unique_four_corners(self):
        current = puzzle()
        self.assertEqual(corner_order(puzzle_tiles(current)), (0, 3, 2, 1))
        self.assertEqual(next_swap(corner_order(puzzle_tiles(current))), (1, 3))
        self.assertEqual(puzzle_state(current), (0, 10, 115))

    def test_portrait_artwork_with_pale_clothing_has_unique_corners(self):
        current = puzzle((1, 3, 0, 2), artwork=PORTRAIT_SOLVED)
        self.assertEqual(corner_order(puzzle_tiles(current)), (1, 3, 0, 2))
        self.assertEqual(next_swap(corner_order(puzzle_tiles(current))), (0, 2))

    def test_pale_clouds_do_not_count_as_frame_edges(self):
        current = puzzle((3, 0, 1, 2), artwork=PALACE_SOLVED)
        self.assertEqual(corner_order(puzzle_tiles(current)), (3, 0, 1, 2))
        self.assertEqual(next_swap(corner_order(puzzle_tiles(current))), (0, 1))

    def test_real_ocr_counts_and_timer_are_recognized_at_device_scale(self):
        current = puzzle()
        current.blocks = [TextBlock("当前进度", .9981, (194, 175, 61, 16)),
                          TextBlock("第1关/共1关", .9979, (178, 207, 85, 20)),
                          TextBlock("拼图", .9986, (202, 237, 37, 22)),
                          TextBlock("已操作：0次剩余：10次", .9897, (137, 271, 168, 16)),
                          TextBlock("操作规则", .9993, (192, 313, 64, 19)),
                          TextBlock("115秒", .9985, (298, 116, 42, 23))]
        self.assertEqual(puzzle_state(current), (0, 10, 115))

    def test_all_24_permutations_need_at_most_three_swaps(self):
        for (name, artwork), order in itertools.product(ARTWORKS, itertools.permutations(range(4))):
            with self.subTest(artwork=name, order=order):
                self.assertEqual(corner_order(puzzle_tiles(puzzle(order, artwork=artwork))), order)
                count = 0
                current = order
                while pair := next_swap(current):
                    first, second = pair
                    current = tuple(current[second] if i == first else current[first] if i == second else tile
                                    for i, tile in enumerate(current))
                    count += 1
                self.assertLessEqual(count, 3)
                self.assertEqual(current, (0, 1, 2, 3))

    def test_scaled_artwork_keeps_corner_assignments(self):
        for (name, artwork), order, (width, height) in itertools.product(
                ARTWORKS, itertools.permutations(range(4)), ((830, 466), (1600, 900))):
            with self.subTest(artwork=name, order=order, size=(width, height)):
                current = puzzle(order, artwork=artwork)
                frame = np.array(Image.fromarray(current.frame).resize((width, height), Image.Resampling.BILINEAR))
                blocks = [TextBlock(b.text, b.score, (b.box[0] * width / 834, b.box[1] * height / 470,
                                                     b.box[2] * width / 834, b.box[3] * height / 470))
                          for b in current.blocks]
                scaled = DailyView(frame, blocks)
                self.assertEqual(corner_order(puzzle_tiles(scaled)), order)

    def test_scaled_swaps_preserve_tile_identity(self):
        for (name, artwork), order, size in itertools.product(
                ARTWORKS, itertools.permutations(range(4)), ((830, 466), (1600, 900))):
            pair = next_swap(order)
            if pair is None:
                continue
            swapped = tuple(order[pair[1]] if i == pair[0] else order[pair[0]] if i == pair[1] else tile
                            for i, tile in enumerate(order))
            tiles = tuple(puzzle_tiles(DailyView(np.array(Image.fromarray(
                puzzle(arrangement, artwork=artwork).frame).resize(size, Image.Resampling.BILINEAR)), []))
                          for arrangement in (order, swapped))
            before, after = tiles
            expected = tuple(before[pair[1]] if i == pair[0] else before[pair[0]] if i == pair[1] else tile
                             for i, tile in enumerate(before))
            with self.subTest(artwork=name, order=order, size=size):
                self.assertTrue(same_tiles(expected, after))
                self.assertFalse(same_tiles(before, after))

    def test_uniform_white_blue_or_cream_images_are_not_guessed(self):
        for color in (230, (235, 225, 215), (195, 225, 245)):
            tiles = tuple(np.full((159, 158, 3), color, np.uint8) for _ in range(4))
            with self.subTest(color=color), self.assertRaises(QuizError):
                corner_order(tiles)

    def test_unknown_or_duplicate_artwork_is_not_guessed(self):
        for tiles in ((SOLVED[0],) * 4, tuple(np.full((159, 158, 3), 100, np.uint8) for _ in range(4))):
            with self.assertRaises(QuizError):
                corner_order(tiles)
        with self.assertRaises(QuizError):
            puzzle_state(puzzle(used=2, remaining=10))
        with self.assertRaises(QuizError):
            puzzle_state(puzzle(seconds=0))
        self.assertFalse(puzzle_visible(screen()))


class PuzzleRunnerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = MagicMock()
        self.runner = RingRunner(self.session, Path(self.tmp.name), lambda: False)
        self.runner.save_metrics = MagicMock()
        self.runner.save = MagicMock()
        self.now = time.monotonic()
        self.clock = patch("time.monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.runner.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        self.solver = PuzzleSolver(self.runner)

    def feed(self, *views):
        script = iter(views)
        last = views[-1]
        def read():
            nonlocal last
            last = next(script, last)
            # Real captures always return a new view and fresh input token.
            self.runner.view = DailyView(last.frame, last.blocks, self.now)
            return self.runner.view
        self.runner.read = read

    def test_success_clicks_two_tiles_then_returns_to_activity(self):
        self.feed(puzzle(), puzzle(), puzzle(), screen())
        with patch("puzzle_tasks.random.uniform", return_value=.5) as delay:
            returned = self.solver.run(puzzle())
        self.assertFalse(puzzle_visible(returned))
        self.assertEqual(delay.call_args_list, [unittest.mock.call(.3, 1.0)] * 2)
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list],
                               TILE_BOXES[1], TILE_BOXES[3])

    def test_success_transition_with_obscured_timer_waits_for_auto_close(self):
        solved = puzzle((0, 1, 2, 3), used=1)
        transition = DailyView(solved.frame, [b for b in solved.blocks if "秒" not in b.text])
        self.assertTrue(puzzle_visible(transition))
        with self.assertRaisesRegex(QuizError, "倒计时无法确认"):
            puzzle_state(transition)
        self.feed(puzzle(), puzzle(), puzzle(), transition, screen())
        returned = self.solver.run(puzzle())
        self.assertFalse(puzzle_visible(returned))
        self.assertEqual(self.session.click.call_count, 2)

    def test_unreadable_post_swap_state_times_out_without_more_clicks(self):
        current = puzzle((0, 1, 2, 3), used=1)
        unreadable = DailyView(current.frame, [b for b in current.blocks if "秒" not in b.text])
        self.feed(puzzle(), puzzle(), puzzle(), unreadable)
        started = self.now
        with self.assertRaisesRegex(QuizError, "倒计时无法确认"):
            self.solver.run(puzzle())
        self.assertGreaterEqual(self.now - started, 3)
        self.assertEqual(self.session.click.call_count, 2)

    def test_unreadable_timer_before_click_still_prevents_input(self):
        current = puzzle()
        unreadable = DailyView(current.frame, [b for b in current.blocks if "秒" not in b.text])
        self.feed(unreadable)
        with self.assertRaisesRegex(QuizError, "倒计时无法确认"):
            self.solver.run(current)
        self.session.click.assert_not_called()

    def test_stop_while_waiting_for_success_transition_prevents_more_input(self):
        current = puzzle((0, 1, 2, 3), used=1)
        transition = DailyView(current.frame, [b for b in current.blocks if "秒" not in b.text])
        self.feed(puzzle(), puzzle(), puzzle(), transition, screen())
        sleep = self.runner.sleep

        def wait(seconds):
            sleep(seconds)
            if self.session.click.call_count == 2:
                self.runner.should_stop = lambda: True
            self.runner.check_stop()

        self.runner.sleep = wait
        with self.assertRaises(DailyStopped):
            self.solver.run(puzzle())
        self.assertEqual(self.session.click.call_count, 2)

    def test_verifies_swap_before_solving_next_pair(self):
        start = puzzle((1, 0, 3, 2))
        middle = puzzle((0, 1, 3, 2), used=1)
        self.feed(start, start, start, middle, middle, middle, middle, screen())
        self.solver.run(start)
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list],
                               TILE_BOXES[0], TILE_BOXES[1], TILE_BOXES[2], TILE_BOXES[3])

    def test_three_swaps_finish_with_no_trial_inputs(self):
        first = puzzle((1, 2, 3, 0))
        second = puzzle((0, 2, 3, 1), used=2)
        third = puzzle((0, 1, 3, 2), used=4)
        self.feed(first, first, first, second, second, second, second,
                  third, third, third, third, screen())
        self.solver.run(first)
        self.assertEqual(self.session.click.call_count, 6)

    def test_portrait_replay_computes_and_verifies_three_swaps(self):
        first = puzzle((1, 3, 0, 2), artwork=PORTRAIT_SOLVED)
        second = puzzle((0, 3, 1, 2), used=2, artwork=PORTRAIT_SOLVED)
        third = puzzle((0, 1, 3, 2), used=4, artwork=PORTRAIT_SOLVED)
        self.feed(first, first, first, second, second, second, second,
                  third, third, third, third, screen())
        self.assertFalse(puzzle_visible(self.solver.run(first)))
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list],
                               TILE_BOXES[0], TILE_BOXES[2], TILE_BOXES[1], TILE_BOXES[2],
                               TILE_BOXES[2], TILE_BOXES[3])

    def test_palace_replay_computes_and_verifies_three_swaps(self):
        first = puzzle((3, 0, 1, 2), artwork=PALACE_SOLVED)
        second = puzzle((0, 3, 1, 2), used=2, artwork=PALACE_SOLVED)
        third = puzzle((0, 1, 3, 2), used=4, artwork=PALACE_SOLVED)
        self.feed(first, first, first, second, second, second, second,
                  third, third, third, third, screen())
        self.assertFalse(puzzle_visible(self.solver.run(first)))
        assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list],
                               TILE_BOXES[0], TILE_BOXES[1], TILE_BOXES[1], TILE_BOXES[2],
                               TILE_BOXES[2], TILE_BOXES[3])

    def test_scaled_correct_swaps_do_not_pause_verification(self):
        for (name, artwork), (width, height) in itertools.product(ARTWORKS, ((830, 466), (1600, 900))):
            with self.subTest(artwork=name, size=(width, height)):
                views = ()
                for used, order in enumerate(((2, 1, 3, 0), (0, 1, 3, 2))):
                    current = puzzle(order, used=used, artwork=artwork)
                    frame = np.array(Image.fromarray(current.frame).resize(
                        (width, height), Image.Resampling.BILINEAR))
                    blocks = [TextBlock(b.text, b.score,
                                        (b.box[0] * width / 834, b.box[1] * height / 470,
                                         b.box[2] * width / 834, b.box[3] * height / 470))
                              for b in current.blocks]
                    views = (*views, DailyView(frame, blocks))
                first, second = views
                self.session.click.reset_mock()

                def read():
                    current = (first, second, screen())[min(self.session.click.call_count // 2, 2)]
                    blocks = [TextBlock(b.text, b.score,
                                        (b.box[0] * current.width / 834, b.box[1] * current.height / 470,
                                         b.box[2] * current.width / 834, b.box[3] * current.height / 470))
                              for b in current.blocks]
                    self.runner.view = DailyView(current.frame, blocks, self.now)
                    return self.runner.view

                self.runner.read = read
                self.assertFalse(puzzle_visible(self.solver.run(first)))
                self.assertEqual(self.session.click.call_count, 4)
                boxes = tuple((x * width / 834, y * height / 470, w * width / 834, h * height / 470)
                              for x, y, w, h in (TILE_BOXES[i] for i in (0, 3, 2, 3)))
                assert_clicks_in_boxes(self, [call.args for call in self.session.click.call_args_list], *boxes)

    def test_ambiguous_or_extra_pale_edge_prevents_input(self):
        for pale_rows in (95, 159):
            edge = np.concatenate((np.full((pale_rows, 15, 3), (195, 225, 245), np.uint8),
                                   np.full((159 - pale_rows, 15, 3), 50, np.uint8)), axis=0)
            tile = np.concatenate((PORTRAIT_SOLVED[0][:, :-15], edge), axis=1)
            with self.subTest(pale_rows=pale_rows), self.assertRaises(QuizError):
                self.solver.run(puzzle(artwork=(tile, *PORTRAIT_SOLVED[1:])))
        self.session.click.assert_not_called()

    def test_stale_start_and_stop_during_pause_prevent_input(self):
        current = puzzle()
        current.captured_at = self.now - 11
        with self.assertRaises(QuizError):
            self.solver.run(current)
        self.runner.sleep = lambda seconds: setattr(self.runner, "should_stop", lambda: True)
        with self.assertRaises(DailyStopped):
            self.solver.run(puzzle())
        self.session.click.assert_not_called()

    def test_selected_tile_can_increment_the_operation_counter(self):
        self.feed(puzzle(), puzzle(used=1), puzzle(used=1), screen())
        self.solver.run(puzzle())
        self.assertEqual(self.session.click.call_count, 2)

    def test_no_confirmed_swap_is_never_retried(self):
        self.feed(puzzle())
        with self.assertRaisesRegex(QuizError, "交换未确认"):
            self.solver.run(puzzle())
        self.assertEqual(self.session.click.call_count, 2)

    def test_time_or_operation_budget_shortage_prevents_input(self):
        for current in (puzzle(used=9), puzzle(seconds=3)):
            with self.subTest():
                with self.assertRaisesRegex(QuizError, "不足"):
                    self.solver.run(current)
        self.session.click.assert_not_called()

    def test_changed_page_or_tiles_during_pause_prevents_input(self):
        for current in (screen(), puzzle((1, 0, 3, 2))):
            self.feed(current)
            with self.assertRaises(QuizError):
                self.solver.run(puzzle())
        self.session.click.assert_not_called()

    def test_stop_between_tile_clicks_prevents_second_input(self):
        self.feed(puzzle())
        self.runner.should_stop = lambda: self.session.click.called
        with self.assertRaises(DailyStopped):
            self.solver.run(puzzle())
        self.session.click.assert_called_once()

    def test_ring_continues_after_success_transition_without_reclicking_task(self):
        current = puzzle((0, 1, 2, 3), used=1)
        transition = DailyView(current.frame, [b for b in current.blocks if "秒" not in b.text])
        self.feed(puzzle(), puzzle(), puzzle(), puzzle(), transition, screen(count=100), screen(count=200))
        self.runner.answer_quiz = MagicMock()
        with patch.object(self.runner, "open_ring") as activity:
            result = self.runner.run()
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["progress"], 200)
        activity.assert_not_called()
        self.runner.answer_quiz.assert_not_called()
        self.assertEqual(self.session.click.call_count, 2)

    def test_ring_started_on_puzzle_skips_activity_and_quiz(self):
        self.feed(puzzle(), puzzle(), puzzle(), puzzle(), screen(), screen(), screen("battle"))
        self.runner.answer_quiz = MagicMock()
        with patch.object(self.runner, "open_ring") as activity:
            self.runner.run()
        activity.assert_not_called()
        self.runner.answer_quiz.assert_not_called()
        self.assertEqual(self.session.click.call_count, 2)


if __name__ == "__main__":
    unittest.main()
