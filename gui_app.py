"""Windows desktop UI for DHXY Assistant v0.3.

Run: python gui_app.py (after pip install -r requirements-gui.txt).
All device/OCR/OpenAI calls are serialized on one background thread.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

try:
    from PySide6.QtCore import QObject, Qt, Signal, QSize, QUrl
    from PySide6.QtGui import QDesktopServices, QPixmap
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox,
        QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
        QPlainTextEdit, QScrollArea, QSpinBox, QSplitter, QTabWidget,
        QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
    )
except ImportError:
    print("GUI dependency missing. Run: python -m pip install -r requirements-gui.txt", file=sys.stderr)
    raise SystemExit(2)

from gui_backend import DeviceBackend, LOCAL_CONFIG, ROOT, local_settings, preview_png, save_local_settings
from quiz_core import QuizError


class Bridge(QObject):
    message = Signal(str, object)


class DeviceWorker:
    """The only thread that touches a MaaSession or its resources."""

    def __init__(self, bridge: Bridge, backend_factory=DeviceBackend):
        self.bridge = bridge
        self.backend_factory = backend_factory
        self.queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.cancelled = threading.Event()
        self.alive = threading.Event()
        self.alive.set()
        self.preview_enabled = False
        self.busy = False
        self.stop_requested = False
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="dhxy-device-worker")
        self.thread.start()

    def submit(self, command: str, **arguments) -> bool:
        with self._lock:
            if self.busy or self.stop_requested or not self.alive.is_set():
                return False
            self.busy = True
            self.cancelled.clear()
            self.queue.put((command, arguments))
            return True

    def pause_preview(self):
        self.preview_enabled = False

    def resume_preview(self):
        self.preview_enabled = True

    def stop(self):
        """Fail closed: no future answer click; disconnection follows the active job."""
        with self._lock:
            if self.stop_requested:
                return
            self.stop_requested = True
            self.cancelled.set()
            self.preview_enabled = False
            # Only one foreground command can be queued. Disconnect afterwards.
            self.queue.put(("disconnect", {}))
        self.bridge.message.emit("stop_requested", None)

    def shutdown(self):
        self.cancelled.set()
        self.preview_enabled = False
        self.alive.clear()
        self.queue.put(("exit", {}))

    def _run(self):
        backend = self.backend_factory()
        last_preview = 0.0
        import time
        try:
            while True:
                try:
                    command, args = self.queue.get(timeout=0.20)
                except queue.Empty:
                    if (self.alive.is_set() and self.preview_enabled
                            and backend.session is not None and not self.busy
                            and time.monotonic() - last_preview > 2.0):
                        last_preview = time.monotonic()
                        try:
                            self.bridge.message.emit("preview", backend.capture())
                        except Exception as exc:
                            self.preview_enabled = False
                            self.bridge.message.emit("preview_error", str(exc))
                    continue

                if command == "exit":
                    break
                if command == "disconnect":
                    try:
                        backend.disconnect()
                        self.bridge.message.emit("disconnected", {})
                    except Exception as exc:
                        self.bridge.message.emit("error", ("disconnect", str(exc)))
                        # DeviceBackend.disconnect releases its lock even on close errors.
                        self.bridge.message.emit("disconnected", {})
                    finally:
                        with self._lock:
                            self.stop_requested = False
                    continue

                self.bridge.message.emit("started", command)
                try:
                    # Cancellation persists for the complete in-flight operation.
                    if self.cancelled.is_set():
                        raise QuizError("Stopped before starting the operation.")
                    if command == "devices":
                        result = backend.devices()
                    elif command == "connect":
                        result = backend.connect()
                        self.preview_enabled = True
                    elif command == "capture":
                        result = backend.capture(save=True, overlay=True)
                    elif command == "inspect":
                        result = backend.inspect()
                    elif command == "guard":
                        result = backend.guard()
                    elif command == "answer":
                        result = backend.answer(should_stop=self.cancelled.is_set, **args)
                    elif command == "auto_answer":
                        def auto_progress(answer):
                            self.bridge.message.emit("auto_progress", answer)
                            self.bridge.message.emit("preview", backend.capture())
                        result = backend.auto_answer(
                            should_stop=self.cancelled.is_set,
                            on_result=auto_progress, **args)
                    elif command == "inspect_daily":
                        result = backend.inspect_daily()
                    elif command == "auto_daily":
                        def daily_progress(progress):
                            # Reuse the runner's fresh frame rather than acquiring another.
                            frame = progress.get("frame")
                            self.bridge.message.emit("daily_progress", {k: v for k, v in progress.items() if k != "frame"})
                            if frame is not None and self.preview_enabled:
                                self.bridge.message.emit("preview", {"png": preview_png(frame)})
                        result = backend.auto_daily(should_stop=self.cancelled.is_set,
                                                    on_result=daily_progress, **args)
                    elif command == "bank":
                        result = backend.bank(args.get("keyword", ""))
                    elif command == "feedback":
                        result = backend.feedback(**args)
                    else:
                        raise QuizError("Unknown worker command.")
                    outcome = "done"
                    message = (command, result)
                except Exception as exc:
                    # Do not echo request contents, credentials or full stack traces.
                    outcome = "error"
                    message = (command, str(exc))
                finally:
                    with self._lock:
                        self.busy = False
                # Publish after clearing busy: the UI can re-enable controls immediately.
                self.bridge.message.emit(outcome, message)
        finally:
            try:
                backend.shutdown()
            finally:
                self.bridge.message.emit("worker_stopped", None)


def make_button(text: str, callback):
    b = QPushButton(text)
    b.clicked.connect(callback)
    return b


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("大话西游手游助手 · v0.3.0")
        self.resize(1280, 850)
        self.setMinimumSize(1000, 690)
        self.bridge = Bridge()
        self.worker = DeviceWorker(self.bridge)
        self.bridge.message.connect(self.on_event)
        self.connected = False
        self.current_busy = False
        self.was_stopped = False
        self.stopping = False
        self.raw_preview: bytes | None = None
        self.last_inspection_valid = False
        self._make_ui()
        self._load_settings()
        self.log("GUI 已启动。连接后可预览单题、提交一次或启动大理寺自动答题。")
        self._refresh_controls()

    def _make_ui(self):
        root = QWidget()
        root.setObjectName("mainContent")
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        main = QVBoxLayout(root)
        main.setContentsMargins(16, 12, 16, 12)
        main.setSpacing(12)

        heading = QHBoxLayout()
        title = QLabel("大话西游 · 日常助手")
        title.setObjectName("mainTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.status = QLabel("未连接")
        self.status.setObjectName("status")
        heading.addWidget(self.status)
        main.addLayout(heading)

        device_group = QGroupBox("Android 模拟器连接")
        devices = QHBoxLayout(device_group)
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(340)
        self.device_combo.addItem("尚未扫描设备", None)
        self.scan_btn = make_button("扫描设备", lambda: self.run_command("devices"))
        self.connect_btn = make_button("连接", self.connect_device)
        self.disconnect_btn = make_button("断开 / 停止", self.stop_device)
        devices.addWidget(self.device_combo, stretch=1)
        devices.addWidget(self.scan_btn)
        devices.addWidget(self.connect_btn)
        devices.addWidget(self.disconnect_btn)
        main.addWidget(device_group)

        self.tabs = QTabWidget()
        main.addWidget(self.tabs, stretch=1)
        self._make_task_tab()
        self._make_ocr_tab()
        self._make_bank_tab()
        self._make_settings_tab()

        log_group = QGroupBox("运行日志（仅本地显示）")
        log_layout = QVBoxLayout(log_group)
        self.logs = QPlainTextEdit()
        self.logs.setObjectName("logs")
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(500)
        self.logs.setFixedHeight(120)
        log_layout.addWidget(self.logs)
        main.addWidget(log_group)

    def _make_task_tab(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        right = QVBoxLayout()
        layout.addLayout(left, 3)
        layout.addLayout(right, 1)
        settings_row = QHBoxLayout()
        left.addLayout(settings_row)

        tasks = QGroupBox("任务选择")
        task_l = QVBoxLayout(tasks)
        self.dali_check = QCheckBox("大理寺答题（单题／自动；已接入）")
        self.dali_check.setChecked(True)
        task_l.addWidget(self.dali_check)
        self.daily_checks = {}
        for name, label in (("师门任务", "师门任务"), ("帮派任务", "帮派任务"),
                            ("宝图任务", "宝图任务"), ("五环任务", "五环任务"),
                            ("卓越竞技场", "卓越竞技场（仅金扫帚）")):
            check = QCheckBox(label)
            check.setChecked(True)
            if name == "宝图任务":
                check.setToolTip("优先使用已有扫帚；领取时优先文牒通关，否则普通领取后等待 5 分钟再检查。")
            self.daily_checks[name] = check
            task_l.addWidget(check)
        task_note = QLabel("日常按当前列表顺序；自动用已有金扫帚，优先竞技场、宝图。大理寺单独启动。")
        task_note.setWordWrap(True)
        task_l.addWidget(task_note)
        daily_controls = QHBoxLayout()
        self.daily_inspect_btn = make_button("检查日常列表", lambda: self.run_command("inspect_daily"))
        self.daily_start_btn = make_button("开始日常任务", self.start_daily)
        self.daily_start_btn.setToolTip("从游戏主界面自动打开活动→扫帚列表，再领取、等待和核对次数；会消耗已有金扫帚，不调用 AI。")
        daily_controls.addWidget(self.daily_inspect_btn)
        daily_controls.addWidget(self.daily_start_btn)
        task_l.addLayout(daily_controls)
        settings_row.addWidget(tasks, 1)

        options = QGroupBox("答题控制")
        options_l = QVBoxLayout(options)
        self.ai_enabled = QCheckBox("题库未命中时询问 OpenAI（可能产生费用）")
        self.allow_trial = QCheckBox("允许试答尚未验证的 AI 候选")
        self.random_answer = QCheckBox("随机答题（不查题库、不调用 AI）")
        self.random_answer.toggled.connect(self.ai_enabled.setDisabled)
        self.random_answer.toggled.connect(self.allow_trial.setDisabled)
        self.layout_confirmed = QCheckBox("已核对本设备 OCR、选项位置与区域框图")
        self.layout_confirmed.setToolTip("仅非随机的单题手动提交需要确认；随机和自动答题会自行识别并校验按钮。")
        for ch in (self.random_answer, self.ai_enabled, self.allow_trial, self.layout_confirmed):
            options_l.addWidget(ch)
        self.model_edit = QLineEdit(os.environ.get("OPENAI_MODEL", ""))
        self.model_edit.setPlaceholderText("API 模型 ID（仅开启 AI 时需要）")
        options_l.addWidget(self.model_edit)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("OpenAI API Key（仅当前运行内存使用，不保存）")
        options_l.addWidget(self.key_edit)
        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("每日 AI 请求次数上限："))
        self.max_calls = QSpinBox()
        self.max_calls.setRange(1, 1000)
        self.max_calls.setValue(30)
        max_row.addWidget(self.max_calls)
        max_row.addStretch()
        options_l.addLayout(max_row)
        settings_row.addWidget(options, 2)

        controls = QHBoxLayout()
        self.preview_btn = make_button("预览本题答案", self.preview_answer)
        self.execute_btn = make_button("提交本题一次", self.execute_answer)
        self.auto_btn = make_button("开始自动答题", self.start_auto_answer)
        self.auto_btn.setToolTip("勾选随机则连续随机提交；否则题库优先，未命中用 AI 并提交。")
        self.pause_btn = make_button("暂停画面预览", self.toggle_preview)
        controls.addWidget(self.preview_btn)
        controls.addWidget(self.execute_btn)
        controls.addWidget(self.auto_btn)
        controls.addWidget(self.pause_btn)
        left.addLayout(controls)

        self.answer_out = QPlainTextEdit()
        self.answer_out.setReadOnly(True)
        self.answer_out.setPlaceholderText("答题建议、来源和提交回执会显示在这里。")
        answer_box = QGroupBox("答题结果")
        al = QVBoxLayout(answer_box)
        al.addWidget(self.answer_out)
        left.addWidget(answer_box, stretch=1)

        video_box = QGroupBox("游戏画面（约每 2 秒刷新，仅截图）")
        vl = QVBoxLayout(video_box)
        self.preview_label = QLabel("请先扫描并连接 Android 模拟器")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(280, 158)
        self.preview_label.setMaximumHeight(220)
        self.preview_label.setWordWrap(True)
        self.preview_label.setObjectName("preview")
        vl.addWidget(self.preview_label, stretch=1)
        row = QGridLayout()
        self.screenshot_btn = make_button("保存截图和区域框图", lambda: self.run_command("capture"))
        self.inspect_btn = make_button("OCR 识别当前题", lambda: self.run_command("inspect"))
        self.guard_btn = make_button("校验答题界面", lambda: self.run_command("guard"))
        row.addWidget(self.screenshot_btn, 0, 0, 1, 2)
        row.addWidget(self.inspect_btn, 1, 0)
        row.addWidget(self.guard_btn, 1, 1)
        vl.addLayout(row)
        right.addWidget(video_box)
        right.addStretch()
        self.tabs.addTab(page, "任务与画面")

    def _make_ocr_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("先手动打开大理寺题目，再点击 OCR 识别；可在本地诊断文件中查看文字及区域框图。")
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(make_button("重新识别", lambda: self.run_command("inspect")))
        row.addWidget(make_button("打开诊断目录", self.open_diagnostics))
        row.addStretch()
        layout.addLayout(row)
        self.ocr_out = QPlainTextEdit()
        self.ocr_out.setReadOnly(True)
        layout.addWidget(self.ocr_out, stretch=1)
        self.tabs.addTab(page, "OCR 调试")

    def _make_bank_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.bank_search = QLineEdit()
        self.bank_search.setPlaceholderText("按题干／答案筛选（当前账号 scope，最多 200 条）")
        self.bank_search.returnPressed.connect(self.refresh_bank)
        row.addWidget(self.bank_search, stretch=1)
        row.addWidget(make_button("刷新题库", self.refresh_bank))
        layout.addLayout(row)
        self.bank_table = QTableWidget(0, 5)
        self.bank_table.setHorizontalHeaderLabels(["状态", "题干", "答案", "模型", "更新时间"])
        self.bank_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.bank_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.bank_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.bank_table, stretch=1)
        fb = QGroupBox("人工录入真实答题反馈（只对助手已提交的题有效）")
        fl = QFormLayout(fb)
        self.attempt_edit = QLineEdit()
        self.attempt_edit.setPlaceholderText("提交结果返回的 attempt_id")
        self.outcome_combo = QComboBox()
        self.outcome_combo.addItem("实际看到答对", "correct")
        self.outcome_combo.addItem("实际看到答错", "wrong")
        self.outcome_combo.addItem("结果看不清", "unknown")
        self.evidence_edit = QLineEdit()
        self.evidence_edit.setPlaceholderText("填写你在游戏内看到的明确证据")
        fl.addRow("提交记录", self.attempt_edit)
        fl.addRow("实际结果", self.outcome_combo)
        fl.addRow("反馈依据", self.evidence_edit)
        fl.addRow("", make_button("保存反馈并更新题库", self.save_feedback))
        layout.addWidget(fb)
        self.tabs.addTab(page, "本地题库")

    def _make_settings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        info = QLabel("本地设置仅保存 ADB 路径、设备地址、题库隔离标识和布局文件。不会保存 OpenAI Key。")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.adb_edit = QLineEdit()
        self.addr_edit = QLineEdit()
        self.scope_edit = QLineEdit()
        self.layout_edit = QLineEdit()
        form = QFormLayout()
        form.addRow("ADB 可执行文件路径（可留空）", self.adb_edit)
        form.addRow("实例地址（如 127.0.0.1:端口）", self.addr_edit)
        form.addRow("账号隔离标识（不要填密码）", self.scope_edit)
        form.addRow("OCR 布局 JSON", self.layout_edit)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.save_settings_btn = make_button("保存设置", self.save_settings)
        row.addWidget(self.save_settings_btn)
        row.addWidget(make_button("查看模拟器接入指南", self.open_guide))
        row.addStretch()
        layout.addLayout(row)
        important = QLabel("设备连接期间不能修改配置；更换模拟器或布局前请先断开。初始 OCR 区域必须在真实画面中校验。")
        important.setWordWrap(True)
        layout.addWidget(important)
        layout.addStretch()
        self.tabs.addTab(page, "连接设置")

    def _load_settings(self):
        try:
            settings = local_settings()
            for key, widget in (("adb_path", self.adb_edit), ("address", self.addr_edit),
                                ("account_scope", self.scope_edit), ("layout", self.layout_edit)):
                widget.setText(settings[key])
        except Exception as exc:
            self.log(f"配置读取失败：{exc}")

    def _settings_data(self):
        return {"adb_path": self.adb_edit.text().strip(), "address": self.addr_edit.text().strip(),
                "account_scope": self.scope_edit.text().strip(), "layout": self.layout_edit.text().strip()}

    def save_settings(self) -> bool:
        if self.connected or self.current_busy or self.worker.busy:
            self.log("请先断开设备并等待任务结束，再修改本地设置。")
            return False
        try:
            save_local_settings(self._settings_data())
        except Exception as exc:
            self.log(f"保存配置失败：{exc}")
            return False
        self.last_inspection_valid = False
        self.layout_confirmed.setChecked(False)
        self.log(f"本地配置已保存：{LOCAL_CONFIG.name}（不包含密钥）。")
        return True

    def connect_device(self):
        if self.current_busy or self.connected:
            return
        data = self.device_combo.currentData()
        if isinstance(data, dict):
            self.adb_edit.setText(data["adb_path"])
            self.addr_edit.setText(data["address"])
        if self.save_settings():
            self.run_command("connect")

    def stop_device(self):
        self.stopping = True
        self.worker.stop()
        self.was_stopped = True
        self.last_inspection_valid = False
        self.layout_confirmed.setChecked(False)
        self._refresh_controls()

    def toggle_preview(self):
        if self.worker.preview_enabled:
            self.worker.pause_preview()
            self.pause_btn.setText("继续画面预览")
            self.log("实时画面预览已暂停；此操作不会终止进行中的 AI 请求。")
        else:
            self.worker.resume_preview()
            self.pause_btn.setText("暂停画面预览")
            self.log("实时画面预览已恢复。")

    def answer_params(self, *, execute: bool, automatic=False):
        if not self.dali_check.isChecked():
            raise QuizError("没有勾选已实现的大理寺答题模块。")
        random_answer = self.random_answer.isChecked()
        if execute and not (automatic or random_answer) and not self.last_inspection_valid:
            raise QuizError("题目 OCR 尚未通过：请点击「OCR 识别当前题」，到「OCR 调试」查看题号、题干和选项的识别结果。")
        if execute and not (automatic or random_answer) and not self.layout_confirmed.isChecked():
            raise QuizError("题目 OCR 已通过，请核对题干、全部选项及区域框图，再勾选布局确认。")
        use_ai = (automatic or self.ai_enabled.isChecked()) and not random_answer
        if use_ai and not self.model_edit.text().strip():
            raise QuizError("按题作答需要填写 OpenAI 模型 ID。")
        return {"use_ai": use_ai, "model": self.model_edit.text().strip(),
                "api_key": self.key_edit.text(), "execute": execute,
                "random_answer": random_answer,
                "allow_trial": automatic or self.allow_trial.isChecked(),
                "layout_confirmed": execute and (automatic or random_answer or self.layout_confirmed.isChecked()),
                "max_daily_calls": self.max_calls.value()}

    def preview_answer(self):
        try:
            params = self.answer_params(execute=False)
        except QuizError as exc:
            self.log(str(exc)); return
        self.run_command("answer", **params)

    def execute_answer(self):
        try:
            params = self.answer_params(execute=True)
        except QuizError as exc:
            self.log(str(exc)); return
        confirmation = QMessageBox.question(
            self, "确认单次点击", "确认已核对当前题目和选项位置？\n\n"
            "本次最多提交一个答案；随机或 AI 答案可能出错，提交后结果仍需人工确认。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmation == QMessageBox.StandardButton.Yes:
            self.run_command("answer", **params)

    def start_auto_answer(self):
        try:
            params = self.answer_params(execute=True, automatic=True)
        except QuizError as exc:
            self.log(str(exc)); return
        mode = ("连续随机选择答案并提交。" if params["random_answer"] else
                "题库优先，未命中时调用 AI 并自动提交候选答案，可能产生 API 费用。")
        confirmation = QMessageBox.question(
            self, "开始自动答题", mode + "\n\n将从当前题连续答到本轮最后一题。"
            "程序会自动识别题目并检查布局，答案可能出错，不会自动标记答对。\n点击「断开 / 停止」可中止后续提交。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirmation == QMessageBox.StandardButton.Yes:
            self.run_command("auto_answer", **params)

    def refresh_bank(self):
        self.run_command("bank", keyword=self.bank_search.text())

    def start_daily(self):
        selected = [name for name, check in self.daily_checks.items() if check.isChecked()]
        if not selected:
            self.log("请先勾选要执行的日常任务。")
            return
        self.run_command("auto_daily", selected=selected)

    def save_feedback(self):
        result = self.outcome_combo.currentData()
        attempt = self.attempt_edit.text().strip()
        evidence = self.evidence_edit.text().strip()
        if not attempt or not evidence:
            self.log("请填写提交记录 attempt_id 和实际看到的证据。")
            return
        self.run_command("feedback", attempt_id=attempt, outcome=result, evidence=evidence)

    def run_command(self, command: str, **kwargs):
        if self.current_busy:
            self.log("已有任务运行中，请等待或停止。")
            return
        self.was_stopped = False
        if not self.worker.submit(command, **kwargs):
            self.log("设备工作线程忙碌，无法启动第二个操作。")
            return
        self.current_busy = True
        self._refresh_controls()

    def _refresh_controls(self):
        busy = self.current_busy or self.worker.busy or self.stopping or self.worker.stop_requested
        self.scan_btn.setEnabled(not busy and not self.connected)
        self.connect_btn.setEnabled(not busy and not self.connected)
        self.disconnect_btn.setEnabled(self.connected or busy)
        for btn in (self.preview_btn, self.execute_btn, self.auto_btn, self.inspect_btn, self.guard_btn,
                    self.screenshot_btn, self.daily_inspect_btn, self.daily_start_btn):
            btn.setEnabled(self.connected and not busy)
        for check in self.daily_checks.values():
            check.setEnabled(not busy)
        self.pause_btn.setEnabled(self.connected)
        self.save_settings_btn.setEnabled(not busy and not self.connected)
        self.random_answer.setEnabled(not busy)
        self.ai_enabled.setEnabled(not busy and not self.random_answer.isChecked())
        self.allow_trial.setEnabled(not busy and not self.random_answer.isChecked())
        for widget in (self.model_edit, self.key_edit, self.max_calls, self.layout_confirmed):
            widget.setEnabled(not busy)
        for widget in (self.adb_edit, self.addr_edit, self.scope_edit, self.layout_edit):
            widget.setEnabled(not busy and not self.connected)
        self.status.setText("执行中…" if busy else ("已连接" if self.connected else "未连接"))

    def on_event(self, kind: str, payload):
        if kind == "daily_progress":
            self.log(payload["message"])
            return
        if kind == "auto_progress":
            self.answer_out.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            attempt = (payload.get("decision") or {}).get("attempt_id")
            if attempt:
                self.attempt_edit.setText(attempt)
            self.log(f"自动答题：已提交第 {payload['question_number']}/{payload['question_total']} 题，结果仍需核实。")
            return
        if kind == "preview":
            self.show_frame(payload["png"])
            return
        if kind == "preview_error":
            self.log(f"预览暂停：{payload}")
            return
        if kind == "stop_requested":
            self.log("已请求停止：进行中的请求可能需要自然返回，但之后不会再提交点击。")
            return
        if kind == "disconnected":
            self.connected = False
            self.stopping = False
            self.current_busy = False
            self.last_inspection_valid = False
            self.layout_confirmed.setChecked(False)
            self.log("模拟器已断开。")
            self._refresh_controls()
            return
        if kind == "worker_stopped":
            self.log("设备工作线程已退出。")
            return
        if kind == "started":
            self.log("自动答题已启动：正在自动识别与校验，无需手动 OCR 或勾选确认。"
                     if payload == "auto_answer" else "开始操作：" + payload)
            return
        if kind == "error":
            command, message = payload
            self.current_busy = False
            self.log(f"{command} 已暂停：{message}")
            self._refresh_controls()
            return
        if kind != "done":
            return
        command, result = payload
        self.current_busy = False
        if command == "devices":
            self.device_combo.clear()
            self.device_combo.addItem("手动配置／选择设备", None)
            for d in result:
                self.device_combo.addItem(f"{d['name']} · {d['address']}", d)
            if len(result) == 1:
                self.device_combo.setCurrentIndex(1)
            self.log(f"扫描完成：检测到 {len(result)} 个 Android 实例。")
        elif command == "connect":
            self.connected = True
            self.log("已连接 " + result["address"] + "；开始定时只读截图。")
        elif command == "capture":
            self.show_frame(result["png"])
            self.log(f"截图已保存：{result.get('saved')}；区域框图：{result.get('overlay', '未生成')}")
        elif command == "inspect":
            self.show_frame(result["png"])
            self.last_inspection_valid = bool(result.get("valid"))
            self.layout_confirmed.setChecked(False)
            data = {k: v for k, v in result.items() if k != "png"}
            self.ocr_out.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            self.log("OCR 识别有效；请核对题目和区域框图后自行确认布局。" if result.get("valid")
                     else "OCR 未通过严格校验：" + result.get("error", "未知原因"))
        elif command == "guard":
            self.last_inspection_valid = True
            self.layout_confirmed.setChecked(False)
            self.log("题号、题干和选项校验通过；核对布局后勾选确认即可提交。")
        elif command == "answer":
            self.answer_out.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            decision = result.get("decision") or {}
            if result.get("click_sent") and decision.get("attempt_id"):
                self.attempt_edit.setText(decision["attempt_id"])
                self.log("已点击一次；并非已答对。请在题库页按游戏实际反馈记录结果。")
                self.tabs.setCurrentIndex(0)
            else:
                self.log("本题仅完成查询／预览，未点击游戏。")
        elif command == "auto_answer":
            state = "本轮最后一题已提交" if result["reason"] == "completed" else "已停止"
            self.log(f"自动答题{state}；本次共提交 {result['submitted']} 题，未自动判定对错。")
        elif command == "inspect_daily":
            self.show_frame(result["png"])
            self.answer_out.setPlainText(json.dumps({k: v for k, v in result.items() if k != "png"}, ensure_ascii=False, indent=2))
            self.log("日常列表识别通过；请核对任务次数与按钮。" if result["valid"] else "日常识别暂停：" + result["error"])
            self.log("日常诊断目录：" + result["diagnostics"])
        elif command == "auto_daily":
            self.answer_out.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            states = {"completed": "所选日常均已核对完成", "stopped": "已停止后续操作",
                      "needs_attention": "竞技场缺少可用金扫帚，仍有未完成项"}
            self.log(states[result["reason"]])
        elif command == "bank":
            self.bank_table.setRowCount(len(result))
            for i, row in enumerate(result):
                for col, field in enumerate(("status", "stem", "answer_text", "model", "updated_at")):
                    cell = QTableWidgetItem(str(row.get(field) or ""))
                    cell.setToolTip(str(row.get("reason") or ""))
                    self.bank_table.setItem(i, col, cell)
            self.log(f"题库载入 {len(result)} 条记录（仅当前账号 scope）。")
        elif command == "feedback":
            self.log(f"人工反馈已记录：{result['outcome']}；可刷新题库。")
        self._refresh_controls()

    def show_frame(self, content: bytes):
        self.raw_preview = content
        self._rescale_preview()

    def _rescale_preview(self):
        if not self.raw_preview:
            return
        pix = QPixmap()
        if pix.loadFromData(self.raw_preview):
            target = self.preview_label.size() - QSize(8, 8)
            self.preview_label.setPixmap(pix.scaled(target, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "preview_label"):
            self._rescale_preview()

    def open_diagnostics(self):
        folder = ROOT / "data/maa/inspection"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_guide(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / "docs/EMULATOR.md")))

    def log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        if hasattr(self, "logs"):
            self.logs.appendPlainText(f"[{stamp}] {message}")

    def closeEvent(self, event):
        # Closing a worker in the middle of an ADB command/paid request is unsafe.
        if self.current_busy or self.worker.busy or self.stopping or self.worker.stop_requested:
            QMessageBox.information(self, "操作尚未结束", "请先点击停止 / 断开，等待当前操作返回后关闭窗口。")
            event.ignore()
            return
        self.worker.shutdown()
        self.worker.thread.join(timeout=3)
        if self.worker.thread.is_alive():
            QMessageBox.warning(self, "设备尚未释放", "设备线程仍在停止中，请稍后再关闭。")
            event.ignore()
            return
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QWidget { font-size: 13px; }
        QMainWindow, QWidget#mainContent { background: #f5f7fb; }
        QLabel#mainTitle { font-size: 23px; font-weight: 750; color: #172554; }
        QLabel#status { background: #eef2ff; padding: 7px 14px; border-radius: 8px; color: #3730a3; }
        QGroupBox { border: 1px solid #dbe2ee; border-radius: 10px; margin-top: 14px;
                    background: #ffffff; padding: 11px 7px 7px; font-weight: 600; }
        QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; }
        QPushButton { padding: 7px 10px; background: #e9eefb; border: 1px solid #d1d9ed;
                      border-radius: 6px; color: #172554; }
        QPushButton:hover { background: #dce6ff; }
        QPushButton:disabled { color: #9aa6b8; background: #f1f3f8; }
        QLineEdit, QComboBox, QSpinBox { padding: 6px; border: 1px solid #cad4e5; border-radius: 6px; }
        QPlainTextEdit, QTableWidget { background: #fbfcff; border: 1px solid #dbe2ee;
                                     border-radius: 7px; font-weight: 400; }
        QLabel#preview { background: #141c31; color: #c6d4f3; border-radius: 8px; }
        QPlainTextEdit#logs { font-family: Consolas, 'Microsoft YaHei UI', monospace; }
        QTabWidget::pane { border: 1px solid #dbe2ee; border-radius: 8px; }
        QTabBar::tab { padding: 9px 15px; margin-right: 2px; }
        QTabBar::tab:selected { background: #e6edff; border-radius: 6px; }
    """)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
