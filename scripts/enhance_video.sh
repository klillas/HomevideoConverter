#!/bin/bash

# --- Check for input file ---
if [ -z "$1" ]; then
    echo "Usage: $0 <path_to_input_video> [path_to_output_folder]"
    echo "Example: $0 my_video.mp4 /home/admin/Desktop/Finished_Videos"
    exit 1
fi

# --- Input and Configuration ---
INPUT_VIDEO="$1"
OUTPUT_FOLDER="${2:-enhanced_videos}"
OUTPUT_FILENAME_SUFFIX="_enhanced"

# --- Auto-Chapter Settings ---
# Drastically increased threshold for noisy analog video.
SCENE_THRESHOLD="0.9999"
# Relaxed this so we can see the true chapter length. 15s is a good minimum.
MIN_CHAPTER_DURATION="15"

# --- Video Encoding (WhatsApp Optimized) ---
CRF="24"
MAX_BITRATE="2.5M"
BUF_SIZE="5M"

# --- Video Enhancement (FFmpeg) ---
DEINTERLACE="yes"
DENOISE_STRENGTH="0.1"
SHARPEN_STRENGTH="0.3"

BRIGHTNESS="0.05"
CONTRAST="1.1"
SATURATION="1.2"
FILM_GRAIN="0.05"

# --- Audio Enhancement ---
HIGH_PASS_FREQ="80"
LOW_PASS_FREQ="10k" 
NORMALIZE_AUDIO="yes"
TARGET_LOUDNESS="-23"

# --- Script Logic Setup ---
mkdir -p "$OUTPUT_FOLDER"
BASENAME=$(basename -- "$INPUT_VIDEO")
FILENAME="${BASENAME%.*}"
TEMP_DIR="$OUTPUT_FOLDER/temp_work"
mkdir -p "$TEMP_DIR"

cleanup() {
    echo "Cleaning up temporary files..."
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

echo "Starting enhancement process for: $INPUT_VIDEO"
echo "Output will be saved to: $OUTPUT_FOLDER"

# ==========================================
# PHASE 1: SCENE DETECTION (IMPROVED)
# ==========================================
echo "--- Phase 1: Detecting Chapters (Using denoising for accuracy) ---"

TOTAL_DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$INPUT_VIDEO")
if [ -z "$TOTAL_DURATION" ]; then
    echo "Error: Could not determine video duration."
    exit 1
fi

CUTS=(0)

# ** MAJOR CHANGE HERE **
# We now apply a denoiser (hqdn3d) BEFORE the scene detector (scdet).
# This cleans the video, making scdet far more accurate on noisy analog sources.
SCENE_TIMES=$(ffmpeg -i "$INPUT_VIDEO" -vf "hqdn3d=luma_spatial=4,scdet=threshold=$SCENE_THRESHOLD" -an -f null - 2>&1 | awk -F'lavfi.scd.time: ' '{if ($2) print $2}' | awk '{print $1}')

LAST_CUT=0
for time in $SCENE_TIMES; do
    IS_VALID=$(awk "BEGIN {print ($time - $LAST_CUT > $MIN_CHAPTER_DURATION) ? 1 : 0}")
    if [ "$IS_VALID" -eq 1 ]; then
        CUTS+=("$time")
        LAST_CUT=$time
    fi
done
CUTS+=("$TOTAL_DURATION")

NUM_CHAPTERS=$((${#CUTS[@]} - 1))
echo "Found $NUM_CHAPTERS distinct chapters/scenes."

if [ $NUM_CHAPTERS -gt 100 ]; then
    echo "WARNING: Still found a very high number of chapters. The source may be very noisy."
    echo "Consider increasing SCENE_THRESHOLD further (e.g., to 0.9) if results are poor."
fi


# ==========================================
# PHASE 2: PROCESSING CHAPTERS
# ==========================================
echo "--- Phase 2: Enhancing and Encoding Chapters ---"

FFMPEG_VIDEO_FILTERS=""
if [ "$DEINTERLACE" = "yes" ]; then
    FFMPEG_VIDEO_FILTERS+="bwdif=mode=send_field:parity=auto:deint=all,"
fi
FFMPEG_VIDEO_FILTERS+="hqdn3d=luma_spatial=$DENOISE_STRENGTH,"
FFMPEG_VIDEO_FILTERS+="unsharp=luma_msize_x=5:luma_msize_y=5:luma_amount=$SHARPEN_STRENGTH,"
FFMPEG_VIDEO_FILTERS+="eq=brightness=$BRIGHTNESS:contrast=$CONTRAST:saturation=$SATURATION,"
FFMPEG_VIDEO_FILTERS+="noise=alls=$FILM_GRAIN:allf=t+u"
FFMPEG_VIDEO_FILTERS=${FFMPEG_VIDEO_FILTERS%,}

for (( i=0; i<$NUM_CHAPTERS; i++ )); do
    
    CHAPTER_NUM=$(printf "%02d" $((i + 1)))
    START_TIME=${CUTS[$i]}
    END_TIME=${CUTS[$((i + 1))]}
    DURATION=$(awk "BEGIN {print $END_TIME - $START_TIME}")
    
    OUTPUT_VIDEO="$OUTPUT_FOLDER/${FILENAME}_${CHAPTER_NUM}${OUTPUT_FILENAME_SUFFIX}.mp4"
    
    echo "========================================================="
    echo "Processing Chapter $CHAPTER_NUM of $NUM_CHAPTERS"
    echo "Time: $START_TIME to $END_TIME (Duration: ${DURATION}s)"
    echo "========================================================="
    
    TEMP_AUDIO="$TEMP_DIR/temp_audio_${CHAPTER_NUM}.wav"
    ENHANCED_AUDIO="$TEMP_DIR/enhanced_audio_${CHAPTER_NUM}.wav"
    NORMALIZED_AUDIO="$TEMP_DIR/normalized_audio_${CHAPTER_NUM}.wav"
    
    ffmpeg -v error -stats -ss "$START_TIME" -t "$DURATION" -i "$INPUT_VIDEO" -vn -acodec pcm_s16le -ar 44100 -ac 2 "$TEMP_AUDIO" -y
    sox "$TEMP_AUDIO" "$ENHANCED_AUDIO" sinc ${HIGH_PASS_FREQ}-${LOW_PASS_FREQ}
    if [ "$NORMALIZE_AUDIO" = "yes" ]; then
        ffmpeg -v error -stats -i "$ENHANCED_AUDIO" -af "loudnorm=I=$TARGET_LOUDNESS:TP=-1.5:LRA=11" -ar 44100 "$NORMALIZED_AUDIO" -y
        mv "$NORMALIZED_AUDIO" "$ENHANCED_AUDIO"
    fi

    ffmpeg -ss "$START_TIME" -t "$DURATION" -i "$INPUT_VIDEO" -i "$ENHANCED_AUDIO" \
    -c:v libx264 -preset veryfast -crf $CRF -maxrate $MAX_BITRATE -bufsize $BUF_SIZE \
    -vf "$FFMPEG_VIDEO_FILTERS" -c:a aac -b:a 256k \
    -map 0:v:0 -map 1:a:0 -movflags +faststart -y "$OUTPUT_VIDEO"

    rm -f "$TEMP_AUDIO" "$ENHANCED_AUDIO" "$NORMALIZED_AUDIO"
    
    echo "Completed Chapter $CHAPTER_NUM -> $OUTPUT_VIDEO"
done

echo "========================================================="
echo "ALL FINISHED! Processed $NUM_CHAPTERS chapters."
echo "Videos are located in: $OUTPUT_FOLDER"
