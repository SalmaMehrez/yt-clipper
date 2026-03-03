import os
import uuid
import logging
import shutil
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import pytubefix
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
    try:
        yt = pytubefix.YouTube(url, client='WEB')
        
        title = yt.title or 'Vidéo sans titre'
        duration = yt.length or 0
        thumbnail = yt.thumbnail_url or ''
        
        resolutions = set()
        for stream in yt.streams.filter(type="video"):
            if stream.resolution:
                # Extraire le nombre (ex: "1080p" -> 1080)
                try:
                    res_val = int(stream.resolution.replace("p", ""))
                    if res_val >= 144:
                        resolutions.add(res_val)
                except ValueError:
                    continue
                    
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
            
            # 1. Download Video Information using pytubefix
            
            video_url = None
            audio_url = None
            video_height = 0
            video_width = 0

            try:
                yt = pytubefix.YouTube(url, client='WEB')
                video_title = yt.title or 'video'

                target_height = 0
                try:
                    if quality != "best" and quality != "audio":
                        target_height = int(quality)
                except ValueError:
                    target_height = 0

                best_video = None
                best_audio = None

                # Find best audio stream
                audio_streams = yt.streams.filter(only_audio=True).order_by('abr').desc()
                if len(audio_streams) > 0:
                    best_audio = audio_streams.first()
                
                if quality == "audio":
                    video_url = None
                else:
                    video_streams = yt.streams.filter(type="video")
                    
                    if target_height > 0:
                        candidates = video_streams.filter(resolution=f"{target_height}p")
                        if len(candidates) > 0:
                            # Prefer video-only streams (often higher quality/dash)
                            dash_candidates = candidates.filter(is_dash=True)
                            if len(dash_candidates) > 0:
                                best_video = dash_candidates.first()
                            else:
                                best_video = candidates.first()
                        else:
                            # Fallback if exact resolution not found: pick highest available
                            best_video = video_streams.order_by('resolution').desc().first()
                    else:
                        best_video = video_streams.order_by('resolution').desc().first()

                if best_video:
                    video_url = best_video.url
                    try:
                        video_height = int(best_video.resolution.replace("p", "")) if best_video.resolution else 0
                    except:
                        video_height = 0
                    video_width = 0 # Not directly available on stream object easily, leave 0
                    video_ext = best_video.subtype
                
                if best_audio:
                    audio_url = best_audio.url

                logger.info(f"SELECTED VIDEO Height: {video_height}px")

            except Exception as e:
                logger.error(f"pytubefix error: {e}")
                raise HTTPException(status_code=400, detail=f"Invalid YouTube URL or video not available. Error: {str(e)}")

            if not video_url and quality != "audio":
                 raise HTTPException(status_code=400, detail="Could not retrieve video stream.")

            # 2. Cut Video using ffmpeg
            output_filename = f"{clip_id}.mp4"
            output_path = os.path.join(TMP_DIR, output_filename)

            logger.info(f"Downloading stream locally first to avoid FFmpeg block...")
            try:
                local_video_path = None
                local_audio_path = None
                
                if best_video:
                    local_video_path = best_video.download(output_path=TMP_DIR, filename=f"{clip_id}_vid.mp4")
                if best_audio:
                    local_audio_path = best_audio.download(output_path=TMP_DIR, filename=f"{clip_id}_aud.mp4")
                    
                logger.info(f"Cutting video from {start_time} to {end_time}...")
                
                # Build ffmpeg inputs from local files
                input_v = ffmpeg.input(local_video_path, ss=start_sec, t=duration) if local_video_path else None
                input_a = ffmpeg.input(local_audio_path, ss=start_sec, t=duration) if local_audio_path else None
                
                if input_a and input_v:
                    stream = ffmpeg.output(input_v, input_a, output_path, 
                                         vcodec='libx264', preset='superfast', crf=23, 
                                         acodec='aac', strict='experimental', movflags='faststart')
                    stream.overwrite_output().run(capture_stdout=True, capture_stderr=True)
                elif input_v:
                    stream = ffmpeg.output(input_v, output_path, 
                                         vcodec='libx264', preset='superfast', crf=23, 
                                         acodec='aac', strict='experimental', movflags='faststart')
                    stream.overwrite_output().run(capture_stdout=True, capture_stderr=True)
                elif input_a:
                    stream = ffmpeg.output(input_a, output_path, 
                                         acodec='aac', strict='experimental')
                    stream.overwrite_output().run(capture_stdout=True, capture_stderr=True)
                    
                # Cleanup downloaded temp files
                if local_video_path: background_tasks.add_task(cleanup_file, local_video_path)
                if local_audio_path: background_tasks.add_task(cleanup_file, local_audio_path)
                
            except ffmpeg.Error as e:
                logger.warning(f"ffmpeg copy failed, falling back to re-encode")
                logger.warning(f"ffmpeg fallback error: {error_msg}")
                raise HTTPException(status_code=500, detail=f"Failed to process video clip: {error_msg}")

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
