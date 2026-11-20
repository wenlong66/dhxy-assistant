"""Offline synthetic feedback pixels; no screenshot, account data or device input."""
import unittest

import numpy as np
from PIL import Image

from quiz_feedback import correct_choice


BUTTON = (36, 40, 235, 50)
GOLD = (122, 204, 240)
TEAL = (145, 175, 110)
GREEN = (155, 230, 105)
# Synthetic tick, not a crop from a verified game feedback screenshot.
TICK = np.array([[c == "#" for c in row] for row in (
    ".................#",
    "................##",
    "................##",
    "...............###",
    "...............##.",
    "..............###.",
    "..............##..",
    ".............###..",
    ".............##...",
    "............###...",
    "............##....",
    "...........###....",
    "##.........##.....",
    "###.......###.....",
    ".###......##......",
    "..###....###......",
    "...###...##.......",
    "....###.###.......",
    ".....#####........",
    "......####........",
    "......###.........",
    ".......#..........",
)], dtype=bool)


def scene(glyph=TICK, *, body=TEAL, color=GREEN, position=(211, 11)):
    y, x = np.mgrid[:140, :320]
    bx, by, width, height = BUTTON
    inside = (x >= bx) & (x < bx + width) & (y >= by) & (y < by + height)
    background = np.where(inside[..., None], body, (45, 45, 45)).astype(np.uint8)
    if glyph is None:
        return background
    gx, gy = bx + position[0], by + position[1]
    mask = np.pad(glyph, ((gy, 140 - gy - glyph.shape[0]),
                          (gx, 320 - gx - glyph.shape[1])))
    return np.where(mask[..., None], color, background).astype(np.uint8)


def scaled(frame, factor):
    height, width = frame.shape[:2]
    ys = np.minimum((np.arange(round(height * factor)) / factor).astype(int), height - 1)
    xs = np.minimum((np.arange(round(width * factor)) / factor).astype(int), width - 1)
    return frame[ys[:, None], xs]


class QuizFeedbackTests(unittest.TestCase):
    def test_green_tick_on_teal_and_gold_buttons_at_multiple_scales(self):
        for body in (TEAL, GOLD):
            for factor in (.75, 1, 1.5, 2):
                with self.subTest(body=body, factor=factor):
                    button = tuple(round(value * factor) for value in BUTTON)
                    self.assertIs(correct_choice(scaled(scene(body=body), factor), button), True)

    def test_interpolated_tick_at_multiple_scales(self):
        for factor in (.5, .75, 1.25, 2):
            for body in (TEAL, GOLD):
                with self.subTest(factor=factor, body=body):
                    source = Image.fromarray(scene(body=body))
                    frame = np.asarray(source.resize((round(320 * factor), round(140 * factor)),
                                                     Image.Resampling.BILINEAR))
                    button = tuple(round(value * factor) for value in BUTTON)
                    self.assertIs(correct_choice(frame, button), True)

    def test_tick_under_dim_overlay_and_on_gradient_body(self):
        for brightness in (.35, .6, .85):
            with self.subTest(brightness=brightness):
                frame = (scene(body=GOLD) * brightness).astype(np.uint8)
                self.assertIs(correct_choice(frame, BUTTON), True)
        frame = scene()
        shade = np.linspace(.8, 1, frame.shape[0])[:, None, None]
        self.assertIs(correct_choice((frame * shade).astype(np.uint8), BUTTON), True)

    def test_plain_gold_teal_green_and_gray_are_not_feedback(self):
        for body in (TEAL, GOLD, GREEN, (120, 120, 120)):
            with self.subTest(body=body):
                self.assertIs(correct_choice(scene(None, body=body), BUTTON), False)

    def test_cross_bars_blobs_and_mirrored_tick_are_rejected(self):
        y, x = np.mgrid[:24, :20]
        cross = (np.abs(y / 23 - x / 19) < .12) | (np.abs(y / 23 + x / 19 - 1) < .12)
        blob = ((x - 10) / 8) ** 2 + ((y - 12) / 10) ** 2 < 1
        vertical = (x > 8) & (x < 12)
        horizontal = (y > 10) & (y < 14)
        for glyph in (cross, blob, vertical, horizontal, np.ones((24, 20), bool), TICK[:, ::-1]):
            for color in (GREEN, (70, 65, 230)):
                with self.subTest(shape=glyph.shape, color=color):
                    self.assertIs(correct_choice(scene(glyph, color=color), BUTTON), False)

    def test_tick_on_other_button_or_away_from_right_edge_is_rejected(self):
        frame = scene()
        self.assertIs(correct_choice(frame, (36, 95, 235, 40)), False)
        self.assertIs(correct_choice(scene(position=(30, 11)), BUTTON), False)
        self.assertIs(correct_choice(scene(position=(211, -25)), BUTTON), False)

    def test_invalid_frames_and_rectangles_fail_closed(self):
        frame = scene()
        for invalid in (None, [], "image", np.empty((0, 0, 3), np.uint8),
                        frame[:, :, 0], frame[:, :, :2], np.zeros((140, 320, 4), np.uint8),
                        frame.astype(float), frame.astype(np.uint16), np.zeros((1, 140, 320, 3), np.uint8)):
            with self.subTest(frame_type=type(invalid)):
                self.assertIs(correct_choice(invalid, BUTTON), False)
        for button in (None, (), (36, 40, 235), (36, 40, 235, 50, 1),
                       (-1, 40, 235, 50), (36, -1, 235, 50), (36, 40, 0, 50),
                       (36, 40, 235, -1), (100, 40, 235, 50), (36, 100, 235, 50),
                       (36.0, 40, 235, 50), (True, 40, 235, 50),
                       (36, 40, "235", 50), (36, 40, 235, float("nan"))):
            with self.subTest(button=button):
                self.assertIs(correct_choice(frame, button), False)

    def test_readonly_input_is_unchanged_and_numpy_integer_bounds_work(self):
        source = scene().tobytes()
        frame = np.frombuffer(source, np.uint8).reshape(140, 320, 3)
        self.assertIs(correct_choice(frame, tuple(np.int64(value) for value in BUTTON)), True)
        self.assertEqual(frame.tobytes(), source)


if __name__ == "__main__":
    unittest.main()
