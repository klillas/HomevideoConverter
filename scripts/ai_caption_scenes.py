#!/usr/bin/env python3
"""ai_caption_scenes.py

Batch-captions existing scene video files by:
  1) extracting a few representative frames
  2) stitching them into a storyboard image
  3) sending the image to a local Ollama vision model (default: moondream)
  4) optionally renaming files with a filename-safe short caption

Designed for Raspberry Pi workflows.

Requirements on the machine running this:
  - ffmpeg
  - ImageMagick (montage)
  - ollama running locally (http://localhost:11434)

Notes:
  - This script does NOT modify video content.
  - Renaming is optional (use --rename).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


@dataclass(frozen=True)
class FramePlan:
    count: int
    fractions: tuple[float, ...]


DEFAULT_FRAME_PLAN = FramePlan(count=3, fractions=(0.10, 0.50, 0.90))


def run(cmd: list[str], *, check: bool = True, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True, timeout=timeout_s)


def which_or_die(exe: str) -> None:
    from shutil import which

    if which(exe) is None:
        raise SystemExit(f"Missing dependency in PATH: {exe}")


def ffprobe_duration_seconds(video: Path, *, timeout_s: int = 30) -> float:
    p = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        timeout_s=timeout_s,
    )
    try:
        return float(p.stdout.strip())
    except ValueError as e:
        raise RuntimeError(f"Failed to parse duration for {video}: {p.stdout!r}") from e


def extract_frames(
    video: Path,
    out_dir: Path,
    plan: FramePlan,
    *,
    max_width: int = 640,
    timeout_s: int = 60,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = ffprobe_duration_seconds(video)

    frame_paths: list[Path] = []
    for i, frac in enumerate(plan.fractions[: plan.count], start=1):
        t = max(0.0, min(dur, dur * frac))
        out = out_dir / f"frame_{i:02d}.jpg"

        # Downscale to reduce CPU + memory pressure for the later montage + VLM.
        vf = f"scale='min({max_width},iw)':-2"

        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "error",
                "-ss",
                f"{t:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                vf,
                "-q:v",
                "3",
                "-y",
                str(out),
            ],
            timeout_s=timeout_s,
        )
        frame_paths.append(out)

    return frame_paths


def stitch_storyboard(frames: list[Path], storyboard_path: Path, *, timeout_s: int = 30) -> None:
    cmd = [
        "montage",
        *[str(p) for p in frames],
        "-tile",
        f"{len(frames)}x1",
        "-geometry",
        "+0+0",
        str(storyboard_path),
    ]
    run(cmd, timeout_s=timeout_s)


def to_filename_slug(text: str) -> str:
    text = text.strip().lower()
    # Replace separators with underscore
    text = re.sub(r"[\s\-]+", "_", text)
    # Keep only safe chars
    text = re.sub(r"[^a-z0-9_]+", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "scene"


def ollama_generate_caption(
    storyboard_path: Path,
    *,
    model: str,
    prompt: str,
    ollama_url: str,
    timeout_s: int,
) -> str:
    if requests is None:
        raise SystemExit("Python package 'requests' is required. Install with: pip install requests")

    b64 = base64.b64encode(storyboard_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
    }

    r = requests.post(
        ollama_url.rstrip("/") + "/api/generate",
        json=payload,
        timeout=timeout_s,
    )
    r.raise_for_status()
    data = r.json()
    resp = (data.get("response") or "").strip()
    return resp


def iter_scene_files(paths: Iterable[Path], glob_pattern: str) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.glob(glob_pattern)))
        elif p.is_file():
            files.append(p)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Caption existing scene video files using a storyboard + local Ollama vision model.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="One or more scene files and/or folders containing scenes.",
    )
    parser.add_argument(
        "--glob",
        default="*.mp4",
        help="When an input is a folder, caption files matching this glob (default: *.mp4).",
    )
    parser.add_argument(
        "--model",
        default="moondream",
        help="Ollama model name (default: moondream).",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama base URL (default: http://localhost:11434).",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "Look at this storyboard of a home video clip. "
            "Provide a 4 to 5 word description suitable for a filename, "
            "using underscores (e.g., kids_playing_in_snow)."
        ),
        help="Prompt sent to the model.",
    )
    parser.add_argument(
        "--probe-seconds",
        type=int,
        default=180,
        help="(reserved) Not used; kept for future parity with audio probing.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout (seconds) for each Ollama request.",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help=(
            "Folder to store temporary frames/storyboards. "
            "Default prefers /mnt/homevideos/_ai_caption_work if available, otherwise ./_ai_caption_work."
        ),
    )
    parser.add_argument(
        "--allow-sd-temp",
        action="store_true",
        help="Allow temp work dir on the SD/root filesystem (not recommended).",
    )
    parser.add_argument(
        "--rename",
        action="store_true",
        help="Rename files by appending the caption slug.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions but do not rename.",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=640,
        help="Downscale extracted frames to this max width (default: 640).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N files (0 = no limit).",
    )
    parser.add_argument(
        "--skip-if-captioned",
        action="store_true",
        help="Skip files that already end with *_<slug>.mp4 (simple underscore heuristic).",
    )
    parser.add_argument(
        "--ffprobe-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for ffprobe (default: 30).",
    )
    parser.add_argument(
        "--ffmpeg-timeout",
        type=int,
        default=120,
        help="Timeout in seconds for each ffmpeg thumbnail extraction (default: 120).",
    )
    parser.add_argument(
        "--montage-timeout",
        type=int,
        default=60,
        help="Timeout in seconds for montage (default: 60).",
    )

    args = parser.parse_args()

    which_or_die("ffmpeg")
    which_or_die("ffprobe")
    which_or_die("montage")

    # Choose a safe default work dir: external drive if mounted.
    if args.work_dir:
        work_root = Path(args.work_dir)
    else:
        ext = Path("/mnt/homevideos")
        work_root = (ext / "_ai_caption_work") if ext.is_dir() else Path("_ai_caption_work")

    # Refuse to write temp work on the SD/root filesystem unless explicitly allowed.
    if not args.allow_sd_temp:
        try:
            resolved = work_root.resolve()
        except FileNotFoundError:
            resolved = work_root.absolute()

        # Heuristic: SD root is typically under / or /home; external drive under /mnt/homevideos.
        if not str(resolved).startswith("/mnt/homevideos/"):
            raise SystemExit(
                f"Refusing to use temp work dir on SD/root filesystem: {resolved}\n"
                "Pass --work-dir /mnt/homevideos/_ai_caption_work (recommended) or use --allow-sd-temp to override."
            )

    work_root.mkdir(parents=True, exist_ok=True)

    scene_files = iter_scene_files([Path(p) for p in args.inputs], args.glob)
    if not scene_files:
        raise SystemExit("No input files found.")

    if args.limit and args.limit > 0:
        scene_files = scene_files[: args.limit]

    total = len(scene_files)
    for idx, scene in enumerate(scene_files, start=1):
        if args.skip_if_captioned and re.search(r"_[a-z0-9]{3,}(?:_[a-z0-9]{3,})+$", scene.stem):
            print(f"[{idx}/{total}] SKIP (looks captioned): {scene.name}")
            continue

        print(f"\n[{idx}/{total}] === {scene} ===")
        t0 = time.time()

        scene_work = work_root / to_filename_slug(scene.stem)
        frames_dir = scene_work / "frames"
        storyboard = scene_work / "storyboard.jpg"

        try:
            print("  - extracting frames...")
            frames = extract_frames(
                scene,
                frames_dir,
                DEFAULT_FRAME_PLAN,
                max_width=args.max_width,
                timeout_s=args.ffmpeg_timeout,
            )

            print("  - stitching storyboard...")
            stitch_storyboard(frames, storyboard, timeout_s=args.montage_timeout)

            print("  - querying ollama...")
            caption = ollama_generate_caption(
                storyboard,
                model=args.model,
                prompt=args.prompt,
                ollama_url=args.ollama_url,
                timeout_s=args.timeout,
            )
        except subprocess.TimeoutExpired as e:
            print(f"  ERROR: timed out while running: {' '.join(e.cmd)}")
            continue
        except subprocess.CalledProcessError as e:
            show = (e.stderr or e.stdout or "").strip()
            print(f"  ERROR: command failed: {' '.join(e.cmd)}")
            if show:
                print(f"  OUTPUT: {show[-500:]}")
            continue

        slug = to_filename_slug(caption)
        print(f"Caption: {caption}")
        print(f"Slug:    {slug}")
        print(f"Done in: {time.time() - t0:.1f}s")

        if args.rename:
            new_name = f"{scene.stem}_{slug}{scene.suffix}"
            new_path = scene.with_name(new_name)
            if args.dry_run:
                print(f"DRY RUN rename: {scene} -> {new_path}")
            else:
                scene.rename(new_path)
                print(f"Renamed: {new_path}")


if __name__ == "__main__":
    main()
