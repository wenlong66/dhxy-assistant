"""Screenshot-calibrated daily pickup and completion checks; no AI or combat input."""
from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass

from maa_adapter import save_image
from quiz_core import QuizError, normalize
from quiz_vision import TextBlock, write_json

TOTALS = {"帮派任务": 20, "五环任务": 1, "师门任务": 20, "宝图任务": 15, "卓越竞技场": 3}
WAIT_SECONDS = {"帮派任务": 420, "五环任务": 180, "师门任务": 540}
PICKUPS = {"帮派任务": ("帮派总管", "帮派任务"),
           "五环任务": ("云游大师", "我自己去拯救苍生！"),
           "师门任务": ("聂小倩", "师门任务")}
TABLE = (250, 58, 462, 282)
DIALOG = (530, 160, 280, 300)
TRACKER = (677, 104, 151, 104)


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

    def tracking(self):
        text = self.text(TRACKER)
        for name in WAIT_SECONDS:
            if re.search(re.escape(name[:2]) + r"[^/]{0,35}\d+/\d+", text):
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
            title = self.find(name, TABLE)
            left = 250 if title[0] < 480 else 483
            if title[1] + 72 > TABLE[1] + TABLE[3]:
                raise QuizError(f"{name}卡片未完整显示，不能据此判断完成。")
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


def choose_task(cards, selected):
    pending = [c for c in cards if c.name in selected and not c.done]
    for name in ("卓越竞技场", "宝图任务"):
        for card in pending:
            if card.name == name and card.action == "立即完成":
                return card
    return next((c for c in pending if c.name != "卓越竞技场"), None)


class DailyStopped(Exception):
    pass


class DailyTimeout(QuizError):
    pass


class DailyRunner:
    def __init__(self, session, runtime_dir, should_stop, on_result=None):
        self.session = session
        self.folder = runtime_dir / "daily"
        self.should_stop = should_stop
        self.on_result = on_result
        self.view = None

    def check_stop(self):
        if self.should_stop():
            raise DailyStopped()

    def emit(self, message):
        if self.on_result:
            self.on_result({"message": message})

    def read(self):
        self.check_stop()
        frame = self.session.capture()
        captured = time.monotonic()
        blocks = self.session.ocr(frame, (0, 0, frame.shape[1], frame.shape[0]))
        self.view = DailyView(frame, blocks, captured)
        self.check_stop()
        return self.view

    def save(self, error=""):
        if self.view is not None:
            save_image(self.view.frame, self.folder / "screen.png")
            write_json(self.folder / "ocr.json", {"error": error, "reference_size": [834, 470],
                                                 "blocks": [asdict(b) for b in self.view.blocks]})

    def sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_stop()
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        self.check_stop()

    def tap(self, view, box):
        self.check_stop()
        if time.monotonic() - view.captured_at > 10:
            raise QuizError("日常截图已过期，未点击，请重新检查。")
        x, y, w, h = box
        self.session.click(round((x + w / 2) * view.width / 834),
                           round((y + h / 2) * view.height / 470))

    def until(self, predicate, message, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.read()
            if predicate(view):
                return view
            self.sleep(0.5)
        raise DailyTimeout(message)

    def open_list(self):
        view = self.read()
        if view.dig_popup:
            self.tap(view, view.find("取消", (250, 280, 160, 50)))
            view = self.until(lambda v: not v.dig_popup, "挖宝弹窗尚未关闭。", 10)
        attempts = {"活动": 0, "扫帚": 0}
        while not view.sweep_panel:
            self.check_stop()
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
                view = self.until(lambda v: v.sweep_panel or (stage == "活动" and v.activity_panel),
                                  f"{stage}入口尚未打开。", 5)
            except DailyTimeout:
                view = self.read()
                self.emit(f"重新检查{stage}入口，未打开时将再次尝试。")
        return view

    def stable_list(self):
        view = self.open_list()
        deadline = time.monotonic() + 15
        previous = None
        last_error = "任务状态仍在变化。"
        while True:
            try:
                cards = view.cards()
            except QuizError as exc:
                previous = None
                last_error = str(exc)
            else:
                signature = [(c.name, c.done, c.action) for c in cards]
                if signature == previous:
                    return view, cards
                previous = signature
            if time.monotonic() >= deadline:
                raise QuizError("等待任务列表稳定超过 15 秒，未点击。" + last_error)
            self.sleep(0.5)
            view = self.read()

    def pickup(self, view, card):
        if card.name not in PICKUPS:
            raise QuizError("宝图普通领取流程尚未实测：请提供无扫帚时的领取对话和开始画面。")
        self.tap(view, card.button)
        npc, option = PICKUPS[card.name]
        view = self.until(lambda v: v.tracking() == card.name or
                          (npc in v.text(DIALOG) and compact(option).rstrip("！!") in v.text(DIALOG)),
                          f"等待{card.name}领取对话超时，未重复领取。")
        if view.tracking() != card.name:
            self.tap(view, view.find(option, DIALOG))
            self.until(lambda v: v.tracking() == card.name, f"未确认{card.name}已开始，未重复领取。", 20)

    def wait_task(self, name):
        estimate = WAIT_SECONDS[name]
        self.emit(f"{name}已开始，约 {estimate // 60} 分钟后首次核对完成次数；停止助手不会停止游戏内自动任务。")
        deadline = time.monotonic() + estimate
        while time.monotonic() < deadline:
            self.sleep(min(30, deadline - time.monotonic()))
            self.emit(f"等待{name}自动执行，距首次检查约 {max(0, round((deadline - time.monotonic()) / 60))} 分钟。")
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
            if active:
                if active not in selected:
                    raise QuizError(f"{active}正在执行，请等待完成或勾选该任务后继续。")
                self.wait_task(active)
            while True:
                view, cards = self.stable_list()
                pending = [c.name for c in cards if c.name in selected and not c.done]
                if not pending:
                    self.save()
                    return {"reason": "completed", "completed": [c.name for c in cards if c.name in selected], "pending": []}
                card = choose_task(cards, selected)
                if card is None:
                    self.save("竞技场未完成且当前没有可用扫帚，未执行普通竞技场。")
                    return {"reason": "needs_attention", "completed": [c.name for c in cards if c.name in selected and c.done],
                            "pending": pending}
                if card.action == "立即完成":
                    self.emit(f"自动使用金扫帚完成{card.name}。")
                    self.tap(view, card.button)
                    # Read immediately: the treasure follow-up has a five-second countdown.
                    view = self.read()
                    if view.dig_popup:
                        self.tap(view, view.find("取消", (250, 280, 160, 50)))
                    self.sleep(0.5)
                    _, updated = self.stable_list()
                    if not next(c for c in updated if c.name == card.name).done:
                        raise QuizError(f"扫帚操作后未确认{card.name}完成，未重复点击。")
                else:
                    self.pickup(view, card)
                    self.wait_task(card.name)
        except DailyStopped:
            return {"reason": "stopped"}
        except QuizError as exc:
            self.save(str(exc))
            raise
