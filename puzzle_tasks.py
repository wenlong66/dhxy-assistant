"""Calibrated 2x2 click-swap puzzle; no trial swaps or completion guesses."""
from __future__ import annotations

import random
import re
import time

import numpy as np

from daily_tasks import compact
from local_vision import scaled_roi
from quiz_core import QuizError
from quiz_vision import INPUT_PAUSE_SECONDS

BOARD = (368, 101, 316, 318)
PUZZLE_INFO = (120, 165, 230, 210)
TILE_BOXES = tuple((BOARD[0] + col * 158, BOARD[1] + row * 159, 158, 159)
                  for row in range(2) for col in range(2))
CORNERS = ((True, False, False, True), (True, True, False, False),
           (False, False, True, True), (False, True, True, False))
MAX_OPERATIONS = 10
EDGE_BAND = 15
EDGE_MARGIN = 20
FADE_MINIMUM = .7
FADE_MAXIMUM = .3


def puzzle_visible(view):
    text = view.text(PUZZLE_INFO)
    return "拼图" in text and "操作规则" in text and "当前进度" in text


def puzzle_state(view):
    if not puzzle_visible(view):
        raise QuizError("未确认拼图界面，未点击。")
    text = compact(view.text(PUZZLE_INFO))
    counts = re.search(r"已操作[:：](\d+)次剩余[:：](\d+)次", text)
    timer = re.search(r"(\d+)秒", view.text((120, 108, 240, 45)))
    if "第1关/共1关" not in text or not counts or not timer:
        raise QuizError("拼图关卡、操作次数或倒计时无法确认，未点击。")
    used, remaining, seconds = int(counts[1]), int(counts[2]), int(timer[1])
    if used + remaining != MAX_OPERATIONS or not 0 <= used <= MAX_OPERATIONS or seconds <= 0:
        raise QuizError("拼图次数或倒计时异常，未点击。")
    return used, remaining, seconds


def puzzle_tiles(view):
    crops = tuple(view.frame[y:y + h, x:x + w]
                  for x, y, w, h in (scaled_roi(box, view.width, view.height) for box in TILE_BOXES))
    ys = (np.arange(159) * crops[0].shape[0] / 159).astype(int)
    xs = (np.arange(158) * crops[0].shape[1] / 158).astype(int)
    return tuple(crop[np.minimum(ys, crop.shape[0] - 1)[:, None],
                      np.minimum(xs, crop.shape[1] - 1)] for crop in crops)


def corner_order(tiles):
    if len(tiles) != 4 or any(tile.shape != (159, 158, 3) for tile in tiles):
        raise QuizError("拼图尺寸与已校准的2×2布局不符。")
    order = ()
    for tile in tiles:
        green, red = (tile[:, :, i].astype(int) for i in (1, 2))
        # shortcut: this artwork has two pale exterior edges per tile; other styles need calibration.
        fade = (red > 180) & (green > 150) & (tile.max(axis=2).astype(int) - tile.min(axis=2) < 80)
        m, n = EDGE_MARGIN, EDGE_BAND
        scores = tuple(float(edge.mean()) for edge in
                       (fade[:n, m:-m], fade[m:-m, -n:], fade[-n:, m:-m], fade[m:-m, :n]))
        if any(FADE_MAXIMUM < score < FADE_MINIMUM for score in scores):
            raise QuizError("拼图外边缘不明确，未尝试交换。")
        exterior = tuple(score >= FADE_MINIMUM for score in scores)
        if exterior not in CORNERS:
            raise QuizError("拼图不是已校准的四角图片，未尝试交换。")
        order = (*order, CORNERS.index(exterior))
    if len(set(order)) != 4:
        raise QuizError("拼图角位无法唯一确认，未尝试交换。")
    return order


def next_swap(order):
    if len(order) != 4 or set(order) != set(range(4)):
        raise QuizError("无效的拼图排列。")
    return next(((slot, order.index(slot)) for slot, tile in enumerate(order) if slot != tile), None)


def same_tiles(before, after):
    m = EDGE_MARGIN
    return all(a.shape == b.shape and float(np.abs(a[m:-m, m:-m].astype(float) -
                                                  b[m:-m, m:-m].astype(float)).mean()) < 3
               for a, b in zip(before, after))


class PuzzleSolver:
    def __init__(self, runner):
        self.runner = runner

    def tap_tile(self, view, index, before, used):
        self.runner.check_stop()
        self.runner._check_tap_view(view)
        self.runner.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.runner.check_stop()
        current = self.runner.read()
        fresh_used, _, seconds = puzzle_state(current)
        if ((current.width, current.height) != (view.width, view.height) or fresh_used != used
                or seconds < 2 or not same_tiles(before, puzzle_tiles(current))):
            raise QuizError("拼图或操作次数在等待期间变化，未点击。")
        self.runner.tap(current, TILE_BOXES[index], urgent=True)

    def verify_swap(self, before, pair, used):
        expected = tuple(before[pair[1]] if i == pair[0] else before[pair[0]] if i == pair[1] else tile
                         for i, tile in enumerate(before))
        deadline = time.monotonic() + 3
        while True:
            current = self.runner.read()
            if not puzzle_visible(current):
                return current
            fresh_used, _, _ = puzzle_state(current)
            tiles = puzzle_tiles(current)
            if fresh_used > used and fresh_used <= used + 2 and same_tiles(expected, tiles):
                return current
            if time.monotonic() >= deadline:
                raise QuizError("拼图交换未确认，已暂停，未重复点击或试错。")
            self.runner.sleep(self.runner.options.poll_seconds)

    def run(self, view):
        for _ in range(3):
            used, remaining, seconds = puzzle_state(view)
            tiles = puzzle_tiles(view)
            pair = next_swap(corner_order(tiles))
            if pair is None:
                return self.runner.until(lambda v: not puzzle_visible(v), "拼图已复原但页面仍未关闭，请人工核对。", 5)
            if remaining < 2 or seconds < 4:
                raise QuizError("拼图剩余次数或时间不足，未尝试交换。")
            self.runner.emit(f"识别到2×2拼图，交换第{pair[0] + 1}块与第{pair[1] + 1}块。")
            self.tap_tile(view, pair[0], tiles, used)
            current = self.runner.read()
            selected_used, _, _ = puzzle_state(current)
            if not used <= selected_used <= used + 1 or not same_tiles(tiles, puzzle_tiles(current)):
                raise QuizError("选择第一块后拼图已变化，未点击第二块。")
            self.tap_tile(current, pair[1], tiles, selected_used)
            view = self.verify_swap(tiles, pair, used)
            if not puzzle_visible(view):
                self.runner.emit("拼图界面已退出，交回活动流程继续检查。")
                return view
        if next_swap(corner_order(puzzle_tiles(view))) is not None:
            raise QuizError("拼图交换3次后仍未复原，未继续试错。")
        return self.runner.until(lambda v: not puzzle_visible(v), "拼图页面仍未关闭，请人工核对。", 5)
