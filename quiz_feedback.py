"""Pure, fail-closed recognition of a revealed correct-option tick in BGR pixels."""
from __future__ import annotations

from numbers import Integral

import numpy as np


_BUTTON_SIZE = (235, 50)
# shortcut: synthetic tick geometry; calibrate against feedback crops when available.
_TICK_POINTS = ((0.0, .55), (.40, .96), (1.0, 0.0))
_TICK_RADIUS = .10


def _resize(pixels, width, height):
    ys = np.minimum((np.arange(height) * pixels.shape[0] / height).astype(int), pixels.shape[0] - 1)
    xs = np.minimum((np.arange(width) * pixels.shape[1] / width).astype(int), pixels.shape[1] - 1)
    return pixels[ys[:, None], xs]


def _components(mask):
    remaining = frozenset(zip(*np.nonzero(mask)))
    while remaining:
        first = next(iter(remaining))
        remaining = remaining - {first}
        pending, points = (first,), (first,)
        while pending:
            y, x = pending[-1]
            pending = pending[:-1]
            neighbors = {(y + dy, x + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)} & remaining
            remaining = remaining - neighbors
            pending = pending + tuple(neighbors)
            points = points + tuple(neighbors)
        yield np.array(points)


def _is_tick(mask):
    height, width = mask.shape
    if not (10 <= width <= 28 and 16 <= height <= 34 and .35 <= width / height <= 1.25):
        return False
    if not .13 <= mask.mean() <= .55:
        return False
    observed = _resize(mask, 18, 24)
    ys, xs = np.nonzero(observed)
    left, bend, right = (ys[xs < 4], ys[(xs >= 5) & (xs < 10)], ys[xs >= 14])
    if any(len(part) < 3 for part in (left, bend, right)):
        return False
    ly, by, ry = (float(np.median(part)) / 23 for part in (left, bend, right))
    if not (.35 <= ly <= .8 and by - ly >= .15 and by - ry >= .45 and ry <= .35):
        return False
    yy, xx = np.meshgrid(np.linspace(0, 1, 24), np.linspace(0, 1, 18), indexing="ij")
    distances = ()
    for (ax, ay), (bx, by) in zip(_TICK_POINTS, _TICK_POINTS[1:]):
        dx, dy = bx - ax, by - ay
        projection = np.clip(((xx - ax) * dx + (yy - ay) * dy) / (dx * dx + dy * dy), 0, 1)
        distances = distances + (np.hypot(xx - ax - projection * dx, yy - ay - projection * dy),)
    template = np.minimum(*distances) <= _TICK_RADIUS
    overlap = np.count_nonzero(observed & template)
    return bool(overlap / observed.sum() >= .70 and overlap / template.sum() >= .50)


def correct_choice(frame, button: tuple[int, int, int, int]) -> bool:
    """Recognize a green/light-teal tick on this button, including gold reveals.

    Association, pre-submit transition checks and device stopping belong to the caller.
    Invalid images/rectangles and ambiguous shapes return False without changing pixels.
    """
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3:
        return False
    if frame.shape[2] != 3 or min(frame.shape[:2]) == 0:
        return False
    if not isinstance(button, (tuple, list)) or len(button) != 4:
        return False
    if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral) for value in button):
        return False
    x, y, width, height = map(int, button)
    if min(x, y) < 0 or width < 40 or height < 16 or not 3 <= width / height <= 6:
        return False
    if x + width > frame.shape[1] or y + height > frame.shape[0]:
        return False
    pixels = _resize(frame[y:y + height, x:x + width], *_BUTTON_SIZE)[5:45, 188:233].astype(float)
    blue, green, red = (pixels[:, :, channel] for channel in range(3))
    chroma = green - (blue + red) / 2
    # Relative contrast keeps a dimmed tick visible but excludes a flat teal body.
    green_like = (green >= 35) & (green - red >= 8) & (green - blue >= -8)
    contrast = (chroma - np.median(chroma) >= 8) | (green - np.median(green) >= 12)
    for points in _components(green_like & contrast):
        if len(points) < 18:
            continue
        top, left = points.min(axis=0)
        bottom, right = points.max(axis=0) + 1
        coordinates = frozenset(map(tuple, points))
        component = np.array([[(row, col) in coordinates
                               for col in range(left, right)] for row in range(top, bottom)])
        if _is_tick(component):
            return True
    return False
