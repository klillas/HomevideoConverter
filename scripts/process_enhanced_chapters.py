#!/usr/bin/env python3

import argparse
import subprocess
import sys
import os
import shutil
import re
from pathlib import Path

# --- Configuration ---
# This section mirrors the configuration variables from the bash script.
# Feel free to adjust these values to suit your needs.

# --- Input and Configuration ---
OUTPUT_FOLDER_DEFAULT = "enhanced_videos"
OUTPUT_FILENAME_SUFFIX = "_enhanced"

# --- Auto-Chapter Settings ---
SCENE_THRESHOLD = 10.0  # Scene detection threshold. Higher for noisy video.
MIN_CHAPTER_DURATION = 15.0  # Minimum length of a chapter in seconds.

# --- Video Encoding (WhatsApp Optimized) ---
CRF = "24"  # Constant Rate Factor (lower is better quality, 18-28 is a sane range).
MAX_BITRATE = "2.5M"
BUF_SIZE = "5M"

# --- Video Enhancement (FFmpeg) ---
DEINTERLACE = True
DENOISE_STRENGTH = "4" # Corresponds to luma_spatial=4 in hqdn3d
SHARPEN_STRENGTH = "0.3"

BRIGHTNESS = "0.05"
CONTRAST = "1.1"
SATURATION = "1.2"
FILM_GRAIN = "0.05" # Strength of added noise

# --- Audio Enhancement ---
HIGH_PASS_FREQ = "80"
LOW_PASS_FREQ = "10k"
NORMALIZE_AUDIO = True
TARGET_LOUDNESS = "-23" # EBU R128 standard for broadcast is -23 LUFS.

def check_dependencies():
    """Checks if required command-line tools are installed."""
    dependencies = ['ffmpeg', 'ffprobe', 'sox']
    # Also require python3 for invoking the extractor
    for dep in dependencies:
        if not shutil.which(dep):
            print(f"Error: Required dependency '{dep}' not found in PATH.")
            print("Please install it and try again.")
            sys.exit(1)


def run_command(command, description):
    """Runs a command, prints its description, and checks for errors."""
    print(f"Executing: {description}")
    try:
        # Using shell=False and passing args as a list is safer
        process = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        return process
    except subprocess.CalledProcessError as e:
        print(f"Error while {description.lower()}:")
        print(f"Command: {' '.join(e.cmd)}")
        print(f"Return Code: {e.returncode}")
        print(f"STDOUT: {e.stdout.strip()}")
        print(f"STDERR: {e.stderr.strip()}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Command '{command[0]}' not found. Is it installed and in your PATH?")
        sys.exit(1)

def get_video_duration(video_path):
    """Gets the total duration of the video in seconds."""
    command = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(video_path)
    ]
    result = run_command(command, "getting video duration")
    try:
        return float(result.stdout.strip())
    except (ValueError, IndexError):
        print("Error: Could not determine video duration.")
        sys.exit(1)

def detect_scenes(video_path):
    """
    Detects scene changes in the video and returns a list of timestamps.
    Applies a denoiser before scene detection for better accuracy on noisy sources.
    """
    print("--- Phase 1: Detecting Chapters (Using denoising for accuracy) ---")
    command = [
        'ffmpeg',
        '-i', str(video_path),
        '-vf', f'hqdn3d=luma_spatial={DENOISE_STRENGTH},scdet=threshold={SCENE_THRESHOLD}',
        '-an',
        '-f', 'null',
        '-'
    ]
    
    print("Executing: detecting scenes (this may take a while)...")
    try:
        # We expect this to fail with a non-zero exit code because it's not creating an output file.
        # The scene detection info is printed to stderr, so we capture that.
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        # Regex to find the scene detection timestamps in ffmpeg's stderr output
        # Example line: [scdet @ 0x7f8c8c00b840] lavfi.scd.time: 21.240000
        scene_times_str = re.findall(r"lavfi\.scd\.time:\s*([\d\.]+)", process.stderr)
        return [float(t) for t in scene_times_str]
        
    except FileNotFoundError:
        print(f"Error: Command 'ffmpeg' not found. Is it installed and in your PATH?")
        sys.exit(1)

def ensure_clean_input(input_video_path: Path, output_folder_path: Path, force: bool = True) -> Path:
    """Create and return a *_CLEAN.mp4 using the extractor script.

    We do this to fix DVD-concatenation timestamp discontinuities so per-scene audio stays synced.
    """
    extractor_script = Path(__file__).with_name('extract_video_and_recombine_to_single_audio_track.py')
    if not extractor_script.exists():
        print(f"Error: extractor script not found: {extractor_script}")
        sys.exit(1)

    cleaned_dir = output_folder_path / "_clean"  # keep intermediates separate
    cleaned_dir.mkdir(parents=True, exist_ok=True)

    cleaned_path = cleaned_dir / f"{input_video_path.stem}_CLEAN.mp4"

    if cleaned_path.exists() and not force:
        print(f"Reusing existing cleaned file: {cleaned_path}")
        return cleaned_path

    print("--- Phase 0: Rebuilding a single synced audio track (CLEAN) ---")
    cmd = [
        sys.executable,
        str(extractor_script),
        "-i",
        str(input_video_path),
        "-o",
        str(cleaned_dir),
    ]
    run_command(cmd, "creating CLEAN intermediate")

    if not cleaned_path.exists():
        # The extractor writes using the stem; double-check by glob.
        candidates = list(cleaned_dir.glob(f"{input_video_path.stem}*_CLEAN.mp4"))
        if candidates:
            return candidates[0]
        print(f"Error: CLEAN file was not created at expected path: {cleaned_path}")
        sys.exit(1)

    return cleaned_path

def main():
    """Main script logic."""
    parser = argparse.ArgumentParser(
        description="A Python script to enhance and split a video file into chapters based on scene changes.\n\n"
                    "This script first generates a *_CLEAN.mp4 (single synced audio track) to prevent audio drift on DVD-concatenated MPEG files.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="Example:\npython process_enhanced_chapters.py my_video.mpg -o /home/admin/Desktop/Finished_Videos"
    )
    parser.add_argument("input_video", help="Path to the input video file.")
    parser.add_argument(
        "-o", "--output-folder",
        default=OUTPUT_FOLDER_DEFAULT,
        help=f"Path to the output folder. Defaults to '{OUTPUT_FOLDER_DEFAULT}'."
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Skip creating the *_CLEAN.mp4 intermediate (not recommended for DVD-concat MPEGs).",
    )
    parser.add_argument(
        "--reuse-clean",
        action="store_true",
        help="Reuse an existing *_CLEAN.mp4 in <output>/_clean if present.",
    )
    args = parser.parse_args()

    check_dependencies()

    input_video_path = Path(args.input_video)
    output_folder_path = Path(args.output_folder)

    if not input_video_path.is_file():
        print(f"Error: Input file not found at '{input_video_path}'")
        sys.exit(1)

    output_folder_path.mkdir(parents=True, exist_ok=True)

    # --- Phase 0: CLEAN intermediate ---
    if args.no_clean:
        clean_input_path = input_video_path
        print("WARNING: --no-clean specified; using original input for scene splitting.")
    else:
        clean_input_path = ensure_clean_input(
            input_video_path,
            output_folder_path,
            force=not args.reuse_clean,
        )

    # --- Script Logic Setup ---
    basename = clean_input_path.stem
    temp_dir = output_folder_path / "temp_work"
    
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Starting enhancement process for: {clean_input_path}")
    print(f"Output will be saved to: {output_folder_path}")
    
    try:
        # ==========================================
        # PHASE 1: SCENE DETECTION (IMPROVED)
        # ==========================================
        total_duration = get_video_duration(clean_input_path)
        scene_times = detect_scenes(clean_input_path)

        cuts = [0.0]
        last_cut = 0.0
        for time in scene_times:
            if time - last_cut > MIN_CHAPTER_DURATION:
                cuts.append(time)
                last_cut = time
        cuts.append(total_duration)

        # Remove duplicates just in case and sort
        cuts = sorted(list(set(cuts)))

        num_chapters = len(cuts) - 1
        print(f"Found {num_chapters} distinct chapters/scenes.")
        if num_chapters > 100:
            print("WARNING: Still found a very high number of chapters. The source may be very noisy.")
            print(f"Consider increasing SCENE_THRESHOLD (current: {SCENE_THRESHOLD}) if results are poor.")

        # ==========================================
        # PHASE 2: PROCESSING CHAPTERS
        # ==========================================
        print("\n--- Phase 2: Enhancing and Encoding Chapters ---")
        
        video_filters = []
        if DEINTERLACE:
            video_filters.append("bwdif=mode=send_field:parity=auto:deint=all")
        
        # Note: Denoiser is run twice, once for scene detection and once here for final output.
        # This is intentional to ensure both processes are optimal.
        video_filters.extend([
            f"hqdn3d=luma_spatial={DENOISE_STRENGTH}",
            f"unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount={SHARPEN_STRENGTH}",
            f"eq=brightness={BRIGHTNESS}:contrast={CONTRAST}:saturation={SATURATION}",
            f"noise=alls={FILM_GRAIN}:allf=t+u"
        ])
        ffmpeg_video_filters_str = ",".join(video_filters)
        
        for i in range(num_chapters):
            chapter_num = i + 1
            start_time = cuts[i]
            end_time = cuts[i+1]
            duration = end_time - start_time
            
            output_video_path = output_folder_path / f"{basename}_{chapter_num:02d}{OUTPUT_FILENAME_SUFFIX}.mp4"
            
            print("=" * 55)
            print(f"Processing Chapter {chapter_num} of {num_chapters}")
            print(f"Time: {start_time:.2f}s to {end_time:.2f}s (Duration: {duration:.2f}s)")
            print("=" * 55)
            
            temp_audio = temp_dir / f"temp_audio_{chapter_num:02d}.wav"
            enhanced_audio = temp_dir / f"enhanced_audio_{chapter_num:02d}.wav"

            # 1. Extract audio for the chapter
            cmd_extract = [
                'ffmpeg', '-v', 'error', '-stats',
                '-ss', str(start_time),
                '-t', str(duration),
                '-i', str(clean_input_path),
                '-vn', '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2',
                str(temp_audio), '-y'
            ]
            run_command(cmd_extract, f"extracting audio for chapter {chapter_num}")
            
            # 2. Enhance audio with SoX (band-pass filter)
            cmd_sox = [
                'sox',
                str(temp_audio),
                str(enhanced_audio),
                'sinc', f"{HIGH_PASS_FREQ}-{LOW_PASS_FREQ}"
            ]
            run_command(cmd_sox, f"applying audio filter for chapter {chapter_num}")
            
            # 3. (Optional) Normalize audio loudness
            if NORMALIZE_AUDIO:
                normalized_audio = temp_dir / f"normalized_audio_{chapter_num:02d}.wav"
                cmd_norm = [
                    'ffmpeg', '-v', 'error', '-stats',
                    '-i', str(enhanced_audio),
                    '-af', f"loudnorm=I={TARGET_LOUDNESS}:TP=-1.5:LRA=11",
                    '-ar', '44100',
                    str(normalized_audio), '-y'
                ]
                run_command(cmd_norm, f"normalizing audio for chapter {chapter_num}")
                # Replace enhanced with normalized version
                shutil.move(str(normalized_audio), str(enhanced_audio))
            
            # 4. Combine video segment with enhanced audio and apply all filters
            cmd_final = [
                'ffmpeg', '-v', 'error', '-stats',
                '-ss', str(start_time),
                '-t', str(duration),
                '-i', str(clean_input_path),
                '-i', str(enhanced_audio),
                '-c:v', 'libx264',
                '-preset', 'veryfast',
                '-crf', CRF,
                '-maxrate', MAX_BITRATE,
                '-bufsize', BUF_SIZE,
                '-vf', ffmpeg_video_filters_str,
                '-c:a', 'aac',
                '-b:a', '256k',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-movflags', '+faststart',
                '-y', str(output_video_path)
            ]
            run_command(cmd_final, f"encoding final video for chapter {chapter_num}")
            
            print(f"Completed Chapter {chapter_num} -> {output_video_path}\n")

    finally:
        # ==========================================
        # CLEANUP
        # ==========================================
        print("Cleaning up temporary files...")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            
    print("=" * 55)
    print(f"ALL FINISHED! Processed {num_chapters} chapters.")
    print(f"Videos are located in: {output_folder_path}")

if __name__ == "__main__":
    main()
