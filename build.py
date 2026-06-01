#!/usr/bin/env python3
"""
build.py — wshp PyInstaller build orchestrator
───────────────────────────────────────────────
Usage:
    python build.py           # full release build  (dist\wshp\wshp.exe)
    python build.py --clean   # wipe dist\ and build\ before building
    python build.py --ico     # only regenerate the ICO, don't build

Steps
─────
1. Generate assets/icon.png  (if missing)
2. Convert  assets/icon.png → assets/icon.ico  (multi-resolution Windows icon)
3. Run PyInstaller with wshp.spec
4. Print a size report and the path to the finished executable
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

# ── Helpers ────────────────────────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).parent.resolve()
DIST = ROOT / "dist" / "wshp"
EXE = DIST / "wshp.exe"


def banner(msg: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  {msg}")
    print(f"{'-' * 60}")


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)


def dir_size_mb(path: pathlib.Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1_048_576


# ── Build steps ────────────────────────────────────────────────────────────────


def generate_png() -> pathlib.Path:
    """Regenerate assets/icon.png using the icon_gen module."""
    sys.path.insert(0, str(ROOT))
    from assets.icon_gen import OUT_PNG, save  # type: ignore

    save()
    ok(f"icon.png -> {OUT_PNG}  ({OUT_PNG.stat().st_size // 1024} KB)")
    return OUT_PNG


def generate_ico(png_path: pathlib.Path) -> pathlib.Path:
    """
    Convert the PNG to a multi-resolution Windows ICO file.
    Sizes included: 16, 24, 32, 48, 64, 128, 256 — covers every Windows
    context from tray icons (16 px) to app-list tiles (256 px).
    """
    from PIL import Image  # type: ignore

    ico_path = png_path.parent / "icon.ico"
    img = Image.open(png_path).convert("RGBA")
    img.save(
        ico_path,
        format="ICO",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    ok(f"icon.ico -> {ico_path}  ({ico_path.stat().st_size // 1024} KB)")
    return ico_path


def clean_artifacts() -> None:
    """Remove dist\ and build\ so PyInstaller starts completely fresh."""
    for folder in [ROOT / "dist", ROOT / "build"]:
        if folder.exists():
            shutil.rmtree(folder)
            ok(f"Removed {folder}")


def run_pyinstaller() -> None:
    """Invoke PyInstaller with the project spec file."""
    spec = ROOT / "wshp.spec"
    if not spec.exists():
        fail(f"wshp.spec not found at {spec}")
        sys.exit(1)

    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "-y", str(spec)]
    print(f"\n  Running: {' '.join(cmd)}\n")

    t0 = time.perf_counter()
    result = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.perf_counter() - t0

    if result.returncode != 0:
        fail(f"PyInstaller exited with code {result.returncode}")
        sys.exit(result.returncode)

    ok(f"PyInstaller finished in {elapsed:.0f} s")


def report() -> None:
    """Print a summary of the build output."""
    if not DIST.exists():
        fail("dist\\wshp directory not found — build may have failed.")
        return

    total_mb = dir_size_mb(DIST)

    banner("Build complete")

    if EXE.exists():
        exe_mb = EXE.stat().st_size / 1_048_576
        print(f"  Executable : {EXE}")
        print(f"  EXE size   : {exe_mb:.1f} MB")
    else:
        fail("wshp.exe not found inside dist\\wshp\\")

    print(f"  Total size : {total_mb:.0f} MB  ({DIST})")
    print()
    print("  To distribute: copy the entire  dist\\wshp\\  folder.")
    print("  NOTE: The user must have CUDA 12 + cuDNN 9 installed for GPU")
    print("        acceleration.  The app falls back to CPU if CUDA is absent.")
    print()
    print("  Whisper models are downloaded to  %USERPROFILE%\\.wshp\\models\\")
    print("  on first launch and are NOT included in the bundle.")


# ── Entry point ────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build wshp.exe with PyInstaller")
    p.add_argument(
        "--clean",
        action="store_true",
        help="Delete dist\\ and build\\ before building",
    )
    p.add_argument(
        "--ico",
        action="store_true",
        help="Only regenerate assets/icon.ico, then exit",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    banner("wshp build")
    print(f"  Root : {ROOT}")
    print(f"  Python: {sys.executable}")

    # ── Step 1: Icons ──────────────────────────────────────────────────────────
    banner("Generating icons")
    png = ROOT / "assets" / "icon.png"
    if not png.exists():
        png = generate_png()
    else:
        ok(f"icon.png exists  ({png.stat().st_size // 1024} KB)")

    generate_ico(png)

    if args.ico:
        print("\n  --ico flag set; skipping PyInstaller.\n")
        return

    # ── Step 2: Clean (optional) ───────────────────────────────────────────────
    if args.clean:
        banner("Cleaning previous build artifacts")
        clean_artifacts()

    # ── Step 3: PyInstaller ────────────────────────────────────────────────────
    banner("Running PyInstaller")
    run_pyinstaller()

    # ── Step 4: Report ─────────────────────────────────────────────────────────
    report()


if __name__ == "__main__":
    main()
