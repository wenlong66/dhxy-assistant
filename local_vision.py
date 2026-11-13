"""Local-only ROI recognition, exact-pixel caching and bounded timing diagnostics.

No device input here. Each caller supplies a fresh frame; cache hits only avoid OCR,
never acquisition. A click/session/layout change must invalidate the cache.
"""
from __future__ import annotations

import math
import os
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass

import numpy as np

from quiz_core import QuizError


@dataclass(frozen=True)
class VisionOptions:
    mode: str = "roi"
    poll_seconds: float = 0.20
    cache_seconds: float = 1.0
    probe_seconds: float = 3.0
    early_probe: bool = True

    def __post_init__(self):
        if self.mode not in {"roi", "full"}:
            raise QuizError("DHXY_VISION_MODE must be roi or full.")
        for name, low, high in (("poll_seconds", .05, 2), ("cache_seconds", 0, 2),
                                ("probe_seconds", 1, 30)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise QuizError(f"Invalid vision option {name}: expected {low}..{high}.")
        if not isinstance(self.early_probe, bool):
            raise QuizError("early_probe must be a boolean.")

    @classmethod
    def from_env(cls):
        try:
            mode = os.environ.get("DHXY_VISION_MODE", "roi")
            early = os.environ.get("DHXY_EARLY_PROBE", "1")
            if early not in {"0", "1"}:
                raise ValueError("DHXY_EARLY_PROBE must be 0 or 1")
            return cls(mode=mode,
                       poll_seconds=float(os.environ.get("DHXY_POLL_SECONDS", "0.20")),
                       cache_seconds=float(os.environ.get("DHXY_CACHE_SECONDS", "1.0")),
                       probe_seconds=float(os.environ.get("DHXY_PROBE_SECONDS", "3.0")),
                       early_probe=early == "1")
        except ValueError as exc:
            raise QuizError(f"Invalid local vision configuration: {exc}") from exc


class VisionMetrics:
    """Bounded samples, exact totals; contains no screenshot, OCR text or secrets."""

    def __init__(self):
        self.counts = defaultdict(int)
        self.totals = defaultdict(float)
        self.samples = defaultdict(lambda: deque(maxlen=512))

    def count(self, name, amount=1):
        self.counts[name] += amount

    def duration(self, name, started):
        elapsed = max(0.0, time.perf_counter() - started) * 1000
        self.counts[name + "_calls"] += 1
        self.totals[name] += elapsed
        self.samples[name].append(elapsed)

    def report(self):
        timings = {}
        for name, values in self.samples.items():
            ordered = sorted(values)
            timings[name] = {
                "calls": self.counts[name + "_calls"],
                "total_ms": round(self.totals[name], 3),
                "mean_ms": round(self.totals[name] / self.counts[name + "_calls"], 3),
                "recent_p50_ms": round(ordered[math.ceil(len(ordered) * .5) - 1], 3),
                "recent_p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
            }
        return {"counters": dict(self.counts), "timings": timings,
                "percentile_window": 512}


def scaled_roi(roi, width, height, reference=(834, 470), padding=0):
    """Reference rectangle -> clipped device rectangle; x/y floor, end ceil."""
    if len(roi) != 4 or any(not math.isfinite(v) for v in roi):
        raise QuizError("Invalid recognition rectangle.")
    x, y, w, h = roi
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > reference[0] + 1 or y + h > reference[1] + 1:
        raise QuizError("Recognition rectangle is outside the calibrated viewport.")
    x1 = max(0, math.floor((x - padding) * width / reference[0]))
    y1 = max(0, math.floor((y - padding) * height / reference[1]))
    x2 = min(width, math.ceil((x + w + padding) * width / reference[0]))
    y2 = min(height, math.ceil((y + h + padding) * height / reference[1]))
    if x2 <= x1 or y2 <= y1:
        raise QuizError("Recognition rectangle has no pixels.")
    return x1, y1, x2 - x1, y2 - y1


def contains(outer, inner):
    x, y, w, h = outer
    a, b, c, d = inner
    return x <= a and y <= b and a + c <= x + w and b + d <= y + h


def color_fraction(frame, roi, lower, upper):
    """BGR range coverage; a primitive for calibrated multi-patch conditions."""
    x, y, w, h = roi
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
        raise QuizError("Color rectangle is outside the frame.")
    lo, hi = np.asarray(lower), np.asarray(upper)
    if lo.shape != (3,) or hi.shape != (3,) or not np.isfinite(lo).all() or not np.isfinite(hi).all() or (lo < 0).any() or (hi > 255).any() or (lo > hi).any():
        raise QuizError("Invalid BGR color range.")
    pixels = frame[y:y+h, x:x+w]
    return float(np.all((pixels >= lo) & (pixels <= hi), axis=2).mean())


class RegionOCR:
    """A per-run LRU with exact comparisons, fixed TTL and explicit invalidation.

    Cache entries require a textured crop and confident text. A negative/blank
    result is never reused across frames. TTL is measured from capture start and
    is NOT extended on a hit. No fuzzy hashes or scene guesses authorize a click.
    """

    def __init__(self, session, options, metrics, check_stop=lambda: None):
        self.session, self.options, self.metrics = session, options, metrics
        self.check_stop = check_stop
        self.entries = OrderedDict()
        self.shape = None
        self.generation = 0

    def invalidate(self):
        self.entries.clear()
        self.generation += 1
        self.metrics.count("cache_invalidations")

    def recognize(self, frame, roi, captured_at, *, single_line=False):
        self.check_stop()
        if self.shape != frame.shape:
            self.invalidate()
            self.shape = frame.shape
        x, y, w, h = roi
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
            raise QuizError("OCR rectangle is outside screenshot.")
        crop = frame[y:y+h, x:x+w]
        key = (tuple(roi), single_line)
        started = time.perf_counter()
        entry = self.entries.get(key)
        hit = False
        if entry is not None:
            pixels, blocks, timestamp = entry
            age = time.monotonic() - timestamp
            hit = (0 <= age <= self.options.cache_seconds and
                   self.options.cache_seconds > 0 and np.array_equal(crop, pixels))
        self.metrics.duration("pixel_compare", started)
        if hit:
            self.entries.move_to_end(key)
            self.metrics.count("ocr_cache_hits")
            self.check_stop()
            return list(blocks)
        self.entries.pop(key, None)
        self.metrics.count("ocr_pixels", w * h)
        self.metrics.count("ocr_cache_misses")
        started = time.perf_counter()
        try:
            if single_line:
                blocks = list(self.session.ocr(frame, roi, single_line=True))
            else:
                blocks = list(self.session.ocr(frame, roi))
        finally:
            self.metrics.duration("ocr", started)
        self.check_stop()
        # Avoid caching black/flat captures and low-confidence or empty OCR.
        confident = blocks and all(math.isfinite(b.score) and b.score >= .8 for b in blocks)
        if self.options.cache_seconds > 0 and confident and float(crop.std()) > 2:
            self.entries[key] = (crop.copy(), tuple(blocks), captured_at)
            while len(self.entries) > 32:
                self.entries.popitem(last=False)
        return blocks
