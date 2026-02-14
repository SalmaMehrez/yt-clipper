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
        'format': 'best',
        'nocheckcertificate': True,
    }
    
    # Client Selection
    if client_type == 'android':
        opts['extractor_args'] = {'youtube': {'player_client': ['android', 'web']}}
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
            
            # 1. Download Video Information using yt-dlp
            current_client = 'web'
            ydl_opts = get_ydl_opts(current_client)
            
            video_url = None
            audio_url = None
            video_height = 0
            video_width = 0
            video_ext = ''
            info = None
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                logger.warning(f"Web extraction failed in clip: {e}")
                logger.info("Falling back to Android in clip...")
                current_client = 'android'
                ydl_opts = get_ydl_opts('android')
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                except Exception as e2:
                    logger.error(f"yt-dlp error: {e2}")
                    raise HTTPException(status_code=400, detail=f"Invalid YouTube URL or video not available. Error: {str(e2)}")

            video_title = info.get('title', 'video')
            formats = info.get('formats', [])

            # Target height based on quality param
            target_height = 0
            try:
                if quality != "best" and quality != "audio":
                    target_height = int(quality)
            except ValueError:
                target_height = 0 

            best_video = None
            best_audio = None

            audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            if audio_formats:
                best_audio = max(audio_formats, key=lambda x: x.get('abr', 0) or 0)
            
            if quality == "audio":
                video_url = None 
            else:
                video_formats = [f for f in formats if f.get('vcodec') != 'none']
                
                if target_height > 0:
                    exact_matches = [f for f in video_formats if f.get('height') == target_height]
                    if exact_matches:
                        candidates = sorted(exact_matches, key=lambda x: x.get('tbr', 0) or 0, reverse=True)
                    else:
                         if video_formats:
                             candidates = sorted(video_formats, key=lambda x: abs((x.get('height', 0) or 0) - target_height))
                         else:
                             candidates = []
                else:
                    candidates = video_formats

                if candidates:
                    best_video = candidates[0]

            if best_video:
                video_url = best_video.get('url')
                video_height = best_video.get('height')
                video_width = best_video.get('width')
                video_ext = best_video.get('ext')
            
            if best_audio:
                audio_url = best_audio.get('url')

            logger.info(f"SELECTED VIDEO: {video_width}x{video_height} (Extension: {video_ext})")

            if not video_url and quality != "audio":
                 raise HTTPException(status_code=400, detail="Could not retrieve video stream.")

            # 2. Download and Cut Video using yt-dlp
            # Using yt-dlp to download the specific segment directly avoids URL expiration issues
            output_filename = f"{clip_id}.mp4"
            output_path = os.path.join(TMP_DIR, output_filename)

            logger.info(f"Downloading clip from {start_time} to {end_time} using yt-dlp...")
            
            try:
                # Configure yt-dlp to download only the specified time range
                download_opts = get_ydl_opts(current_client)
                download_opts.update({
                    'format': f'bestvideo[height<={target_height if target_height > 0 else 2160}]+bestaudio/best' if quality != "audio" else 'bestaudio',
                    'outtmpl': output_path.replace('.mp4', '.%(ext)s'),
                    'download_ranges': lambda info_dict, ydl: [{
                        'start_time': start_sec,
                        'end_time': end_sec,
                    }],
                    'force_keyframes_at_cuts': True,
                    'postprocessors': [{
                        'key': 'FFmpegVideoConvertor',
                        'preferedformat': 'mp4',
                    }],
                    'merge_output_format': 'mp4',
                })
                
                with yt_dlp.YoutubeDL(download_opts) as ydl:
                    ydl.download([url])
                
                # Find the downloaded file (yt-dlp might add format extension)
                possible_files = [
                    output_path,
                    output_path.replace('.mp4', '.webm'),
                    output_path.replace('.mp4', '.mkv'),
                ]
                
                actual_file = None
                for f in possible_files:
                    if os.path.exists(f):
                        actual_file = f
                        break
                
                if not actual_file:
                    raise Exception("Downloaded file not found")
                
                # Rename to .mp4 if needed
                if actual_file != output_path:
                    os.rename(actual_file, output_path)
                    
                logger.info(f"Clip successfully created: {output_path}")
                
            except Exception as e:
                logger.error(f"yt-dlp download failed: {str(e)}")

            # 3. Upload or Return Local Link
            download_url = f"/download/{output_filename}"
            if HAS_GCS and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                try:
                    bucket = GCS_CLIENT.bucket(BUCKET_NAME)
                    blob = bucket.blob(output_filename)
                    blob.upload_from_filename(output_path)
                    download_url = blob.public_url
                    background_tasks.add_task(cleanup_file, output_path)
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
