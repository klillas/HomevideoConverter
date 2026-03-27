import subprocess
import sys
import os

def format_time(seconds):
    """Converts seconds to a nice HH:MM:SS.ss format."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{mins:02d}:{secs:05.2f}"

def analyze_audio_timing(video_file):
    if not os.path.exists(video_file):
        print(f"Error: File '{video_file}' not found.")
        return

    print(f"Analyzing timeline of '{os.path.basename(video_file)}'...")
    
    # Step 1: Get the stream IDs so we know which track is which
    info_cmd =[
        "ffprobe", "-v", "error",
        "-probesize", "5000M",
        "-analyzeduration", "100000M",
        "-select_streams", "a",
        "-show_entries", "stream=index,codec_name",
        "-of", "csv=p=0",
        video_file
    ]
    try:
        info_out = subprocess.check_output(info_cmd, text=True).strip().split('\n')
    except subprocess.CalledProcessError:
        print("Error getting stream info.")
        return

    streams = {}
    for line in info_out:
        if not line.strip(): continue
        parts = line.split(',')
        if len(parts) >= 2:
            streams[int(parts[0])] = parts[1].strip()

    print(f"Found {len(streams)} audio tracks. Scanning packets to build master timeline...")
    print("(This streams the raw data very efficiently. Please wait...)")

    # Step 2: Read every single packet in the file in order
    packet_cmd =[
        "ffprobe", "-v", "error",
        "-fflags", "+igndts+genpts",
        "-select_streams", "a",
        "-show_entries", "packet=stream_index,pts_time,dts_time",
        "-of", "csv=p=0",
        video_file
    ]

    stream_blocks = {}
    current_starts = {}
    last_times = {}

    # These track the internal DVD clock resetting to zero
    accumulated_offset = 0.0
    highest_time_in_segment = 0.0
    last_raw_t = None

    process = subprocess.Popen(packet_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    count = 0
    # Read output line-by-line as it generates so we don't crash the Raspberry Pi RAM
    for line in process.stdout:
        line = line.strip()
        if not line: continue
        
        parts = line.split(',')
        if len(parts) < 2: continue
        
        try:
            idx = int(parts[0])
            # Check PTS time, fallback to DTS if PTS is missing
            t_str = parts[1]
            if t_str == 'N/A' or not t_str:
                if len(parts) > 2 and parts[2] != 'N/A' and parts[2]:
                    t_str = parts[2]
                else:
                    continue
            t = float(t_str)
        except ValueError:
            continue

        count += 1
        if count % 50000 == 0:
            print(f"  ...scanned {count} audio packets...")

        # Did the DVD clock suddenly jump backward? (File concatenation boundary)
        if last_raw_t is not None:
            if t < last_raw_t - 2.0:
                accumulated_offset += highest_time_in_segment
                highest_time_in_segment = 0.0
        
        last_raw_t = t
        if t > highest_time_in_segment:
            highest_time_in_segment = t

        # Calculate the "Real" time exactly like VLC does
        adjusted_t = t + accumulated_offset

        if idx not in current_starts:
            current_starts[idx] = adjusted_t
            last_times[idx] = adjusted_t

        # If there is a silence/break gap of more than 1.5 seconds, start a new block
        if adjusted_t - last_times[idx] > 1.5:
            if idx not in stream_blocks:
                stream_blocks[idx] = []
            stream_blocks[idx].append((current_starts[idx], last_times[idx]))
            current_starts[idx] = adjusted_t

        last_times[idx] = adjusted_t

    process.wait()

    # Close off the final blocks
    for idx in current_starts:
        if idx not in stream_blocks:
            stream_blocks[idx] =[]
        stream_blocks[idx].append((current_starts[idx], last_times[idx]))

    # Print the Results!
    print("\n" + "="*60)
    print("               AUDIO TIMELINE ANALYSIS")
    print("="*60)
    for idx, codec in streams.items():
        # FFmpeg streams start at 0, our earlier script printed 1/2. We adjust here for readability.
        print(f"\n-> Track {idx} (Codec: {codec}):")
        blocks = stream_blocks.get(idx,[])
        if not blocks:
            print("   No active audio data found.")
        else:
            total_duration = 0
            for i, (start, end) in enumerate(blocks):
                duration = end - start
                total_duration += duration
                # Filter out tiny 0.1s glitch blips that might occur on DVD resets
                if duration > 0.1:
                    print(f"   Segment {i+1:02d}: {format_time(start)}  -->  {format_time(end)}  (Duration: {duration:05.2f}s)")
            print(f"[Total Play Time for Track: {format_time(total_duration)}]")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 analyze_timeline.py <video_file.mpg>")
    else:
        analyze_audio_timing(sys.argv[1])
