"""Windows bundle entry and offline smoke check; never connects to a device."""
import json
import struct
import sys
import tempfile
import traceback
from pathlib import Path


def self_test(report_path):
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from gui_app import MainWindow
        from gui_backend import local_settings
        from maa_adapter import ROOT, load_config, load_maa, wait_job
        from quiz_core import QuizStore
        from quiz_vision import Layout
        from setup_ocr import verify_models

        cfg = load_config()
        verify_models(cfg["resource_dir"] / "model/ocr")
        Layout.load(cfg["layout"])
        Layout.load(ROOT / "config/ring.layout.json")
        api = load_maa()
        # Keep smoke-check logs and databases out of the shipped data directory.
        with tempfile.TemporaryDirectory(prefix="portable-check-", dir=ROOT) as tmp:
            temporary = Path(tmp)
            if not api["Toolkit"].init_option(str(temporary), {"logging": False}):
                raise RuntimeError("Maa initialization failed")
            resource = api["Resource"]()
            wait_job(resource.post_bundle(str(cfg["resource_dir"])), 30, "OCR resource loading")
            if not resource.loaded:
                raise RuntimeError("OCR resource bundle is not loaded")
            settings = temporary / "local.json"
            settings.write_text(json.dumps(local_settings(), ensure_ascii=False), encoding="utf-8")
            load_config(settings)
            store = QuizStore(temporary / "quiz.sqlite3")
            store.close()
            app = QApplication([])
            window = MainWindow()
            window.show()
            app.processEvents()
            if not window.isVisible():
                raise RuntimeError("GUI did not open")
            window.tabs.setCurrentIndex(3)
            app.processEvents()
            if window.layout_edit.text() != "config/dali.layout.json":
                raise RuntimeError("GUI did not load the default layout")
            if not window.grab().save(str(Path(report_path).with_suffix(".png"))):
                raise RuntimeError("GUI screenshot could not be saved")
            QTimer.singleShot(300, window.close)
            if app.exec() != 0 or window.worker.thread.is_alive():
                raise RuntimeError("GUI did not shut down cleanly")
        report = {"ok": True, "bits": struct.calcsize("P") * 8, "root": str(ROOT),
                  "checks": ["models", "layouts", "maa_native_resource", "config_io", "sqlite", "gui"],
                  "device_connected": False}
    except Exception:
        report = {"ok": False, "error": traceback.format_exc()}
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(sys.argv[2]))
    from gui_app import main
    raise SystemExit(main())
