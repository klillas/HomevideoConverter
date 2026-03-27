import subprocess
import os
import argparse
import sys

def strip_audio(input_file, output_dir):
    # 1. Verify input file exists
    if not os.path.isfile(input_file):
        print(f"Error: The input file '{input_file}' was not found.")
        sys.exit(1)

    # 2. Create the output directory if it doesn't exist
    if not os.path.exists(output_dir):
        print(f"Creating output folder: '{output_dir}'")
        os.makedirs(output_dir)

    # 3. Construct the output file name safely
    # Extract the filename and extension (e.g., "vacation", ".mp4")
    base_name = os.path.basename(input_file)
    name, ext = os.path.splitext(base_name)
    
    # Append a suffix to the filename
    output_filename = f"{name}_no_audio{ext}"
    output_file = os.path.join(output_dir, output_filename)

    # 4. Strict Safety Check
    # Ensure the exact paths do not match under any circumstance
    if os.path.abspath(input_file) == os.path.abspath(output_file):
        print("Safety triggered: Input and output paths are identical.")
        # Fallback to prevent overwrite just in case the original already had "_no_audio"
        output_filename = f"{name}_copy{ext}"
        output_file = os.path.join(output_dir, output_filename)

    # 5. FFmpeg Command
    command =[
        'ffmpeg',
        '-y',               # Overwrite an existing output file (but NOT the original input)
        '-i', input_file,   # Input file
        '-c:v', 'copy',     # Fast video copy
        '-an',              # Drop audio
        '-sn',              # Drop subtitles
        '-map_metadata', '0:s:v:0', # Keep only video metadata
        output_file
    ]

    try:
        print(f"Input:  {os.path.abspath(input_file)}")
        print(f"Output: {os.path.abspath(output_file)}")
        print("Processing...")
        
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print("Success! Silent video saved.")
        
    except subprocess.CalledProcessError as e:
        print("An error occurred while processing the video:")
        print(e.stderr.decode('utf-8'))

if __name__ == "__main__":
    # Setup command line argument parsing
    parser = argparse.ArgumentParser(description="Strip audio and metadata from a video file.")
    
    parser.add_argument(
        "-i", "--input", 
        required=True, 
        help="Path to the original video file (e.g., /home/pi/Videos/my_video.mp4)"
    )
    
    parser.add_argument(
        "-o", "--output_dir", 
        required=True, 
        help="Path to the folder where the silent video will be saved (e.g., /home/pi/Videos/Silent)"
    )
    
    # Parse arguments
    args = parser.parse_args()
    
    # Run the function
    strip_audio(args.input, args.output_dir)
