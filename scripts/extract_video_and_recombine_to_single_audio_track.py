import subprocess
import os
import argparse
import sys
import re

def format_time(seconds):
    """Converts seconds to a nice HH:MM:SS.sss format."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{mins:02d}:{secs:06.3f}"


def get_video_duration(video_file: str) -> float | None:
    """Return container duration in seconds (best-effort)."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_file,
    ]
    try:
        out = subprocess.check_output(cmd, text=True).strip()
        return float(out) if out else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def _run_ffmpeg_volumedetect(video_file: str, stream_index: int, probe_seconds: int) -> tuple[float, float] | None:
    """Return (mean_db, max_db) for a stream over the first probe_seconds, or None if it can't be measured."""
    # Use global stream index mapping: -map 0:<stream_index>
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "info",
        "-probesize",
        "200M",
        "-analyzeduration",
        "200M",
        "-t",
        str(probe_seconds),
        "-i",
        video_file,
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None

    text_out = (p.stderr or "") + "\n" + (p.stdout or "")
    # Example:
    # [Parsed_volumedetect_0 @ ...] mean_volume: -33.3 dB
    # [Parsed_volumedetect_0 @ ...] max_volume: -0.3 dB
    mean_m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text_out)
    max_m = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text_out)
    if not mean_m or not max_m:
        return None

    try:
        return float(mean_m.group(1)), float(max_m.group(1))
    except ValueError:
        return None


def pick_primary_audio_stream(
    streams: dict[int, str],
    preferred: int | None = None,
    *,
    video_file: str | None = None,
    volume_probe_seconds: int = 180,
) -> int:
    """Pick the single audio stream to use for timeline reconstruction.

    Order:
      1) user override
      2) volume-based detection (if video_file provided)
      3) codec heuristics fallback
    """
    if preferred is not None:
        if preferred not in streams:
            raise ValueError(
                f"Requested audio stream index {preferred} not found. Available: {sorted(streams.keys())}"
            )
        return preferred

    # 1) Volume-based: pick stream with loudest max_volume (closest to 0dB), breaking ties on mean_volume
    if video_file:
        measurements: list[tuple[int, float, float]] = []
        for idx in sorted(streams.keys()):
            m = _run_ffmpeg_volumedetect(video_file, idx, volume_probe_seconds)
            if not m:
                continue
            mean_db, max_db = m
            # Ignore essentially silent tracks (DVD pcm menus often report -91 dB)
            if max_db <= -80.0 and mean_db <= -80.0:
                continue
            measurements.append((idx, mean_db, max_db))

        if measurements:
            # max_db higher is better (e.g. -0.3 is better than -7.0). For mean_db, higher is better too.
            measurements.sort(key=lambda x: (x[2], x[1]), reverse=True)
            best = measurements[0]
            print(
                f"Auto-selected audio stream {best[0]} by volume (mean={best[1]:.1f} dB, max={best[2]:.1f} dB)."
            )
            return best[0]

    # 2) Heuristic: prefer AC3 and other typical program audio codecs over PCM DVD menu tracks.
    for codec in ("ac3", "eac3", "dts", "mp2", "aac"):
        for idx, c in streams.items():
            if c == codec:
                return idx

    # fallback: choose the lowest stream index
    return sorted(streams.keys())[0]


def analyze_audio_timeline(video_file, only_stream_index: int | None = None):
    """Analyzes the video file to find all audio segments across audio tracks (or a single selected track)."""
    if not os.path.exists(video_file):
        print(f"Error: File '{video_file}' not found.")
        return None, None

    print(f"Analyzing timeline of '{os.path.basename(video_file)}'...")

    info_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file,
    ]
    try:
        info_out = subprocess.check_output(info_cmd, text=True).strip().split("\n")
    except subprocess.CalledProcessError:
        return None, None

    streams = {int(p.split(",")[0]): p.split(",")[1].strip() for p in info_out if p.strip()}
    if not streams:
        print("No audio tracks found.")
        return None, None

    if only_stream_index is not None:
        # keep only selected stream
        if only_stream_index not in streams:
            print(f"Error: Selected audio stream {only_stream_index} not found. Available: {sorted(streams.keys())}")
            return None, None
        streams = {only_stream_index: streams[only_stream_index]}

    packet_cmd = [
        "ffprobe", "-v", "error",
        "-fflags", "+igndts+genpts",
        "-select_streams", "a",
        "-show_entries", "packet=stream_index,pts_time,dts_time",
        "-of", "csv=p=0",
        video_file,
    ]

    stream_blocks: dict[int, list[tuple[float, float]]] = {}
    current_starts, last_times = {}, {}

    # Track discontinuities per stream (DVD concatenations can reset timestamps)
    accumulated_offset: dict[int, float] = {}
    highest_time_in_segment: dict[int, float] = {}
    last_raw_t: dict[int, float] = {}

    process = subprocess.Popen(packet_cmd, stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None

    for line in process.stdout:
        parts = line.strip().split(",")
        try:
            idx = int(parts[0])
            if idx not in streams:
                continue
            t_str = parts[1] if parts[1] not in ("N/A", "") else (parts[2] if len(parts) > 2 else "N/A")
            if t_str in ("N/A", ""):
                continue
            t = float(t_str)
        except (ValueError, IndexError):
            continue

        if idx not in accumulated_offset:
            accumulated_offset[idx] = 0.0
            highest_time_in_segment[idx] = 0.0
            last_raw_t[idx] = None  # type: ignore[assignment]

        # Detect per-stream backward jumps (timestamp resets)
        if last_raw_t[idx] is not None and t < last_raw_t[idx] - 1.0:
            accumulated_offset[idx] += highest_time_in_segment[idx]
            highest_time_in_segment[idx] = 0.0

        last_raw_t[idx] = t
        if t > highest_time_in_segment[idx]:
            highest_time_in_segment[idx] = t

        adjusted_t = t + accumulated_offset[idx]

        if idx not in current_starts:
            current_starts[idx] = adjusted_t
        if idx not in stream_blocks:
            stream_blocks[idx] = []

        if idx in last_times and adjusted_t - last_times[idx] > 1.5:
            stream_blocks[idx].append((current_starts[idx], last_times[idx]))
            current_starts[idx] = adjusted_t

        last_times[idx] = adjusted_t

    process.wait()

    for idx in current_starts:
        if idx not in stream_blocks:
            stream_blocks[idx] = []
        stream_blocks[idx].append((current_starts[idx], last_times[idx]))

    return streams, stream_blocks


def extract_full_tracks(video_file, streams, base_name):
    """STAGE 1: Extracts selected audio tracks into temporary files."""
    temp_files = {}
    codec_map = {
        "ac3": {"ext": "ac3", "codec": "ac3"},
        "pcm_dvd": {"ext": "wav", "codec": "pcm_s16le"},
    }

    for stream_index, codec_name in streams.items():
        codec_info = codec_map.get(codec_name, {"ext": codec_name, "codec": codec_name})
        temp_file_path = f"{base_name}_temp_track_{stream_index}.{codec_info['ext']}"
        temp_files[stream_index] = temp_file_path

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-v", "error",
            "-probesize", "200M",
            "-analyzeduration", "200M",
            "-i", video_file,
            "-map", f"0:{stream_index}",
            "-c:a", codec_info["codec"],
            temp_file_path,
        ]
        subprocess.run(ffmpeg_cmd, capture_output=True)

    return temp_files


def combine_audio_segments(base_name, stream_blocks, streams, temp_files, target_duration: float | None = None):
    """STAGE 2 & 3: Creates synchronized master track from the selected stream."""
    if not streams:
        raise ValueError("No streams selected")

    if len(streams) != 1:
        raise ValueError(f"combine_audio_segments expects exactly 1 stream, got: {sorted(streams.keys())}")

    primary_stream_index = next(iter(streams.keys()))

    master_timeline = []
    packed_offset = 0.0

    for start, end in stream_blocks.get(primary_stream_index, []):
        duration = end - start
        if duration > 0.1:
            master_timeline.append(
                {
                    "global_start": start,
                    "duration": duration,
                    "stream_index": primary_stream_index,
                    "packed_start": packed_offset,
                }
            )
            packed_offset += duration

    master_timeline.sort(key=lambda x: x["global_start"])

    continuous_timeline = []
    current_time = 0.0

    if master_timeline and master_timeline[0]["global_start"] > 0.05:
        continuous_timeline.append({"type": "silence", "duration": master_timeline[0]["global_start"]})
        current_time = master_timeline[0]["global_start"]

    for segment in master_timeline:
        gap = segment["global_start"] - current_time
        if gap > 0.05:
            continuous_timeline.append({"type": "silence", "duration": gap})
            current_time += gap
        elif gap < -0.05:
            overlap = -gap
            if overlap >= segment["duration"]:
                continue
            segment["packed_start"] += overlap
            segment["duration"] -= overlap

        continuous_timeline.append(
            {
                "type": "audio",
                "stream_index": segment["stream_index"],
                "packed_start": segment["packed_start"],
                "duration": segment["duration"],
            }
        )
        current_time += segment["duration"]

    segment_files = []
    for i, item in enumerate(continuous_timeline):
        temp_seg_file = f"{base_name}_temp_seg_{i:03d}.wav"
        segment_files.append(temp_seg_file)
        if item["type"] == "silence":
            cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                str(item["duration"]),
                temp_seg_file,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                temp_files[item["stream_index"]],
                "-ss",
                str(item["packed_start"]),
                "-t",
                str(item["duration"]),
                "-ac",
                "2",
                "-ar",
                "48000",
                temp_seg_file,
            ]
        subprocess.run(cmd)

    concat_list_file = f"{base_name}_concat_list.txt"
    with open(concat_list_file, "w") as f:
        for seg_file in segment_files:
            f.write(f"file '{os.path.abspath(seg_file).replace('\\\\', '/')}'\n")

    final_audio = f"{base_name}_master_audio.m4a"

    # Clamp/pad final audio to match video duration if known.
    # This prevents accidental doubling and makes the output safe for scene cuts.
    if target_duration and target_duration > 0:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list_file,
                "-af",
                f"apad=pad_dur={max(0.0, target_duration)},atrim=duration={target_duration}",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                final_audio,
            ]
        )
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list_file,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                final_audio,
            ]
        )

    return segment_files, concat_list_file, final_audio


def mux_final_video(input_video, final_audio, output_file):
    """STAGE 4: Recombines original video stream with new audio, stripping original audio/metadata."""
    print(f"Muxing final video to: {output_file}")
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        input_video,
        "-i",
        final_audio,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-map_metadata",
        "-1",
        "-map_metadata:s:v",
        "0:s:v:0",
        output_file,
    ]
    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(description="Rebuild video with clean metadata and a single synced audio track.")
    parser.add_argument("-i", "--input", required=True, help="Input video file")
    parser.add_argument("-o", "--output_dir", required=True, help="Output directory")
    parser.add_argument(
        "--audio_stream",
        type=int,
        default=None,
        help="Optional: force which input audio stream index to use (ffprobe stream index, e.g. 4).",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print("Input file not found.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    base_filename = os.path.splitext(os.path.basename(args.input))[0]
    temp_prefix = os.path.join(args.output_dir, f"temp_{base_filename}")
    output_path = os.path.join(args.output_dir, f"{base_filename}_CLEAN.mp4")

    # 0) Get target duration from video container
    target_duration = get_video_duration(args.input)

    streams_all, blocks_all = analyze_audio_timeline(args.input)
    if not streams_all:
        return

    primary_stream = pick_primary_audio_stream(
        streams_all,
        preferred=args.audio_stream,
        video_file=args.input,
        volume_probe_seconds=180,
    )
    print(f"Using primary audio stream index: {primary_stream} (codec: {streams_all.get(primary_stream)})")

    streams, blocks = analyze_audio_timeline(args.input, only_stream_index=primary_stream)
    if not streams:
        return

    temp_tracks = extract_full_tracks(args.input, streams, temp_prefix)
    seg_files, concat_list, master_audio = combine_audio_segments(
        temp_prefix,
        blocks,
        streams,
        temp_tracks,
        target_duration=target_duration,
    )

    try:
        mux_final_video(args.input, master_audio, output_path)
        print("\nProcess Complete. Final file saved.")
    finally:
        print("Cleaning up...")
        for f in [concat_list, master_audio] + seg_files + list(temp_tracks.values()):
            if f and os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    main()
