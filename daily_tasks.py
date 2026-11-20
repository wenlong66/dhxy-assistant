"""Screenshot-calibrated daily pickup and completion checks; no AI or combat input."""
from __future__ import annotations

import logging
import math
import random
import re
import time
from dataclasses import asdict, dataclass

from maa_adapter import save_image
from local_vision import RegionOCR, VisionMetrics, VisionOptions, contains, scaled_roi
from quiz_core import QuizError, normalize
from quiz_vision import INPUT_PAUSE_SECONDS, TextBlock, join_blocks, random_click_point, write_json

TOTALS = {"帮派任务": 20, "五环任务": 1, "师门任务": 20, "宝图任务": 15, "卓越竞技场": 3, "大理寺答题": 10}
WAIT_SECONDS = {"帮派任务": 420, "五环任务": 180, "师门任务": 540, "宝图任务": 300}
PICKUPS = {"帮派任务": ("帮派总管", "帮派任务"),
           "五环任务": ("云游大师", "我自己去拯救苍生！"),
           "师门任务": ("聂小倩", "师门任务"),
           "宝图任务": ("店小二", "宝图任务"),
           "大理寺答题": ("大理寺官员", "我要参加有奖问答")}
TABLE = (250, 58, 462, 282)
DIALOG = (530, 160, 280, 300)
TRACKER = (677, 104, 151, 104)
TASK_INTERVAL_MIN_SECONDS = 10
DEFAULT_TASK_INTERVAL_MAX_SECONDS = 300


def compact(text):
    return re.sub(r"\s+", "", normalize(text))


@dataclass(frozen=True)
class Card:
    name: str
    current: int | None
    total: int
    action: str
    button: tuple | None

    @property
    def done(self):
        return not self.action or self.current == self.total


class DailyView:
    def __init__(self, frame, blocks, captured_at=None):
        self.frame = frame
        self.height, self.width = frame.shape[:2]
        if abs((self.width / self.height) / (834 / 470) - 1) > 0.025:
            raise QuizError("日常画面比例与已校准截图不符，请使用 16:9 横屏。")
        self.captured_at = time.monotonic() if captured_at is None else captured_at
        self.blocks = [TextBlock(b.text, b.score,
                                (b.box[0] * 834 / self.width, b.box[1] * 470 / self.height,
                                 b.box[2] * 834 / self.width, b.box[3] * 470 / self.height))
                       for b in blocks if math.isfinite(b.score) and b.score >= 0.8
                       and len(b.box) == 4 and min(b.box[2:]) > 0]

    def inside(self, roi):
        x, y, w, h = roi
        return [b for b in self.blocks if x <= b.box[0] + b.box[2] / 2 < x + w
                and y <= b.box[1] + b.box[3] / 2 < y + h]

    def text(self, roi):
        return "".join(compact(b.text) for b in sorted(self.inside(roi), key=lambda b: (b.box[1], b.box[0])))

    def find(self, text, roi):
        matches = [b for b in self.inside(roi) if compact(b.text).rstrip("!！") == compact(text).rstrip("!！")]
        if len(matches) != 1:
            raise QuizError(f"无法唯一识别「{text}」，已暂停，请检查日常诊断截图。")
        return matches[0].box

    @property
    def sweep_panel(self):
        return "金扫帚" in self.text((260, 350, 75, 40)) and "返回" in self.text((700, 430, 65, 38))

    @property
    def activity_panel(self):
        return "本日经验" in self.text((125, 55, 95, 35)) and "日常" in self.text((716, 85, 45, 65))

    @property
    def dig_popup(self):
        text = self.text((230, 130, 375, 60))
        return "宝图任务已完成" in text and "是否开始挖宝" in text

    @property
    def broom_guide(self):
        text = self.text((200, 410, 470, 40))
        return "用了金扫帚" in text and "日常不用愁" in text

    def tracking(self):
        blocks = self.inside(TRACKER)
        if not blocks:
            return None
        for line in join_blocks(blocks, 0.8).splitlines():
            text = compact(line)
            if text.startswith("帮派悬赏"):
                continue
            for name in WAIT_SECONDS:
                match = re.fullmatch(re.escape(name[:2]) + r"[^()/]{0,35}\(([1-9]\d*)/([1-9]\d*)\)", text)
                if match and int(match[1]) <= int(match[2]):
                    return name
        return None

    def go_button_visible(self, left, top):
        x1, x2 = (round(x * self.width / 834) for x in (left + 165, left + 228))
        y1, y2 = (round(y * self.height / 470) for y in (top + 23, top + 72))
        pixels = self.frame[y1:y2, x1:x2]
        blue, green, red = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        return ((green > 100) & (green > red * 1.25) & (blue > red * 1.1)).mean() > 0.12

    def cards(self):
        if not self.sweep_panel or self.dig_popup:
            raise QuizError("请打开活动 → 日常 → 右下角金扫帚任务列表。")
        cards = []
        for name, total in TOTALS.items():
            if name == "大理寺答题" and not any(compact(b.text).rstrip("!！") == name for b in self.inside(TABLE)):
                continue
            title = self.find(name, TABLE)
            left = 250 if title[0] < 480 else 483
            if title[1] + 72 > TABLE[1] + TABLE[3]:
                # Completed cards move to the bottom, where the broom panel can hide them.
                cards.append((title[1], left, Card(name, None, total, "", None)))
                continue
            roi = (left, title[1] - 4, 229, 76)
            # Restrict the count to its own line, excluding the activity score beneath it.
            match = re.search(r"(\d+)/(\d+)", self.text((left + 53, title[1] + 23, 113, 22)))
            current = int(match[1]) if match and int(match[2]) == total and 0 <= int(match[1]) <= total else None
            # OCR on the supplied screenshots consistently reads 前往 as 前注.
            buttons = [b for b in self.inside(roi) if compact(b.text) in ("前往", "前注", "立即完成", "一键完成")]
            if len(buttons) > 1 or (not buttons and self.go_button_visible(left, title[1])):
                raise QuizError(f"{name}的领取/扫帚按钮无法确认，已暂停。")
            button = buttons[0] if len(buttons) == 1 else None
            action = ("立即完成" if compact(button.text) in ("立即完成", "一键完成") else "前往") if button else ""
            cards.append((title[1], left, Card(name, current, total, action, button.box if button else None)))
        return [c for _, _, c in sorted(cards, key=lambda item: (round(item[0] / 40), item[1]))]


class LocalDailyView(DailyView):
    """Recognize only regions actually requested by the current flow state.

    The Maa adapter returns boxes in full-image coordinates even with an ROI.
    Region padding protects text edges; selection still uses the original ROI.
    """

    def __init__(self, frame, reader, captured_at):
        super().__init__(frame, [], captured_at)
        self.reader = reader
        self.regions = []
        self.generation = reader.generation

    def inside(self, roi):
        self.reader.check_stop()
        if self.generation != self.reader.generation:
            raise QuizError("日常画面已失效，请重新截图。")
        roi = tuple(roi)
        blocks = None
        for known, values in self.regions:
            if contains(known, roi):
                blocks = values
                self.reader.metrics.count("same_frame_region_hits")
                break
        if blocks is None:
            device_roi = scaled_roi(roi, self.width, self.height, padding=4)
            raw = self.reader.recognize(self.frame, device_roi, self.captured_at)
            # First frame/geometry initialization increments the cache generation.
            self.generation = self.reader.generation
            blocks = DailyView(self.frame, raw, self.captured_at).blocks
            self.regions.append((roi, blocks))
            known = {(b.text, b.box) for b in self.blocks}
            self.blocks.extend(b for b in blocks if (b.text, b.box) not in known)
        x, y, w, h = roi
        return [b for b in blocks if x <= b.box[0] + b.box[2] / 2 < x + w
                and y <= b.box[1] + b.box[3] / 2 < y + h]


def choose_task(cards, selected):
    pending = [c for c in cards if c.name in selected and not c.done]
    for name in ("卓越竞技场", "宝图任务", "大理寺答题"):
        for card in pending:
            if card.name == name and card.action == "立即完成":
                return card
    # shortcut: total activity is not shown here; run five-ring last until a reliable 50-point check is available.
    for card in pending:
        if card.name not in ("卓越竞技场", "五环任务"):
            return card
    return next((c for c in pending if c.name == "五环任务"), None)


class DailyStopped(Exception):
    pass


class DailyTimeout(QuizError):
    pass


class DailyRunner:
    def __init__(self, session, runtime_dir, should_stop, on_result=None, *, vision_options=None, answer_quiz=None,
                 task_interval_max=DEFAULT_TASK_INTERVAL_MAX_SECONDS):
        if type(task_interval_max) is not int or task_interval_max < TASK_INTERVAL_MIN_SECONDS:
            raise QuizError(f"日常任务最大间隔必须是至少 {TASK_INTERVAL_MIN_SECONDS} 秒的整数。")
        self.task_interval_max = task_interval_max
        self.session = session
        self.folder = runtime_dir / "daily"
        self.should_stop = should_stop
        self.on_result = on_result
        self.answer_quiz = answer_quiz
        self.view = None
        self._preview_at = float("-inf")
        self.options = vision_options or VisionOptions.from_env()
        self.metrics = VisionMetrics()
        self.reader = RegionOCR(session, self.options, self.metrics, self.check_stop)

    def check_stop(self):
        if self.should_stop():
            raise DailyStopped()

    def emit(self, message):
        if self.on_result:
            progress = {"message": message}
            now = time.monotonic()
            if self.view is not None and 0 <= now - self.view.captured_at <= 10 and now - self._preview_at >= 2:
                # GUI may display this existing frame; never capture just for a log message.
                progress["frame"] = self.view.frame
                self._preview_at = now
            started = time.perf_counter()
            try:
                self.on_result(progress)
            finally:
                self.metrics.duration("progress_callback", started)

    def read(self):
        self.check_stop()
        captured = time.monotonic()  # Includes acquisition time in staleness checks.
        started = time.perf_counter()
        try:
            frame = self.session.capture()
        finally:
            self.metrics.duration("capture", started)
        self.check_stop()
        self.metrics.count("frames")
        if self.options.mode == "full":
            started = time.perf_counter()
            try:
                blocks = self.session.ocr(frame, (0, 0, frame.shape[1], frame.shape[0]))
            finally:
                self.metrics.duration("ocr", started)
            self.metrics.count("ocr_pixels", frame.shape[1] * frame.shape[0])
            self.view = DailyView(frame, blocks, captured)
        else:
            self.view = LocalDailyView(frame, self.reader, captured)
        self.check_stop()
        return self.view

    def save_metrics(self):
        report = self.metrics.report()
        report.update(mode=self.options.mode, capture_scope="full_device_frame",
                      recognition_scope="on_demand_roi" if self.options.mode == "roi" else "full_frame",
                      options=asdict(self.options))
        try:
            write_json(self.folder / "performance.json", report)
        except OSError:
            # Diagnostics must not turn a successful/failed input into an automatic retry.
            logging.getLogger(__name__).warning("Could not write daily performance diagnostics.")

    def save(self, error=""):
        self.save_metrics()
        if self.view is not None:
            save_image(self.view.frame, self.folder / "screen.png")
            write_json(self.folder / "ocr.json", {"error": error, "reference_size": [834, 470],
                                                 "partial_ocr": isinstance(self.view, LocalDailyView),
                                                 "regions": [r for r, _ in getattr(self.view, "regions", [])],
                                                 "blocks": [asdict(b) for b in self.view.blocks]})

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        self.check_stop()

    def _check_tap_view(self, view):
        age = time.monotonic() - view.captured_at
        if age < 0 or age > 10:
            raise QuizError("日常截图已过期，未点击，请重新检查。")
        if getattr(view, "_click_used", False) or (isinstance(view, LocalDailyView) and view.generation != self.reader.generation):
            raise QuizError("当前画面已执行过点击或失效，必须重新截图。")

    def tap(self, view, box, *, urgent=False):
        self.check_stop()
        self._check_tap_view(view)
        if box is None or len(box) != 4 or any(not math.isfinite(v) for v in box):
            raise QuizError("无效的点击位置，未点击。")
        x, y, w, h = box
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 834 or y + h > 470:
            raise QuizError("点击位置超出校准区域，未点击。")
        left, right = (math.ceil(edge * view.width / 834) for edge in (x, x + w))
        top, bottom = (math.ceil(edge * view.height / 470) for edge in (y, y + h))
        if left >= right or top >= bottom:
            raise QuizError("点击区域没有可用像素，未点击。")
        if not urgent:
            self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
            self._check_tap_view(view)
        point = random_click_point((left, top, right - left, bottom - top))
        view._click_used = True
        started = time.perf_counter()
        try:
            self.session.click(*point)
        finally:
            self.metrics.duration("click", started)
            # Also invalidate after a failed/ambiguous input; never retry from this view.
            self.reader.invalidate()
        self.check_stop()

    def until(self, predicate, message, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.read()
            if predicate(view):
                return view
            self.sleep(min(self.options.poll_seconds, max(0, deadline - time.monotonic())))
        raise DailyTimeout(message)

    def open_list(self):
        view = self.read()
        if view.dig_popup:
            self.tap(view, view.find("取消", (250, 280, 160, 50)), urgent=True)
            view = self.until(lambda v: not v.dig_popup, "挖宝弹窗尚未关闭。", 10)
        attempts = {"活动": 0, "扫帚": 0}
        while True:
            self.check_stop()
            if view.broom_guide:
                self.emit("检测到金扫帚引导，点击关闭后继续日常任务。")
                self.tap(view, (400, 420, 20, 10))
                view = self.until(lambda v: not v.broom_guide, "金扫帚引导仍未关闭，已暂停，未重复点击。", 5)
            if view.sweep_panel:
                return view
            if view.activity_panel:
                stage, box = "扫帚", (713, 405, 40, 30)
            else:
                finished = [b for b in view.inside((60, 320, 650, 80)) if "明天再来" in compact(b.text)]
                if finished:
                    self.tap(view, finished[0].box)
                    self.sleep(0.5)
                    view = self.read()
                    if view.activity_panel or view.sweep_panel:
                        continue
                if "日常" not in view.text((680, 78, 60, 28)) or "活动" not in view.text((755, 78, 65, 28)):
                    raise QuizError("未识别到主界面，请手动打开活动 → 日常任务列表后继续。")
                stage, box = "活动", (8, 108, 42, 42)
            if attempts[stage] >= 3:
                raise QuizError(f"{stage}入口重试 3 次后仍未打开，已保存现场。")
            attempts[stage] += 1
            self.emit(f"正在打开{stage}入口（第 {attempts[stage]}/3 次）。")
            # shortcut: activity/broom/close icons use this 16:9 layout; recalibrate if the UI moves.
            self.tap(view, box)
            try:
                view = self.until(lambda v: v.broom_guide or v.sweep_panel or (stage == "活动" and v.activity_panel),
                                  f"{stage}入口尚未打开。", 5)
            except DailyTimeout:
                view = self.read()
                self.emit(f"重新检查{stage}入口，未打开时将再次尝试。")

    def stable_list(self):
        view = self.open_list()
        deadline = time.monotonic() + 15
        previous = None
        last_error = "任务状态仍在变化。"
        while True:
            if view.broom_guide:
                view = self.open_list()
                previous = None
            try:
                cards = view.cards()
            except QuizError as exc:
                previous = None
                last_error = str(exc)
            else:
                signature = [(c.name, c.current, c.done, c.action, c.button) for c in cards]
                if signature == previous:
                    return view, cards
                previous = signature
            if time.monotonic() >= deadline:
                raise QuizError("等待任务列表稳定超过 15 秒，未点击。" + last_error)
            self.sleep(self.options.poll_seconds)
            view = self.read()

    def pickup(self, view, card):
        if card.name not in PICKUPS:
            raise QuizError(f"{card.name}没有已接入的普通领取流程。")
        if card.name == "大理寺答题" and self.answer_quiz is None:
            raise QuizError("大理寺随机答题处理不可用，未点击领取。")
        self.tap(view, card.button)
        npc, option = PICKUPS[card.name]
        if card.name == "宝图任务":
            npc = "店小"  # The supplied screenshot's OCR omits the final 二.
        resume_option = "大理寺答题（未完成）"

        def dialog_option(v):
            if card.name == "大理寺答题" and compact(resume_option) in v.text(DIALOG):
                return resume_option
            return option

        view = self.until(lambda v: v.tracking() == card.name or
                          (npc in v.text(DIALOG) and compact(dialog_option(v)).rstrip("！!") in v.text(DIALOG)),
                          f"等待{card.name}领取对话超时，未重复领取。")
        if view.tracking() != card.name:
            document_option = "宝图任务（文牒通关）"
            if card.name == "宝图任务" and compact(document_option) in view.text(DIALOG):
                self.emit("优先选择宝图任务（文牒通关），随后核对是否直接完成。")
                self.tap(view, view.find(document_option, DIALOG))
                return True
            self.tap(view, view.find(dialog_option(view), DIALOG))
            if card.name == "大理寺答题":
                self.emit("开始大理寺随机答题，不查询题库、不调用 AI。")
                self.check_stop()
                try:
                    result = self.answer_quiz(lambda r: self.emit(
                        f"大理寺随机答题：已提交 {r['question_number']}/{r['question_total']} 题。"))
                finally:
                    self.reader.invalidate()
                self.check_stop()
                if result["reason"] == "stopped":
                    raise DailyStopped()
                return True
            self.until(lambda v: v.tracking() == card.name, f"未确认{card.name}已开始，未重复领取。", 20)
        return False

    def wait_task(self, name):
        estimate = WAIT_SECONDS[name]
        early_probe = self.options.early_probe
        if early_probe:
            self.emit(f"检测到{name}任务进度，每约 {self.options.probe_seconds:g} 秒截图检查完成提示；最长 {estimate // 60} 分钟后核对次数，不操作战斗。")
        else:
            self.emit(f"检测到{name}任务进度，{estimate // 60} 分钟后首次核对完成状态；停止助手不会停止游戏内自动任务。")
        deadline = time.monotonic() + estimate
        next_notice = time.monotonic() + 30
        hints = 0
        while time.monotonic() < deadline:
            interval = self.options.probe_seconds if early_probe else 30
            self.sleep(min(interval, max(0, deadline - time.monotonic())))
            if early_probe and time.monotonic() < deadline:
                view = self.read()
                # Absence of tracking alone is NOT completion (combat/occlusion).
                terminal = view.text((60, 320, 650, 80)) if view.tracking() is None else ""
                positive = "明天再来" in terminal and ("已完成" in terminal or "任务已完成" in terminal)
                hints = hints + 1 if positive else 0
                if hints >= 2 or (name == "宝图任务" and view.dig_popup):
                    self.emit(f"检测到{name}完成提示，提前核对列表次数。")
                    break
            if time.monotonic() >= next_notice:
                self.emit(f"等待{name}自动执行，距定时核对约 {max(0, round((deadline - time.monotonic()) / 60))} 分钟。")
                next_notice = time.monotonic() + 30
        deadline = time.monotonic() + 300
        while True:
            view, cards = self.stable_list()
            card = next(c for c in cards if c.name == name)
            if card.done:
                evidence = "无前往/立即完成按钮" if not card.action else f"{card.current}/{card.total}"
                self.emit(f"{name}已核对完成：{evidence}。")
                return
            progress = f"{card.current if card.current is not None else '?'}/{card.total}"
            if time.monotonic() >= deadline:
                raise QuizError(f"{name}等待超过预估时间及额外 5 分钟，仍为 {progress}；已暂停，未领取下一项。")
            self.emit(f"{name}仍有领取按钮（{progress}），继续等待，不重复领取。")
            self.tap(view, (710, 42, 28, 28))
            self.sleep(30)

    def run(self, selected):
        selected = set(selected)
        if not selected or selected - TOTALS.keys():
            raise QuizError("请选择已接入的日常任务。")
        try:
            view = self.read()
            active = view.tracking()
            task_completed = False
            if active:
                if active not in selected:
                    raise QuizError(f"{active}正在执行，请等待完成或勾选该任务后继续。")
                self.wait_task(active)
                task_completed = True
            while True:
                view, cards = self.stable_list()
                missing = selected - {c.name for c in cards}
                if missing:
                    raise QuizError(f"未能识别所选日常「{'、'.join(sorted(missing))}」，已暂停，不能据此判断完成。")
                pending = [c.name for c in cards if c.name in selected and not c.done]
                if not pending:
                    self.save()
                    return {"reason": "completed", "completed": [c.name for c in cards if c.name in selected], "pending": []}
                card = choose_task(cards, selected)
                if card is None:
                    self.save("竞技场未完成且当前没有可用扫帚，未执行普通竞技场。")
                    return {"reason": "needs_attention", "completed": [c.name for c in cards if c.name in selected and c.done],
                            "pending": pending}
                if task_completed:
                    delay = random.uniform(TASK_INTERVAL_MIN_SECONDS, self.task_interval_max)
                    self.emit(f"上一项日常已完成，随机等待 {delay:.1f} 秒后重新核对并执行下一项。")
                    self.sleep(delay)
                    task_completed = False
                    continue
                if card.action == "立即完成":
                    self.emit(f"自动使用金扫帚完成{card.name}。")
                    self.tap(view, card.button)
                    # Read immediately: the treasure follow-up has a five-second countdown.
                    view = self.read()
                    if view.dig_popup:
                        self.tap(view, view.find("取消", (250, 280, 160, 50)), urgent=True)
                    self.sleep(0.5)
                    _, updated = self.stable_list()
                    if not next(c for c in updated if c.name == card.name).done:
                        raise QuizError(f"扫帚操作后未确认{card.name}完成，未重复点击。")
                else:
                    if self.pickup(view, card):
                        self.sleep(0.5)
                        _, updated = self.stable_list()
                        if not next(c for c in updated if c.name == card.name).done:
                            operation = "随机答题" if card.name == "大理寺答题" else "文牒通关"
                            raise QuizError(f"{operation}后未确认{card.name}完成，已暂停，未重复执行。")
                    else:
                        self.wait_task(card.name)
                task_completed = True
        except DailyStopped:
            return {"reason": "stopped"}
        except QuizError as exc:
            self.save(str(exc))
            raise
        finally:
            self.save_metrics()
