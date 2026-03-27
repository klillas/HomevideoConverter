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
    
    info_cmd =["ffprobe", "-v", "error", "-select_streams", "a",
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
    
    packet_cmd =["ffprobe", "-v", "error", "-fflags", "+igndts+genpts", "-select_streams", "a",
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

def extract_full_tracks(video_file, streams):
    """
    STAGE 1: Extracts each full audio stream into its own temporary file.
    Re-encodes to fix the broken DVD headers so the files become standard.
    """
    print("="*60)
    print("STAGE 1: Extracting full audio tracks to temporary files...")
    print("="*60)
    
    base_name, _ = os.path.splitext(video_file)
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

        ffmpeg_cmd =["ffmpeg", "-y", "-i", video_file, "-map", f"0:{stream_index}",
                      "-c:a", codec_info['codec'], temp_file_path]
        
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR: Failed to extract track {stream_index}.")
            return None
        else:
            print("  -> Success.")
            
    return temp_files

def segment_temp_files(base_name, stream_blocks, streams, temp_files):
    """
    STAGE 2: Segments the temporary files based on their PACKED internal timeline.
    """
    print("\n" + "="*60)
    print("STAGE 2: Splitting temporary tracks into final segments...")
    print("="*60)
    
    master_timeline =[]
    
    # Track the "packed" internal start time for each temporary file
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
                    "codec": streams.get(stream_index, "unknown"),
                    "packed_start": track_packed_offsets[stream_index] # <--- THE MAGIC FIX
                })
                # Accumulate the duration so the next segment of this track starts exactly here
                track_packed_offsets[stream_index] += duration
    
    # Sort chronologically by the global timeline so the output files are numbered 01 to 10 in order
    master_timeline.sort(key=lambda x: x['global_start'])

    # For Stage 2, the temp files are clean, so we can use "copy" for lossless cutting
    codec_map = {
        'ac3': 'ac3',
        'pcm_dvd': 'wav',
    }

    for i, segment in enumerate(master_timeline):
        segment_num = i + 1
        stream_index = segment['stream_index']
        input_codec = segment['codec']
        
        source_temp_file = temp_files[stream_index]
        extension = codec_map.get(input_codec, input_codec)
        output_file = f"{base_name}_audio_{segment_num:02d}.{extension}"
        
        packed_start_time = segment['packed_start']
        
        print(f"[{segment_num}/{len(master_timeline)}] Creating '{os.path.basename(output_file)}'")
        print(f"  -> Source: temp track {stream_index}, Packed Start: {format_time(packed_start_time)}, Duration: {segment['duration']:.2f}s")

        # Standard Accurate Seek (-ss BEFORE -i) using the packed time + copy codec
        ffmpeg_cmd =[
            "ffmpeg", "-y",
            "-ss", str(packed_start_time),
            "-i", source_temp_file,
            "-t", str(segment['duration']),
            "-c", "copy", 
            output_file
        ]
        
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR: ffmpeg failed for segment {segment_num}.")
        else:
            print("  -> Success.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 split_audio_by_timeline.py <video_file.mpg>")
        sys.exit(1)
        
    video_file = sys.argv[1]
    base_name, _ = os.path.splitext(video_file)
    
    streams, stream_blocks = analyze_audio_timeline(video_file)
    if not (streams and stream_blocks):
        sys.exit(1)
        
    temp_files = extract_full_tracks(video_file, streams)
    
    if temp_files:
        try:
            segment_temp_files(base_name, stream_blocks, streams, temp_files)
        finally:
            print("\nCleaning up temporary files...")
            for path in temp_files.values():
                if os.path.exists(path):
                    os.remove(path)
                    print(f"  Removed '{os.path.basename(path)}'")
            print("\nAll tasks completed.")
