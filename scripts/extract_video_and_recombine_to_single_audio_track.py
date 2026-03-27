import subprocess
import os
import argparse
import sys

def format_time(seconds):
    """Converts seconds to a nice HH:MM:SS.sss format."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{mins:02d}:{secs:06.3f}"

def analyze_audio_timeline(video_file):
    """Analyzes the video file to find all audio segments across all tracks."""
    if not os.path.exists(video_file):
        print(f"Error: File '{video_file}' not found.")
        return None, None

    print(f"Analyzing timeline of '{os.path.basename(video_file)}'...")
    
    info_cmd =["ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file]
    try:
        info_out = subprocess.check_output(info_cmd, text=True).strip().split('\n')
    except subprocess.CalledProcessError:
        return None, None

    streams = {int(p.split(',')[0]): p.split(',')[1].strip() for p in info_out if p.strip()}
    if not streams:
        print("No audio tracks found.")
        return None, None
        
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
            stream_blocks[idx] =[]

        if idx in last_times and adjusted_t - last_times[idx] > 1.5:
            stream_blocks[idx].append((current_starts[idx], last_times[idx]))
            current_starts[idx] = adjusted_t
        
        last_times[idx] = adjusted_t
    process.wait()

    for idx in current_starts:
        stream_blocks[idx].append((current_starts[idx], last_times[idx]))
    
    return streams, stream_blocks

def extract_full_tracks(video_file, streams, base_name):
    """STAGE 1: Extracts audio tracks into temporary files."""
    temp_files = {}
    codec_map = {'ac3': {'ext': 'ac3', 'codec': 'ac3'}, 'pcm_dvd': {'ext': 'wav', 'codec': 'pcm_s16le'}}

    for stream_index, codec_name in streams.items():
        codec_info = codec_map.get(codec_name, {'ext': codec_name, 'codec': codec_name})
        temp_file_path = f"{base_name}_temp_track_{stream_index}.{codec_info['ext']}"
        temp_files[stream_index] = temp_file_path

        ffmpeg_cmd =["ffmpeg", "-y", "-i", video_file, "-map", f"0:{stream_index}",
                      "-c:a", codec_info['codec'], temp_file_path]
        subprocess.run(ffmpeg_cmd, capture_output=True)
            
    return temp_files

def combine_audio_segments(base_name, stream_blocks, streams, temp_files):
    """STAGE 2 & 3: Creates synchronized master track."""
    master_timeline =[]
    track_packed_offsets = {idx: 0.0 for idx in streams.keys()}
    
    for stream_index, blocks in stream_blocks.items():
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
    
    master_timeline.sort(key=lambda x: x['global_start'])
    continuous_timeline =[]
    current_time = 0.0
    
    if master_timeline and master_timeline[0]['global_start'] > 0.05:
        continuous_timeline.append({"type": "silence", "duration": master_timeline[0]['global_start']})
        current_time = master_timeline[0]['global_start']

    for segment in master_timeline:
        gap = segment['global_start'] - current_time
        if gap > 0.05: 
            continuous_timeline.append({"type": "silence", "duration": gap})
            current_time += gap
        elif gap < -0.05: 
            # Fix: If segments overlap, trim to prevent the track from desyncing/stretching
            overlap = -gap
            if overlap >= segment['duration']: continue
            segment['packed_start'] += overlap
            segment['duration'] -= overlap

        continuous_timeline.append({
            "type": "audio", 
            "stream_index": segment['stream_index'], 
            "packed_start": segment['packed_start'], 
            "duration": segment['duration']
        })
        current_time += segment['duration']

    segment_files =[]
    for i, item in enumerate(continuous_timeline):
        temp_seg_file = f"{base_name}_temp_seg_{i:03d}.wav"
        segment_files.append(temp_seg_file)
        if item['type'] == 'silence':
            cmd =["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", str(item['duration']), temp_seg_file]
        else:
            # THE FIX: Note that `-ss` is now placed AFTER `-i`. 
            # This forces FFmpeg to decode accurately from the start to find the exact frame, ignoring broken raw AC3 timestamps.
            cmd =["ffmpeg", "-y", "-v", "error", "-i", temp_files[item['stream_index']], "-ss", str(item['packed_start']), "-t", str(item['duration']), "-ac", "2", "-ar", "48000", temp_seg_file]
        subprocess.run(cmd)

    concat_list_file = f"{base_name}_concat_list.txt"
    with open(concat_list_file, "w") as f:
        for seg_file in segment_files:
            f.write(f"file '{os.path.abspath(seg_file).replace('\\', '/')}'\n")

    final_audio = f"{base_name}_master_audio.m4a"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", concat_list_file, "-c:a", "aac", "-b:a", "192k", final_audio])
    return segment_files, concat_list_file, final_audio

def mux_final_video(input_video, final_audio, output_file):
    """STAGE 4: Recombines original video stream with new audio, stripping original audio/metadata."""
    print(f"Muxing final video to: {output_file}")
    command =[
        'ffmpeg', '-y', '-v', 'error',
        '-i', input_video,      # Original source
        '-i', final_audio,      # New master audio
        '-map', '0:v:0',        # Copy ONLY the video from source
        '-map', '1:a:0',        # Copy the single audio stream from the new audio file
        '-c:v', 'copy',         # Direct video stream copy (Fast!)
        '-c:a', 'copy',         # Copy encoded audio
        '-map_metadata', '-1',  # Strip all global metadata
        '-map_metadata:s:v', '0:s:v:0', # Map ONLY metadata from the video stream
        output_file
    ]
    subprocess.run(command, check=True)

def main():
    parser = argparse.ArgumentParser(description="Rebuild video with clean metadata and a single synced audio track.")
    parser.add_argument("-i", "--input", required=True, help="Input video file")
    parser.add_argument("-o", "--output_dir", required=True, help="Output directory")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print("Input file not found.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    base_filename = os.path.splitext(os.path.basename(args.input))[0]
    temp_prefix = os.path.join(args.output_dir, f"temp_{base_filename}")
    output_path = os.path.join(args.output_dir, f"{base_filename}_CLEAN.mp4")

    streams, blocks = analyze_audio_timeline(args.input)
    if not streams: return
    
    temp_tracks = extract_full_tracks(args.input, streams, temp_prefix)
    seg_files, concat_list, master_audio = combine_audio_segments(temp_prefix, blocks, streams, temp_tracks)

    try:
        mux_final_video(args.input, master_audio, output_path)
        print("\nProcess Complete. Final file saved.")
    finally:
        print("Cleaning up...")
        for f in[concat_list, master_audio] + seg_files + list(temp_tracks.values()):
            if f and os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    main()
