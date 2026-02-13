import os
import uuid
import logging
import shutil
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import ffmpeg
import asyncio

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube Clipper")

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

# Temporary directory for processing
TMP_DIR = "/tmp/yt_clipper" if os.name == 'posix' else "./tmp/yt_clipper"
os.makedirs(TMP_DIR, exist_ok=True)

# Google Cloud Storage (Optional - requires credentials)
try:
    from google.cloud import storage
    GCS_CLIENT = storage.Client()
    HAS_GCS = True
except Exception as e:
    logger.warning(f"Google Cloud Storage not configured: {e}")
    HAS_GCS = False

BUCKET_NAME = os.environ.get("BUCKET_NAME", "your-gcs-bucket-name")

def cleanup_file(path: str):
    """Deletes a file after processing."""
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Deleted temporary file: {path}")
    except Exception as e:
        logger.error(f"Error deleting file {path}: {e}")

def get_seconds(time_str: str) -> int:
    """Converts HH:MM:SS or MM:SS to seconds."""
    parts = list(map(int, time_str.split(':')))
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 1:
        return parts[0]
    return 0

# Concurrency Limit
# 4K re-encoding is heavy. We limit concurrent processing to avoid server crash.
# Default to 2 concurrent "heavy" tasks. Others will wait in queue.
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", 2))
processing_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

@app.post("/api/info")
async def get_video_info(url: str = Form(...)):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            title = info.get('title', 'Vidéo sans titre')
            duration = info.get('duration', 0)
            thumbnail = info.get('thumbnail', '')
            formats = info.get('formats', [])
            
            # Extract unique resolutions
            resolutions = set()
            for f in formats:
                h = f.get('height')
                if h and h >= 144: # Filter out very low quality or audio-only in video list
                    resolutions.add(h)
            
            # Sort descending
            sorted_resolutions = sorted(list(resolutions), reverse=True)
            
            # Format for frontend
            qualities = []
            for res in sorted_resolutions:
                label = f"{res}p"
                if res >= 2160: label += " (4K)"
                elif res >= 1440: label += " (2K)"
                elif res == 1080: label += " (HD)"
                
                qualities.append({"value": str(res), "label": label})
                
            # Always add Audio option
            qualities.append({"value": "audio", "label": "Audio uniquement (MP3/M4A)"})

            return JSONResponse({
                "status": "success",
                "title": title,
                "duration": duration,
                "thumbnail": thumbnail,
                "qualities": qualities
            })
            
    except Exception as e:
        logger.error(f"Error fetching info: {e}")
        raise HTTPException(status_code=400, detail=f"Impossible de récupérer les infos : {str(e)}")

@app.post("/api/clip")
async def create_clip(
    url: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    quality: str = Form("best"),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    clip_id = str(uuid.uuid4())
    logger.info(f"Processing clip request {clip_id} for URL: {url} with quality: {quality}")

    # Acquire semaphore to limit concurrent heavy processing
    if processing_semaphore.locked():
        logger.info(f"Request {clip_id} is waiting in queue...")
    
    async with processing_semaphore:
        logger.info(f"Request {clip_id} acquired processing slot.")
        try:
            start_sec = get_seconds(start_time)
            end_sec = get_seconds(end_time)
            duration = end_sec - start_sec

            if duration <= 0:
                raise HTTPException(status_code=400, detail="End time must be greater than start time.")
            # Removed duration limit as requested
            
            # 1. Download Video Information using yt-dlp
            # We fetch ALL formats and manually select the best one to ensure quality
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
            }
            
            video_url = None
            audio_url = None
            video_height = 0
            video_width = 0

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    video_title = info.get('title', 'video')
                    formats = info.get('formats', [])

                    # Target height based on quality param
                    target_height = 0
                    try:
                        if quality != "best" and quality != "audio":
                            target_height = int(quality)
                    except ValueError:
                        target_height = 0 # Fallback to best if parsing fails

                    best_video = None
                    best_audio = None

                    # Find best audio first
                    audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                    if audio_formats:
                        best_audio = max(audio_formats, key=lambda x: x.get('abr', 0) or 0)
                    
                    if quality == "audio":
                        video_url = None # Audio only
                    else:
                        # Get ALL video formats (webm, mp4, etc.)
                        video_formats = [f for f in formats if f.get('vcodec') != 'none']
                        
                        if target_height > 0:
                            # 1. Try to find EXACT match (height)
                            exact_matches = [f for f in video_formats if f.get('height') == target_height]
                            
                            if exact_matches:
                                # Prefer higher bitrate options within this height
                                candidates = sorted(exact_matches, key=lambda x: x.get('tbr', 0) or 0, reverse=True)
                            else:
                                 # 2. Fallback: Find closest options (minimize difference)
                                 if video_formats:
                                     # Sort by distance to target height
                                     candidates = sorted(video_formats, key=lambda x: abs((x.get('height', 0) or 0) - target_height))
                                 else:
                                     candidates = []
                        else:
                            candidates = video_formats

                        if candidates:
                            # Pick the best candidate (first one after sorting)
                            best_video = candidates[0]

                    if best_video:
                        video_url = best_video.get('url')
                        video_height = best_video.get('height')
                        video_width = best_video.get('width')
                        video_ext = best_video.get('ext')
                    
                    if best_audio:
                        audio_url = best_audio.get('url')

                    logger.info(f"SELECTED VIDEO: {video_width}x{video_height} (Extension: {video_ext})")

                except Exception as e:
                    logger.error(f"yt-dlp error: {e}")
                    raise HTTPException(status_code=400, detail=f"Invalid YouTube URL or video not available. Error: {str(e)}")

            if not video_url and quality != "audio":
                 raise HTTPException(status_code=400, detail="Could not retrieve video stream.")

            # 2. Cut Video using ffmpeg
            output_filename = f"{clip_id}.mp4"
            output_path = os.path.join(TMP_DIR, output_filename)

            logger.info(f"Cutting video from {start_time} to {end_time}...")
            
            try:
                # Build ffmpeg inputs
                input_v = ffmpeg.input(video_url, ss=start_sec, t=duration)
                
                # Determine if we can copy or need to re-encode
                # If original is NOT mp4 (e.g. webm VP9 for 4K), we MUST re-encode for MP4 output
                # OR if we want to be safe, we re-encode.
                
                # For 4K/2K (usually VP9/AV1), standard QuickTime/Windows might not play it if we just copy to MP4 container.
                # We will Force Re-encode for high quality to ensure standard H.264 MP4.
                # It is slower, but guaranteed to work.
                
                should_reencode = True
                
                if audio_url:
                    input_a = ffmpeg.input(audio_url, ss=start_sec, t=duration)
                    
                    if should_reencode:
                        # Re-encode video to H.264 (fast preset), copy audio if possible or AAC
                        stream = ffmpeg.output(input_v, input_a, output_path, 
                                             vcodec='libx264', preset='superfast', crf=23, 
                                             acodec='aac', strict='experimental', movflags='faststart')
                    else:
                        stream = ffmpeg.output(input_v, input_a, output_path, c='copy', movflags='faststart')
                else:
                    if should_reencode:
                        stream = ffmpeg.output(input_v, output_path, 
                                             vcodec='libx264', preset='superfast', crf=23, 
                                             acodec='aac', strict='experimental', movflags='faststart')
                    else:
                        stream = ffmpeg.output(input_v, output_path, c='copy', movflags='faststart')
                
                # Run
                stream.overwrite_output().run(capture_stdout=True, capture_stderr=True)

            except ffmpeg.Error as e:
                logger.warning(f"ffmpeg copy failed, falling back to re-encode: {e.stderr.decode('utf-8') if e.stderr else str(e)}")
                try:
                    # Fallback to re-encoding if copy fails (e.g. incompatible codecs for mp4)
                    # Use ultrafast preset to keep it bearable for long videos
                    if audio_url:
                        input_a = ffmpeg.input(audio_url, ss=start_sec, t=duration)
                        stream = ffmpeg.output(input_v, input_a, output_path, vcodec='libx264', acodec='aac', preset='ultrafast', strict='experimental', movflags='faststart')
                    else:
                        stream = ffmpeg.output(input_v, output_path, vcodec='libx264', acodec='aac', preset='ultrafast', strict='experimental', movflags='faststart')
                    
                    stream.overwrite_output().run(capture_stdout=True, capture_stderr=True)
                except ffmpeg.Error as e2:
                    logger.error(f"ffmpeg fallback error: {e2.stderr.decode('utf-8') if e2.stderr else str(e2)}")
                    raise HTTPException(status_code=500, detail="Failed to process video clip.")

            # 3. Upload to GCS or Return Local Link
            download_url = ""
            
            if HAS_GCS and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                logger.info(f"Uploading to GCS bucket: {BUCKET_NAME}")
                try:
                    bucket = GCS_CLIENT.bucket(BUCKET_NAME)
                    blob = bucket.blob(output_filename)
                    blob.upload_from_filename(output_path)
                    # Make public (if bucket policy allows, or use signed URL)
                    # blob.make_public() # Be careful with this permission
                    download_url = blob.public_url
                    # Schedule cleanup of local file
                    background_tasks.add_task(cleanup_file, output_path)
                except Exception as e:
                    logger.error(f"GCS Upload Error: {e}")
                    # Fallback to local
                    download_url = f"/download/{output_filename}"
            else:
                download_url = f"/download/{output_filename}"
                # For local demo, we keep the file for a bit, or you could schedule cleanup after a delay
                # Here we don't auto-clean immediately so user can download. 
                # In production, use a cron job or GCS lifecycle.

            return JSONResponse({
                "status": "success",
                "message": "Clip created successfully.",
                "download_url": download_url,
                "title": video_title,
                "duration": duration,
                "resolution": f"{video_width}x{video_height}" if video_height else "Inconnue"
            })

        except HTTPException as e:
            raise e
        except Exception as e:
            logger.exception("Unexpected error")
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{filename}")
async def download_file(filename: str, background_tasks: BackgroundTasks):
    file_path = os.path.join(TMP_DIR, filename)
    if os.path.exists(file_path):
        # Optional: cleanup after download
        # background_tasks.add_task(cleanup_file, file_path) 
        return FileResponse(file_path, filename=filename, media_type="video/mp4")
    else:
        raise HTTPException(status_code=404, detail="File not found or expired.")

@app.get("/")
async def read_root():
    return FileResponse("static/index.html")
