"""Calibrated choice-popup OCR; submission and answer selection stay with answer_once."""
from __future__ import annotations

import re
import time

from maa_adapter import ROOT
from quiz_core import Question, QuizError, digest, game_day, normalize
from quiz_vision import Layout, Observation, QuizReader, ReadError, join_blocks, option_labels, parse_fields


class RingQuizReader:
    def __init__(self, session, scope):
        self.session, self.scope = session, scope
        self.layout = Layout.load(ROOT / "config/ring.layout.json")

    def read(self) -> Observation:
        try:
            frame = self.session.capture()
            captured_at = time.monotonic()
            height, width = frame.shape[:2]
            regions = self.layout.scaled(width, height)
            fields = {name: self.session.ocr(frame, roi, single_line=name.startswith("label_"))
                      for name, roi in regions.items()}
            return self._observation(fields, regions, (width, height), captured_at, frame)
        except QuizError as exc:
            raise ReadError(str(exc)) from exc

    def _observation(self, fields, regions, size, captured_at, frame):
        labels = option_labels(fields, self.layout.minimum)
        names = ["question", *[f"label_{k}" for k in labels], *[f"option_{k}" for k in labels]]
        text = {name: join_blocks(fields.get(name, []), self.layout.minimum) for name in names}
        for name in names:
            x, y, w, h = regions[name]
            if any(not (x <= b.box[0] and y <= b.box[1] and b.box[0] + b.box[2] <= x + w
                        and b.box[1] + b.box[3] <= y + h) for b in fields[name]):
                raise QuizError(f"OCR region {name} contains misaligned/clipped text; inspect the layout.")
        for label in labels:
            if normalize(text[f"label_{label}"]).upper() != label:
                raise QuizError(f"Option {label} label is missing/misaligned; inspect the layout.")
        options = tuple((k, re.sub(r"\s*\n\s*", "", text[f"option_{k}"])) for k in labels)
        # shortcut: identical questions share a daily instance; add presentation tracking for confirmed replays.
        instance = digest([game_day(), self.scope, self.session.address, normalize(text["question"]),
                           sorted(normalize(value) for _, value in options)])
        question = Question(text["question"], options, instance, scope=self.scope)
        buttons = self.layout.scaled(*size, group="buttons")
        return Observation(question, size, {k: buttons[k] for k in labels}, captured_at, frame,
                           regions["question"], {k: regions[f"option_{k}"] for k in labels})


class UnifiedQuizReader:
    def __init__(self, session, layout, scope):
        self.dali = QuizReader(session, layout, scope)
        self.popup = RingQuizReader(session, scope)

    def read(self):
        frame, captured, fields, _ = self.dali.inspect()
        try:
            return parse_fields(fields, self.dali.layout, (frame.shape[1], frame.shape[0]),
                                self.dali.session.address, self.dali.scope, captured, frame)
        except QuizError:
            regions = self.popup.layout.scaled(frame.shape[1], frame.shape[0])
            fields = {name: self.popup.session.ocr(frame, roi, single_line=name.startswith("label_"))
                      for name, roi in regions.items()}
            try:
                return self.popup._observation(fields, regions, (frame.shape[1], frame.shape[0]), captured, frame)
            except QuizError as exc:
                raise ReadError(str(exc)) from exc
