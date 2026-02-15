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
    
    # Client Selection - Multi-client strategy to bypass bot detection
    if client_type == 'android':
        opts['extractor_args'] = {'youtube': {'player_client': ['android', 'web']}}
    elif client_type == 'ios':
        opts['extractor_args'] = {'youtube': {'player_client': ['ios', 'web']}}
    else:
        # Default Web client
        opts['extractor_args'] = {'youtube': {'player_client': ['web', 'web_creator']}}

    # Cookie Injection
    if check_cookies and os.path.exists(COOKIES_PATH):
        opts['cookiefile'] = COOKIES_PATH
        logger.info(f"Using cookies.txt for {client_type} extraction")
        
    return opts

@app.post("/api/info")
async def get_video_info(url: str = Form(...)):
    clients = ['web', 'ios', 'android']
    
    for client in clients:
        # Try with cookies first, then without cookies
        for use_cookies in [True, False]:
            try:
                cookie_status = "with cookies" if use_cookies else "WITHOUT cookies"
                logger.info(f"Attempting {client} extraction {cookie_status}...")
                
                opts = get_ydl_opts(client, check_cookies=use_cookies)
                # Disable format checking during info extraction to avoid "Requested format is not available"
                opts['check_formats'] = False 
                
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return process_info(info)
            except Exception as e:
                logger.warning(f"{client} extraction ({cookie_status}) failed: {e}")
                continue # Try next combination
                
    logger.error("All extraction attempts failed.")
    raise HTTPException(status_code=400, detail="Echec extraction : Format non disponible ou blocage YouTube (Web/iOS/Android)")

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

def download_clip_native(url, start_sec, end_sec, client_type, quality, output_path, check_cookies=True):
    """
    Universal Strategy: Handles both single files and split streams (DASH).
    Uses native yt-dlp clipping (download_ranges) for efficiency.
    """
    base_path = os.path.splitext(output_path)[0]
    ydl_opts = get_ydl_opts(client_type, check_cookies=check_cookies)
    
    # Adapt format based on quality
    if quality == 'audio':
        format_spec = 'bestaudio/best'
    else:
        # Use user's "Universal" format spec
        format_spec = 'best[ext=mp4]/bestvideo+bestaudio/best'

    ydl_opts.update({
        'format': format_spec,
        'merge_output_format': 'mp4',
        'download_ranges': yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
        'force_keyframes_at_cuts': True,
        'outtmpl': f'{base_path}.%(ext)s',
        'check_formats': False, # Avoid "Requested format is not available" errors
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    })

    logger.info(f"Downloading clip with Universal Strategy ({client_type}, format: {format_spec})...")
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # download=True will trigger download_ranges
            info = ydl.extract_info(url, download=True)
            
        # Verify and normalize output file
        possible_files = [f"{base_path}.mp4", f"{base_path}.mkv", f"{base_path}.webm", f"{base_path}.m4a"]
        final_file = next((f for f in possible_files if os.path.exists(f)), None)
        
        if final_file:
            if final_file != output_path:
                 # Ensure destination is clear before moving
                 if os.path.exists(output_path):
                     os.remove(output_path)
                 os.rename(final_file, output_path)
            
            logger.info(f"Clip successfully created at {output_path}")
            return info # Return info for metadata extraction in create_clip
        else:
            raise Exception("Resulting file not found after yt-dlp execution")

    except Exception as e:
        logger.error(f"Universal Strategy failed: {str(e)}")
        raise e

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

            # Retry Logic: Try Web -> IOS -> Android, each with and without cookies
            success = False
            last_error = ""
            video_info = None
            
            clients_to_try = ['web', 'ios', 'android']
            for client in clients_to_try:
                if success: break
                for use_cookies in [True, False]:
                    try:
                        cookie_status = "with cookies" if use_cookies else "WITHOUT cookies"
                        logger.info(f"Attempting {client} clipping {cookie_status}...")
                        
                        # Ensure file clean before retry
                        if os.path.exists(final_output_path):
                            os.remove(final_output_path)
                            
                        video_info = download_clip_native(url, start_sec, end_sec, client, quality, final_output_path, check_cookies=use_cookies)
                        success = True
                        break # Success with this client/cookie combo
                    except Exception as e:
                        logger.warning(f"{client} clipping ({cookie_status}) failed: {e}")
                        last_error = str(e)

            if not success:
                 raise Exception(f"Failed to create clip after all retries. Last error: {last_error}")

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
