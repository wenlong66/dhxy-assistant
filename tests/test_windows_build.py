"""Offline checks for portable paths and the release file allowlist."""
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_windows


class WindowsBuildTests(unittest.TestCase):
    def test_source_root_does_not_follow_python_executable(self):
        with patch.object(sys, "frozen", False, create=True):
            module = runpy.run_path(str(build_windows.ROOT / "maa_adapter.py"))
        self.assertEqual(module["ROOT"], build_windows.ROOT)

    def test_frozen_root_and_relative_paths_follow_executable_not_cwd(self):
        with tempfile.TemporaryDirectory(prefix="portable-中文-") as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config/connection.example.json").write_bytes(
                (build_windows.ROOT / "config/connection.example.json").read_bytes())
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "executable", str(root / "DHXY-Assistant.exe")):
                module = runpy.run_path(str(build_windows.ROOT / "maa_adapter.py"))
            self.assertEqual(module["ROOT"], root)
            cfg = module["load_config"]()
            self.assertEqual(cfg["resource_dir"], root / "assets/resource")
            self.assertEqual(cfg["database"], root / "data/quiz.sqlite3")
            self.assertEqual(cfg["runtime_dir"], root / "data/maa")

    def test_release_copies_only_allowlisted_files_not_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "source", Path(tmp) / "release"
            for name in (*build_windows.RELEASE_FILES, "config/ai.local.json", "config/local.json",
                         "data/quiz.sqlite3", "assets/resource/model/ocr/private.png"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test-only", encoding="utf-8")
            with patch.object(build_windows, "ROOT", source):
                build_windows.copy_release_files(target)
            actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
            self.assertEqual(actual, set(build_windows.RELEASE_FILES))

    def test_missing_vc_runtime_is_rejected_before_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "Missing bundled VC runtime"):
                build_windows.verify_runtime(Path(tmp))

    def test_release_requires_every_declared_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(build_windows, "ROOT", Path(tmp)):
            with self.assertRaises(FileNotFoundError):
                build_windows.copy_release_files(Path(tmp) / "release")


if __name__ == "__main__":
    unittest.main()
