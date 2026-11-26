"""Build a fresh Windows x64 portable directory and ZIP; never overwrite releases."""
from __future__ import annotations

import json
import platform
import shutil
import struct
import subprocess
import sys
from datetime import datetime
from importlib.metadata import distribution
from pathlib import Path

from quiz_core import QuizError
from setup_ocr import FILES, verify_models

ROOT = Path(__file__).resolve().parent
APP_NAME = "DHXY-Assistant"
RELEASE_FILES = (
    "config/connection.example.json", "config/dali.layout.json", "config/ring.layout.json",
    "config/ai.example.json", "assets/resource/pipeline/dali.json",
    *(f"assets/resource/model/ocr/{name}" for name in FILES),
    "docs/EMULATOR.md", "docs/GUI.md", "docs/WINDOWS_BUILD.md",
)
DEPENDENCIES = ("maafw", "MaaAgentBinary", "StrEnum", "numpy", "Pillow",
                "PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6")


def copy_release_files(destination: Path):
    # An explicit allowlist keeps local keys, screenshots and databases out of releases.
    for name in RELEASE_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)


def copy_notices(destination: Path):
    for name in DEPENDENCIES:
        package = distribution(name)
        for entry in package.files or ():
            if any(word in entry.name.lower() for word in ("license", "copying", "notice")):
                target = destination / "licenses" / name / entry
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package.locate_file(entry), target)
    versions = {name: distribution(name).version for name in DEPENDENCIES}
    (destination / "dependency-versions.json").write_text(
        json.dumps(versions, indent=2), encoding="utf-8")


def verify_runtime(bundle: Path):
    required = {"msvcp140.dll", "msvcp140_1.dll", "msvcp140_atomic_wait.dll", "concrt140.dll",
                "vcruntime140.dll", "vcruntime140_1.dll"}
    included = {path.name.lower() for path in (bundle / "_internal").rglob("*.dll")}
    missing = required - included
    if missing:
        raise RuntimeError(f"Missing bundled VC runtime DLLs: {', '.join(sorted(missing))}")


def run(*args, **kwargs):
    subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def build():
    if (sys.platform != "win32" or struct.calcsize("P") != 8
            or platform.machine().upper() not in ("AMD64", "X86_64")):
        raise RuntimeError("Build with Windows x64 Python, not 32-bit or ARM64 Python.")
    verify_models()
    run(sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-q")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    release = ROOT / "dist" / f"{APP_NAME}-win64-{stamp}"
    release.mkdir(parents=True, exist_ok=False)
    work = ROOT / "build/windows" / stamp
    work.mkdir(parents=True, exist_ok=False)
    agents = Path(distribution("MaaAgentBinary").locate_file("MaaAgentBinary"))
    run(sys.executable, "-X", "utf8", "-m", "PyInstaller", "--onedir", "--windowed",
        "--noupx", "--name", APP_NAME, "--distpath", str(release),
        "--workpath", str(work / "work"), "--specpath", str(work),
        "--collect-all", "maa", "--copy-metadata", "maafw",
        "--add-data", f"{agents}:MaaAgentBinary", "windows_entry.py")
    bundle = release / APP_NAME
    verify_runtime(bundle)
    copy_release_files(bundle)
    copy_notices(bundle)
    (bundle / "README.txt").write_text(
        "完整解压后双击 DHXY-Assistant.exe，无需安装 Python。\n"
        "请保留 _internal、assets、config；配置和题库保存在应用目录内。\n"
        "使用、升级和注意事项见 docs/WINDOWS_BUILD.md。\n", encoding="utf-8-sig")
    report = work / "smoke-test.json"
    # A different working directory catches accidental reliance on the source tree.
    subprocess.run([str(bundle / f"{APP_NAME}.exe"), "--self-test", str(report)],
                   cwd=work, check=True, timeout=90)
    result = json.loads(report.read_text(encoding="utf-8"))
    if not result.get("ok") or result.get("bits") != 64 or Path(result["root"]) != bundle:
        raise RuntimeError(f"Portable self-test failed; see {report}")
    if (bundle / "data").exists() or {p.name for p in (bundle / "config").iterdir()} != {
            "connection.example.json", "dali.layout.json", "ring.layout.json", "ai.example.json"}:
        raise RuntimeError("Unexpected runtime data or local config in release; ZIP was not created.")
    archive = shutil.make_archive(str(release), "zip", root_dir=release)
    print(f"\nPortable app: {bundle}\nZIP: {archive}\nSelf-test: {report}", flush=True)


def main():
    try:
        build()
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError, QuizError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
