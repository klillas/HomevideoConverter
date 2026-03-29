#!/usr/bin/env python3

"""batch_process_folder.py

Batch wrapper around `process_enhanced_chapters.py`.

Given an input root folder and an output root folder, this script:
  - recursively finds video files under the input root
  - for each video, runs `process_enhanced_chapters.py <video> -o <output_subdir>`
  - preserves the relative folder structure from input root into output root

Output:
  <output_root>/<relative_path_from_input>/<video_stem>_*_enhanced.mp4
  plus a CLEAN intermediate in: <output_root>/<relative_path_from_input>/_clean/

Notes:
  - This script does not write temporary work to the SD card if you point the output root to the external drive.
  - It will skip files that already appear processed unless you pass --force.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mpg", ".mpeg", ".m2v", ".avi", ".mkv"}


@dataclass(frozen=True)
class VideoJob:
    input_file: Path
    output_dir: Path
    rel_dir: Path


def iter_video_files(root: Path, *, exts: set[str]) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in exts:
            yield p


def looks_processed(output_dir: Path, stem: str) -> bool:
    # If any enhanced clip exists for this stem, assume processed.
    return any(output_dir.glob(f"{stem}_*_*enhanced.mp4")) or any(output_dir.glob(f"{stem}_*_enhanced.mp4"))


def run_process_enhanced(input_file: Path, output_dir: Path) -> None:
    script = Path(__file__).with_name("process_enhanced_chapters.py")
    if not script.exists():
        raise SystemExit(f"Missing script: {script}")

    cmd = [sys.executable, str(script), str(input_file), "-o", str(output_dir)]
    subprocess.run(cmd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch process a folder tree into WhatsApp-friendly scene clips (preserving folder structure).",
    )
    parser.add_argument("input_root", help="Root folder containing source videos")
    parser.add_argument("output_root", help="Root folder where processed scene clips will be written")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even if output appears to already exist.",
    )
    parser.add_argument(
        "--ext",
        action="append",
        default=None,
        help="Extra file extension(s) to include (e.g. --ext .vob). Can be provided multiple times.",
    )

    args = parser.parse_args()

    input_root = Path(args.input_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()

    if not input_root.is_dir():
        raise SystemExit(f"Input root is not a directory: {input_root}")

    output_root.mkdir(parents=True, exist_ok=True)

    exts = set(VIDEO_EXTS)
    if args.ext:
        exts.update({e if e.startswith(".") else f".{e}" for e in args.ext})

    videos = list(iter_video_files(input_root, exts=exts))
    if not videos:
        print("No videos found.")
        return

    print(f"Found {len(videos)} video(s) under: {input_root}")

    # Create jobs
    jobs: list[VideoJob] = []
    for vf in videos:
        rel_dir = vf.parent.relative_to(input_root)
        out_dir = output_root / rel_dir
        jobs.append(VideoJob(input_file=vf, output_dir=out_dir, rel_dir=rel_dir))

    for i, job in enumerate(jobs, start=1):
        job.output_dir.mkdir(parents=True, exist_ok=True)
        stem = job.input_file.stem

        print("=" * 72)
        print(f"[{i}/{len(jobs)}] {job.input_file}")
        print(f"Output dir: {job.output_dir}")

        if not args.force and looks_processed(job.output_dir, stem):
            print("SKIP: output already appears to exist (use --force to reprocess).")
            continue

        try:
            run_process_enhanced(job.input_file, job.output_dir)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: processing failed for {job.input_file} (exit {e.returncode}).")
            # Continue with remaining files
            continue

    print("\nDone.")


if __name__ == "__main__":
    main()
