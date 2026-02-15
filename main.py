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
import sys
import subprocess
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    import aiofiles
    print("INFO: aiofiles is installed and imported successfully.")
except ImportError:
    print("CRITICAL: aiofiles is NOT installed. Static files will fail.")

app = FastAPI(title="YouTube Clipper")

# CORS for development and production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for simplicity in this demo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for frontend with absolute path
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
if not STATIC_DIR.exists():
    print(f"WARNING: Static directory not found at {STATIC_DIR}")
else:
    print(f"INFO: Mounting static files from {STATIC_DIR}")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/debug-paths")
async def debug_paths():
    """Debug endpoint to inspect server file structure"""
    import os
    try:
        files = os.listdir(str(BASE_DIR))
        static_files = os.listdir(str(STATIC_DIR)) if STATIC_DIR.exists() else "Static dir not found"
        return {
            "cwd": os.getcwd(),
            "base_dir": str(BASE_DIR),
            "root_files": files,
            "static_files": static_files
        }
    except Exception as e:
        return {"error": str(e)}

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
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", 2))
processing_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Start defining Cookie logic
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
COOKIES_ENV = os.environ.get("COOKIES_TXT")

if COOKIES_ENV:
    try:
        with open(COOKIES_PATH, "w") as f:
            f.write(COOKIES_ENV)
        logger.info(f"Created cookies.txt from environment variable at {COOKIES_PATH}")
    except Exception as e:
        logger.error(f"Failed to create cookies.txt: {e}")

def get_ydl_opts(client_type='web', check_cookies=True):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
    }
    
    # Client Selection
    if client_type == 'android':
        # Android client bypasses bot detection
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['android', 'web']
            }
        }
    else:
        pass

    # Cookie Injection
    if check_cookies and os.path.exists(COOKIES_PATH):
        opts['cookiefile'] = COOKIES_PATH
        logger.info("Using cookies.txt for extraction")
        
    return opts

@app.post("/api/info")
async def get_video_info(url: str = Form(...)):
    # 1. Try with WEB client (High Quality)
    try:
        logger.info("Attempting extraction with WEB client (High Quality)...")
        opts = get_ydl_opts('web')
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return process_info(info)
            
    except Exception as e:
        logger.warning(f"WEB client extraction failed: {e}")
        error_msg = str(e)
        
        # 2. If Sign In / Bot error, Fallback to ANDROID (Low Quality)
        if "Sign in to confirm" in error_msg or "403" in error_msg or "Video unavailable" in error_msg or "format is not available" in error_msg:
             logger.info("Falling back to ANDROID client (Low Quality)...")
             try:
                 opts = get_ydl_opts('android')
                 with yt_dlp.YoutubeDL(opts) as ydl:
                     info = ydl.extract_info(url, download=False)
                     return process_info(info)
             except Exception as e2:
                 logger.error(f"ANDROID fallback also failed: {e2}")
                 raise HTTPException(status_code=400, detail=f"Echec extraction (Web & Android): {str(e2)}")
        else:
             raise HTTPException(status_code=400, detail=f"Impossible de récupérer les infos : {str(e)}")

def process_info(info):
    title = info.get('title', 'Vidéo sans titre')
    duration = info.get('duration', 0)
    thumbnail = info.get('thumbnail', '')
    formats = info.get('formats', [])
    
    resolutions = set()
    for f in formats:
        h = f.get('height')
        if not h and f.get('format_note'):
            import re
            match = re.search(r'(\d{3,4})', f['format_note'])
            if match: h = int(match.group(1))

        if h and h >= 144: resolutions.add(h)
    
    sorted_resolutions = sorted(list(resolutions), reverse=True)
    
    qualities = []
    for res in sorted_resolutions:
        label = f"{res}p"
        if res >= 2160: label += " (4K)"
        elif res >= 1440: label += " (2K)"
        elif res == 1080: label += " (HD)"
        qualities.append({"value": str(res), "label": label})
        
    qualities.append({"value": "audio", "label": "Audio uniquement (MP3/M4A)"})

    return JSONResponse({
        "status": "success",
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "qualities": qualities
    })

def download_clip_native(url, start_sec, end_sec, client_type, quality, output_path):
    """
    Download full video with yt-dlp, then clip with FFmpeg.
    This is the most reliable approach that works with all videos.
    """
    ydl_opts = get_ydl_opts(client_type)
    
    # Download full video to temporary file
    temp_video_id = str(uuid.uuid4())
    temp_video_path = os.path.join(TMP_DIR, f"temp_{temp_video_id}.%(ext)s")
    
    # Select format: best pre-merged format available
    if quality == 'audio':
        format_str = 'bestaudio/best'
    else:
        # Accept any pre-merged format (MP4 or WebM)
        format_str = 'best[vcodec!=none][acodec!=none]/best'
    
    ydl_opts.update({
        'outtmpl': temp_video_path,
        'format': format_str,
    })
    
    logger.info(f"Downloading full video with yt-dlp ({client_type}, format: {format_str})...")
    
    # Download full video
    info = None
    downloaded_file = None
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Get actual downloaded filename
        downloaded_file = ydl.prepare_filename(info)
    
    if not downloaded_file or not os.path.exists(downloaded_file):
        raise Exception(f"Failed to download video")
    
    logger.info(f"Video downloaded: {downloaded_file}, now clipping with FFmpeg...")
    
    # Calculate duration
    duration = end_sec - start_sec
    
    try:
        # Clip with FFmpeg
        (
            ffmpeg
            .input(downloaded_file, ss=start_sec, t=duration)
            .output(output_path, vcodec='copy', acodec='copy', avoid_negative_ts='make_zero')
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        
        logger.info(f"Clip created successfully: {output_path}")
        
    except ffmpeg.Error as e:
        error_msg = e.stderr.decode('utf-8') if e.stderr else str(e)
        logger.error(f"FFmpeg clipping failed: {error_msg}")
        raise Exception(f"FFmpeg clipping failed: {error_msg[-500:]}")
    finally:
        # Cleanup temporary full video
        try:
            if downloaded_file and os.path.exists(downloaded_file):
                os.remove(downloaded_file)
                logger.info(f"Cleaned up temporary file: {downloaded_file}")
        except Exception as e:
            logger.warning(f"Failed to cleanup temp file: {e}")
    
    if not os.path.exists(output_path):
        raise Exception(f"Output file not created at {output_path}")
    
    if os.path.getsize(output_path) == 0:
        raise Exception("Clipped file is 0 bytes")
        
    return info

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
            
            output_filename = f"{clip_id}.mp4"
            final_output_path = os.path.join(TMP_DIR, output_filename)
            
            # Ensure final output doesn't exist
            if os.path.exists(final_output_path):
                 try: os.remove(final_output_path)
                 except: pass

            # Retry Logic: Try Web client first, then Android
            success = False
            last_error = ""
            video_info = None
            
            # Attempt 1: Web Client
            try:
                video_info = download_clip_native(url, start_sec, end_sec, 'web', quality, final_output_path)
                success = True
            except Exception as e:
                logger.warning(f"Web client clipping failed: {e}")
                last_error = str(e)
                
                # Attempt 2: Android Client (Fallback)
                if "Sign in" in str(e) or "403" in str(e) or "Video unavailable" in str(e) or "not available" in str(e):
                    logger.info("Retrying with ANDROID client...")
                    try:
                         # Ensure file clean before retry
                         if os.path.exists(final_output_path):
                             os.remove(final_output_path)
                             
                         video_info = download_clip_native(url, start_sec, end_sec, 'android', quality, final_output_path)
                         success = True
                    except Exception as e2:
                        logger.error(f"Android client clipping also failed: {e2}")
                        last_error = str(e2)

            if not success:
                 raise Exception(f"Failed to create clip after retries. Last error: {last_error}")

            logger.info(f"Clip created successfully: {final_output_path}")

            if not os.path.exists(final_output_path):
                logger.error(f"Output file not found at {final_output_path}")
                raise HTTPException(status_code=500, detail="Clip file was not created successfully")
            
            file_size = os.path.getsize(final_output_path)
            logger.info(f"Clip created successfully: {final_output_path} ({file_size} bytes)")

            # Extract metadata for response
            video_title = video_info.get("title", "Clip YouTube")
            video_width = video_info.get("width", 0)
            video_height = video_info.get("height", 0)

            # 3. Upload or Return Local Link
            download_url = f"/download/{output_filename}"
            if HAS_GCS and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                try:
                    bucket = GCS_CLIENT.bucket(BUCKET_NAME)
                    blob = bucket.blob(output_filename)
                    blob.upload_from_filename(final_output_path)
                    download_url = blob.public_url
                    background_tasks.add_task(cleanup_file, final_output_path)
                except Exception as e:
                    logger.error(f"GCS Upload Error: {e}")

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
        return FileResponse(file_path, filename=filename, media_type="video/mp4")
    else:
        raise HTTPException(status_code=404, detail="File not found or expired.")

@app.get("/")
async def read_root():
    return FileResponse("static/index.html")
