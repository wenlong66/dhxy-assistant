"""Calibrated choice-popup OCR; submission and answer selection stay with answer_once."""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import replace

import numpy as np

from maa_adapter import ROOT
from local_vision import contains, scaled_roi
from quiz_core import Question, QuizError, digest, game_day, normalize
from quiz_vision import (UNREADABLE_CHARACTERS, Layout, Observation, QuestionReadError, QuizReader, ReadError, join_blocks,
                         join_question_blocks, option_labels, partial_option_buttons_confirmed, question_icon_visible,
                         read_question_blocks, recheck_for_answer)

QUESTION_ICON = (185, 146, 24, 32)
QUESTION_TEXT_MAX_BRIGHTNESS = 128
QUESTION_TEXT_MIN_CLUSTER_PIXELS = 2


def question_image_token(frame, roi, icon_roi, blocks):
    if not isinstance(frame, np.ndarray):
        return ""
    x, y, w, h = roi
    ix, iy, iw, ih = icon_roi
    pixels = frame[y:y + h, x:x + w]
    crosses_icon = any(not contains(icon_roi, b.box) and b.box[0] < ix + iw and b.box[0] + b.box[2] > ix
                       and b.box[1] < iy + ih and b.box[1] + b.box[3] > iy for b in blocks)
    if question_icon_visible(frame, icon_roi) and not crosses_icon:
        yy, xx = np.indices(pixels.shape[:2])
        outside_icon = (xx + x < ix) | (xx + x >= ix + iw) | (yy + y < iy) | (yy + y >= iy + ih)
        pixels = pixels[outside_icon]
    return hashlib.sha256(pixels.tobytes()).hexdigest()


def question_submission_image_token(frame, roi, icon_roi):
    # Background noise must not create a new submission identity.
    x, y, w, h = roi
    ix, iy, iw, ih = icon_roi
    pixels = frame[y:y + h, x:x + w]
    dark = pixels.mean(axis=2) < QUESTION_TEXT_MAX_BRIGHTNESS
    if question_icon_visible(frame, icon_roi):
        yy, xx = np.indices(dark.shape)
        dark = dark & ~((xx + x >= ix) & (xx + x < ix + iw) & (yy + y >= iy) & (yy + y < iy + ih))
    padded = np.pad(dark.astype(np.uint8), 1)
    neighbors = sum(padded[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3))
    strokes = dark & (neighbors >= QUESTION_TEXT_MIN_CLUSTER_PIXELS)
    return hashlib.sha256(strokes.tobytes()).hexdigest()


class RingQuizReader:
    def __init__(self, session, scope):
        self.session, self.scope = session, scope
        self.layout = Layout.load(ROOT / "config/ring.layout.json")
        self.retry_token = None
        self.retry_instance = None
        self.previous_question = None
        self.previous_observation = None

    def confirm_retry(self):
        """Called only after a calibrated failed-answer dialogue was dismissed and recaptured."""
        self.retry_token = uuid.uuid4().hex
        self.retry_instance = None

    def read(self) -> Observation:
        try:
            frame = self.session.capture()
            captured_at = time.monotonic()
            height, width = frame.shape[:2]
            regions = self.layout.scaled(width, height)
            fields = self.read_fields(frame, regions, (width, height))
            return self._observation(fields, regions, (width, height), captured_at, frame)
        except QuestionReadError:
            raise
        except QuizError as exc:
            raise ReadError(str(exc)) from exc

    def read_fields(self, frame, regions, size):
        icon_roi = scaled_roi(QUESTION_ICON, *size)
        return {name: (read_question_blocks(self.session, frame, roi, self.layout.minimum, icon_roi)
                       if name == "question" else
                       self.session.ocr(frame, roi, single_line=name.startswith("label_")))
                for name, roi in regions.items()}

    def _observation(self, fields, regions, size, captured_at, frame):
        labels = option_labels(fields, self.layout.minimum)
        names = ["question", *[f"label_{k}" for k in labels], *[f"option_{k}" for k in labels]]
        icon_roi = scaled_roi(QUESTION_ICON, *size)
        text = {}
        unreadable_options = []
        question_error = None
        for name in names:
            try:
                blocks = fields.get(name, [])
                text[name] = (join_question_blocks(blocks, self.layout.minimum, frame, icon_roi)
                              if name == "question" else join_blocks(blocks, self.layout.minimum))
                if name.startswith("option_") and ("*" in text[name] or UNREADABLE_CHARACTERS.search(text[name])):
                    raise QuizError("Option contains unrecognized characters.")
            except QuizError as exc:
                if name.startswith("option_") and isinstance(frame, np.ndarray):
                    label = name[-1]
                    unreadable_options.append(label)
                    text[name] = f"*{label}*"
                    continue
                if name != "question":
                    raise QuizError(f"OCR 区域 {name} 未通过：{exc}") from exc
                question_error = f"OCR 区域 question 未通过：{exc}"
                text[name] = (join_question_blocks(blocks, self.layout.minimum, frame, icon_roi,
                                                   mask_unreadable=True) if blocks else "*")
        if unreadable_options and (frame.shape != (size[1], size[0], 3) or not partial_option_buttons_confirmed(
                frame, {k: self.layout.scaled(*size, group="buttons")[k] for k in labels})):
            raise QuizError("无法确认选项按钮画面，未答题。")
        for name in names:
            x, y, w, h = regions[name]
            if any(not (x <= b.box[0] and y <= b.box[1] and b.box[0] + b.box[2] <= x + w
                        and b.box[1] + b.box[3] <= y + h) for b in fields.get(name, [])):
                raise QuizError(f"OCR region {name} contains misaligned/clipped text; inspect the layout.")
        for label in labels:
            if normalize(text[f"label_{label}"]).upper() != label:
                raise QuizError(f"Option {label} label is missing/misaligned; inspect the layout.")
        options = tuple((k, re.sub(r"\s*\n\s*", "", text[f"option_{k}"])) for k in labels)
        if len({normalize(value) for _, value in options}) != len(options):
            raise QuizError("Options must be distinguishable; recheck OCR.")
        stem = text["question"]
        pixels = question_image_token(frame, regions["question"], icon_roi, fields.get("question", []))
        visual_token = digest([game_day(), self.scope, self.session.address, pixels]) if pixels else ""
        previous = self.previous_question
        if previous is not None and previous.matches_content(stem, options):
            stem, options = previous.stem, previous.options
            if not previous.is_readable:
                question_error = question_error or "OCR 区域 question 未通过：沿用本题随机兜底。"
        if (previous is not None and visual_token and previous.visual_token == visual_token
                and previous.matches_content(previous.stem, options)
                and (question_error is not None or not previous.is_readable)):
            stem, options = previous.stem, previous.options
            question_error = question_error or "本题题干此前未读清，沿用随机兜底。"
        if question_error is not None and not visual_token:
            raise QuizError(question_error)
        question = Question(stem, options, visual_token or digest([stem, options]), scope=self.scope,
                            is_readable=question_error is None and not unreadable_options, visual_token=visual_token,
                            unreadable_options=tuple(unreadable_options))
        buttons = self.layout.scaled(*size, group="buttons")
        observation = Observation(question, size, {k: buttons[k] for k in labels}, captured_at, frame,
                                  regions["question"], {k: regions[f"option_{k}"] for k in labels},
                                  question_icon_roi=icon_roi)
        if self.previous_observation is not None:
            comparable = replace(observation, question=replace(
                question, instance_id=self.previous_observation.question.instance_id))
            try:
                observation = recheck_for_answer(self.previous_observation, comparable)
            except ReadError:
                pass
        question = observation.question
        # shortcut: identical popups share a daily instance until a calibrated failure confirms a retry.
        instance = digest([game_day(), self.scope, self.session.address,
                           normalize(question.stem).replace("*", "") or question.visual_token,
                           sorted(normalize(value) for _, value in question.options)])
        if self.retry_token is not None:
            if self.retry_instance is None:
                self.retry_instance = instance
            if instance == self.retry_instance:
                instance = digest([instance, self.retry_token])
            else:
                self.retry_token = self.retry_instance = None
        question = replace(question, instance_id=instance,
                           visual_instance_id=digest([
                               game_day(), self.scope, self.session.address,
                               question_submission_image_token(frame, regions["question"], icon_roi), self.retry_token,
                           ]) if question.visual_token else "")
        observation = replace(observation, question=question)
        self.previous_question, self.previous_observation = question, observation
        if not question.is_readable:
            raise QuestionReadError(question_error or "OCR 区域 question 未通过：题干识别不稳定，随机兜底。",
                                    observation)
        return observation


class UnifiedQuizReader:
    def __init__(self, session, layout, scope):
        self.dali = QuizReader(session, layout, scope)
        self.popup = RingQuizReader(session, scope)

    def confirm_retry(self):
        self.popup.confirm_retry()

    def read(self):
        frame, captured, fields, _ = self.dali.inspect()
        try:
            return self.dali.parse(fields, (frame.shape[1], frame.shape[0]), captured, frame)
        except QuestionReadError:
            raise
        except QuizError:
            regions = self.popup.layout.scaled(frame.shape[1], frame.shape[0])
            fields = self.popup.read_fields(frame, regions, (frame.shape[1], frame.shape[0]))
            try:
                return self.popup._observation(fields, regions, (frame.shape[1], frame.shape[0]), captured, frame)
            except QuestionReadError:
                raise
            except QuizError as exc:
                raise ReadError(str(exc)) from exc
