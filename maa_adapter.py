"""MaaFramework 5.15.0 adapter. Optional imports; no game login or credential access."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from quiz_core import QuizError

ROOT = Path(__file__).resolve().parent
MAA_VERSION = "5.15.0"


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = json.loads((ROOT / "config/connection.example.json").read_text(encoding="utf-8"))
    if path is not None:
        supplied = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(supplied, dict) or set(supplied) - set(cfg):
            raise QuizError("Unknown connection config keys; compare connection.example.json.")
        cfg.update(supplied)
    for name in ("resource_dir", "layout", "database", "runtime_dir"):
        p = Path(cfg[name]).expanduser()
        cfg[name] = p if p.is_absolute() else ROOT / p
    if not isinstance(cfg["timeout_seconds"], (int, float)) or not 1 <= cfg["timeout_seconds"] <= 120:
        raise QuizError("timeout_seconds must be between 1 and 120.")
    for name in ("adb_path", "address", "account_scope"):
        if not isinstance(cfg[name], str):
            raise QuizError(f"{name} must be text.")
    if not cfg["account_scope"].strip():
        raise QuizError("account_scope must identify this single game account.")
    return cfg


def load_maa():
    try:
        from importlib.metadata import version
        if version("maafw") != MAA_VERSION:
            raise QuizError(f"Use maafw=={MAA_VERSION}; this adapter pins the Python API.")
        from maa.controller import AdbController
        from maa.define import MaaAdbScreencapMethodEnum, MaaAdbInputMethodEnum
        from maa.pipeline import JOCR
        from maa.resource import Resource
        from maa.tasker import Tasker
        from maa.toolkit import Toolkit
    except ImportError:
        raise QuizError("Install device dependencies: python -m pip install -r requirements-device.txt") from None
    return dict(AdbController=AdbController, Screen=MaaAdbScreencapMethodEnum,
                Input=MaaAdbInputMethodEnum, JOCR=JOCR, Resource=Resource,
                Tasker=Tasker, Toolkit=Toolkit)


def wait_job(job, timeout: float, operation: str, *, require_success: bool = True):
    """Bound Python polling; native driver teardown can still depend on the driver."""
    deadline = time.monotonic() + timeout
    while not job.done:
        if time.monotonic() >= deadline:
            raise QuizError(f"{operation} timed out; stop and inspect the connection, do not retry a click.")
        time.sleep(0.05)
    if require_success and not job.succeeded:
        raise QuizError(f"{operation} failed; no automatic retry.")
    return job


def select_device(devices, address: str = ""):
    # Discovery may list the same instance through different adb installations.
    by_address = {}
    for d in devices:
        by_address.setdefault(d.address, d)
    if address:
        return by_address.get(address)
    if len(by_address) != 1:
        raise QuizError("Expected exactly one device. Set address explicitly; never pick the first of multiple instances.")
    return next(iter(by_address.values()))


class MaaSession:
    def __init__(self, config: dict[str, Any], *, execute: bool = False, api=None):
        self.cfg = config
        self.execute = execute
        self.api = api or load_maa()
        self.controller = None
        self.resource = None
        self.tasker = None
        self.address = ""
        config["runtime_dir"].mkdir(parents=True, exist_ok=True)
        # Logs remain local; do not dump screenshots on native recognition errors.
        if not self.api["Toolkit"].init_option(str(config["runtime_dir"]), {
            "logging": True, "save_draw": False, "save_on_error": False,
        }):
            raise QuizError("MaaFramework initialization failed.")

    def discover(self):
        found = self.api["Toolkit"].find_adb_devices(self.cfg["adb_path"] or None)
        return found

    def device_list(self):
        return [{"name": d.name, "adb_path": str(d.adb_path), "address": d.address}
                for d in self.discover()]

    def connect(self):
        device = select_device(self.discover(), self.cfg["address"])
        if device is not None:
            params = dict(adb_path=device.adb_path, address=device.address,
                          screencap_methods=device.screencap_methods,
                          input_methods=device.input_methods, config=device.config)
        elif self.cfg["address"] and self.cfg["adb_path"]:
            # Manual fallback uses ordinary ADB. No injection, root or emulator extras.
            params = dict(adb_path=Path(self.cfg["adb_path"]), address=self.cfg["address"],
                          screencap_methods=self.api["Screen"].Encode,
                          input_methods=self.api["Input"].AdbShell, config={})
        else:
            raise QuizError("Device not found. Specify both adb_path and address for manual connection.")
        self.address = params["address"]
        self.controller = self.api["AdbController"](**params)
        if not self.controller.set_screenshot_use_raw_size(True):
            raise QuizError("Could not enforce a single raw screenshot/click coordinate space.")
        wait_job(self.controller.post_connection(), self.cfg["timeout_seconds"], "ADB connection")
        if not self.controller.connected:
            raise QuizError("ADB is not connected. Check instance port and debugging authorization.")
        return self

    def ensure_ocr(self):
        if self.tasker is not None:
            return
        if self.controller is None:
            raise QuizError("Connect the device first.")
        from setup_ocr import verify_models
        verify_models(self.cfg["resource_dir"] / "model/ocr")
        self.resource = self.api["Resource"]()
        wait_job(self.resource.post_bundle(str(self.cfg["resource_dir"])),
                 self.cfg["timeout_seconds"], "Resource loading")
        self.tasker = self.api["Tasker"]()
        if not self.tasker.bind(self.resource, self.controller) or not self.tasker.inited:
            raise QuizError("Maa tasker could not bind resources and controller.")

    def capture(self):
        if self.controller is None or not self.controller.connected:
            raise QuizError("No connected device.")
        image = wait_job(self.controller.post_screencap(), self.cfg["timeout_seconds"], "Screenshot").get()
        if image is None or len(image.shape) != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 100:
            raise QuizError("Empty/unsupported screenshot; expected a BGR image.")
        return image.copy()

    def guard_scene(self, roi):
        """A real Maa pipeline with OCR + DoNothing; never clicks."""
        self.ensure_ocr()
        job = self.tasker.post_task("DaliSceneGuard", {
            "DaliSceneGuard": {"roi": list(roi)},
        })
        wait_job(job, self.cfg["timeout_seconds"], "Dali scene guard")

    def ocr(self, image, roi, *, single_line=False):
        self.ensure_ocr()
        # Keep low-score results so a missed negation/digit cannot be silently dropped.
        param = self.api["JOCR"](roi=tuple(roi), expected=[], threshold=0.0,
                                  order_by="Vertical", only_rec=single_line)
        job = self.tasker.post_recognition("OCR", param, image)
        wait_job(job, self.cfg["timeout_seconds"], "OCR", require_success=False)
        detail = job.get()
        if detail is None:
            raise QuizError("No OCR task details; check model installation/native logs.")
        from quiz_vision import TextBlock
        out = []
        for node in detail.nodes:
            reco = node.recognition
            if reco is not None:
                for item in reco.all_results:
                    if hasattr(item, "text"):
                        out.append(TextBlock(item.text, float(item.score), tuple(item.box)))
        return out

    def click(self, x: int, y: int):
        if not self.execute:
            raise QuizError("Device input is disabled. An explicit --execute is required.")
        wait_job(self.controller.post_click(int(x), int(y)), self.cfg["timeout_seconds"], "Answer click")

    def close(self):
        if self.tasker is not None:
            try:
                wait_job(self.tasker.post_stop(), 3, "Stop")
            except QuizError:
                pass


def save_image(image, path: Path, rectangles: dict | None = None):
    from PIL import Image, ImageDraw
    path.parent.mkdir(parents=True, exist_ok=True)
    output = Image.fromarray(image[:, :, ::-1]).convert("RGB")
    if rectangles:
        draw = ImageDraw.Draw(output)
        for name, (x, y, w, h) in rectangles.items():
            draw.rectangle((x, y, x + w - 1, y + h - 1), outline="red", width=2)
            draw.text((x, max(0, y - 12)), name, fill="red")
    output.save(path)
