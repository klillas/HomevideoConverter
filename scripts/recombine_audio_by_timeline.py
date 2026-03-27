import subprocess
import sys
import os

def format_time(seconds):
    """Converts seconds to a nice HH:MM:SS.sss format."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{mins:02d}:{secs:06.3f}"

def analyze_audio_timeline(video_file):
    """
    Analyzes the video file to find all audio segments across all tracks.
    """
    if not os.path.exists(video_file):
        print(f"Error: File '{video_file}' not found.")
        return None, None

    print(f"Analyzing timeline of '{os.path.basename(video_file)}'...")
    
    info_cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file]
    try:
        info_out = subprocess.check_output(info_cmd, text=True).strip().split('\n')
    except subprocess.CalledProcessError:
        print("Error getting stream info.")
        return None, None

    streams = {int(p.split(',')[0]): p.split(',')[1].strip() for p in info_out if p.strip()}
    if not streams:
        print("No audio tracks found.")
        return None, None
        
    print(f"Found {len(streams)} audio tracks. Scanning packets to build master timeline...")
    
    packet_cmd = ["ffprobe", "-v", "error", "-fflags", "+igndts+genpts", "-select_streams", "a",
                  "-show_entries", "packet=stream_index,pts_time,dts_time", "-of", "csv=p=0", video_file]

    stream_blocks = {}
    current_starts, last_times = {}, {}
    accumulated_offset, highest_time_in_segment, last_raw_t = 0.0, 0.0, None
    
    process = subprocess.Popen(packet_cmd, stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        parts = line.strip().split(',')
        try:
            idx = int(parts[0])
            t_str = parts[1] if parts[1] != 'N/A' else parts[2]
            t = float(t_str)
        except (ValueError, IndexError):
            continue

        if last_raw_t is not None and t < last_raw_t - 2.0:
            accumulated_offset += highest_time_in_segment
            highest_time_in_segment = 0.0
        
        last_raw_t = t
        highest_time_in_segment = max(t, highest_time_in_segment)
        adjusted_t = t + accumulated_offset

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
        stream_blocks[idx].append((current_starts[idx], last_times[idx]))
    
    print("Analysis complete.\n")
    return streams, stream_blocks

def extract_full_tracks(video_file, streams, base_name):
    """
    STAGE 1: Extracts each full audio stream into its own temporary file.
    Re-encodes to fix the broken DVD headers so the files become standard.
    """
    print("="*60)
    print("STAGE 1: Extracting full audio tracks to temporary files...")
    print("="*60)
    
    temp_files = {}
    
    codec_map = {
        'ac3': {'ext': 'ac3', 'codec': 'ac3'},
        'pcm_dvd': {'ext': 'wav', 'codec': 'pcm_s16le'},
    }

    for stream_index, codec_name in streams.items():
        codec_info = codec_map.get(codec_name, {'ext': codec_name, 'codec': codec_name})
        temp_file_path = f"{base_name}_temp_track_{stream_index}.{codec_info['ext']}"
        temp_files[stream_index] = temp_file_path

        print(f"Extracting Stream {stream_index} ({codec_name}) to '{os.path.basename(temp_file_path)}'...")

        ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_file, "-map", f"0:{stream_index}",
                      "-c:a", codec_info['codec'], temp_file_path]
        
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR: Failed to extract track {stream_index}.")
            print(result.stderr)
            return None
        else:
            print("  -> Success.")
            
    return temp_files

def combine_audio_segments(base_name, stream_blocks, streams, temp_files):
    """
    STAGE 2 & 3: Slices tracks, bridges gaps with silence, and combines to a single encoded file.
    """
    print("\n" + "="*60)
    print("STAGE 2: Generating normalized segments and bridging gaps...")
    print("="*60)
    
    master_timeline = []
    track_packed_offsets = {idx: 0.0 for idx in streams.keys()}
    
    for stream_index, blocks in stream_blocks.items():
        if not blocks: continue
        
        for start, end in blocks:
            duration = end - start
            if duration > 0.1:
                master_timeline.append({
                    "global_start": start, 
                    "duration": duration,
                    "stream_index": stream_index,
                    "packed_start": track_packed_offsets[stream_index]
                })
                track_packed_offsets[stream_index] += duration
    
    # Sort chronologically by the global timeline
    master_timeline.sort(key=lambda x: x['global_start'])

    # Build a continuous timeline that includes generated silences to maintain sync with the video
    continuous_timeline = []
    current_time = 0.0
    
    if master_timeline and master_timeline[0]['global_start'] > 0.05:
        # Add silence at the beginning if the first audio segment doesn't start at 0
        continuous_timeline.append({"type": "silence", "duration": master_timeline[0]['global_start']})
        current_time = master_timeline[0]['global_start']

    for segment in master_timeline:
        gap = segment['global_start'] - current_time
        if gap > 0.05:  # Add explicit silence if gap is larger than 50ms
            continuous_timeline.append({"type": "silence", "duration": gap})
        
        continuous_timeline.append({
            "type": "audio",
            "stream_index": segment['stream_index'],
            "packed_start": segment['packed_start'],
            "duration": segment['duration']
        })
        current_time = segment['global_start'] + segment['duration']

    segment_files = []
    
    # Create standardized temporary WAV files (48kHz, Stereo) so they combine without glitches
    for i, item in enumerate(continuous_timeline):
        temp_seg_file = f"{base_name}_temp_seg_{i:03d}.wav"
        segment_files.append(temp_seg_file)
        
        if item['type'] == 'silence':
            print(f"[{i+1}/{len(continuous_timeline)}] Inserting {item['duration']:.2f}s of silence (Syncing gap)")
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=stereo",
                "-t", str(item['duration']),
                temp_seg_file
            ]
            subprocess.run(cmd)
            
        else:
            stream_index = item['stream_index']
            source_temp_file = temp_files[stream_index]
            packed_start_time = item['packed_start']
            
            print(f"[{i+1}/{len(continuous_timeline)}] Slicing Track {stream_index} (Duration: {item['duration']:.2f}s)")
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-ss", str(packed_start_time),
                "-i", source_temp_file,
                "-t", str(item['duration']),
                "-ac", "2", "-ar", "48000", # Standardize format
                temp_seg_file
            ]
            subprocess.run(cmd)

    print("\n" + "="*60)
    print("STAGE 3: Encoding all slices into a single master track...")
    print("="*60)
    
    concat_list_file = f"{base_name}_concat_list.txt"
    with open(concat_list_file, "w") as f:
        for seg_file in segment_files:
            # Use forward slashes for FFmpeg's concat demuxer, even on Windows
            safe_path = os.path.abspath(seg_file).replace('\\', '/')
            f.write(f"file '{safe_path}'\n")

    # Encode all segments into a final AAC file
    final_output = f"{base_name}_master_audio.m4a"
    
    concat_cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_file,
        "-c:a", "aac",
        "-b:a", "192k",
        final_output
    ]
    
    result = subprocess.run(concat_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("  ERROR: FFmpeg failed during the final encode.")
        print(result.stderr)
    else:
        print(f"  -> SUCCESS! Final combined audio saved as: '{os.path.basename(final_output)}'")
        
    return segment_files, concat_list_file

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 recombine_audio_by_timeline.py <video_file> [output_directory]")
        sys.exit(1)
        
    video_file = sys.argv[1]
    
    # Handle the optional output directory argument
    if len(sys.argv) >= 3:
        out_dir = sys.argv[2]
        if not os.path.isdir(out_dir):
            print(f"Error: Output directory '{out_dir}' does not exist.")
            sys.exit(1)
        file_base_name = os.path.splitext(os.path.basename(video_file))[0]
        base_name = os.path.join(out_dir, file_base_name)
    else:
        base_name, _ = os.path.splitext(video_file)
    
    streams, stream_blocks = analyze_audio_timeline(video_file)
    if not (streams and stream_blocks):
        sys.exit(1)
        
    temp_files = extract_full_tracks(video_file, streams, base_name)
    
    # Initialize variables to allow safe cleanup in the finally block
    segment_files = []
    concat_list_file = None

    try:
        if temp_files:
            # Main processing happens here
            segment_files, concat_list_file = combine_audio_segments(base_name, stream_blocks, streams, temp_files)
    finally:
        # This block will run regardless of success or failure, ensuring cleanup
        print("\nCleaning up temporary files...")
        
        # 1. Clean up full track extractions (e.g., _temp_track_1.ac3)
        if temp_files:
            for path in temp_files.values():
                if os.path.exists(path):
                    os.remove(path)
                    print(f"  Removed '{os.path.basename(path)}'")

        # 2. Clean up WAV segments (e.g., _temp_seg_001.wav)
        if segment_files:
            for path in segment_files:
                if os.path.exists(path):
                    os.remove(path)
                    print(f"  Removed '{os.path.basename(path)}'")

        # 3. Clean up the concat list file (e.g., _concat_list.txt)
        if concat_list_file and os.path.exists(concat_list_file):
            os.remove(concat_list_file)
            print(f"  Removed '{os.path.basename(concat_list_file)}'")
        
        print("\nAll tasks completed.")
