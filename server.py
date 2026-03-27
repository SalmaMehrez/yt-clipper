import os
import sys
import uuid
import json
import time
import threading
import webbrowser
import subprocess
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# Helper to find ffmpeg in PyInstaller bundle
def get_ffmpeg_path():
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, 'ffmpeg.exe')
    return 'ffmpeg' # Assumes ffmpeg is in PATH if not bundled

FFMPEG_PATH = get_ffmpeg_path()
TEMP_DIR = os.path.join(os.path.expanduser("~"), "yt_clipper_temp")
os.makedirs(TEMP_DIR, exist_ok=True)

def cleanup_files(file_paths):
    """Wait 5 seconds and then try to delete the files."""
    def target():
        time.sleep(5)
        for path in file_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    print(f"Cleaned up: {path}")
            except Exception as e:
                print(f"Error cleaning up {path}: {e}")
    threading.Thread(target=target).start()

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok"})

@app.route('/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        ydl_opts = {'skip_download': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                "title": info.get('title'),
                "duration": info.get('duration'),
                "thumbnail": info.get('thumbnail'),
                "uploader": info.get('uploader')
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/clip', methods=['POST'])
def create_clip():
    data = request.json
    url = data.get('url')
    start = data.get('start')
    end = data.get('end')

    if not all([url, start, end]):
        return jsonify({"error": "Missing parameters"}), 400

    clip_id = str(uuid.uuid4())[:8]
    raw_video = os.path.join(TEMP_DIR, f"raw_{clip_id}.mp4")
    output_video = os.path.join(TEMP_DIR, f"clip_{clip_id}.mp4")

    try:
        # Step 1: Download full video (yt-dlp)
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': raw_video,
            'quiet': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Step 2: Clip with ffmpeg
        # Construct ffmpeg command
        # start/end can be seconds or HH:MM:SS
        ffmpeg_cmd = [
            FFMPEG_PATH, '-y',
            '-ss', str(start),
            '-to', str(end),
            '-i', raw_video,
            '-c:v', 'libx264',
            '-c:a', 'aac',
            '-movflags', 'faststart',
            output_video
        ]
        
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True)

        # Step 3: Send file and schedule cleanup
        cleanup_files([raw_video, output_video])
        return send_file(output_video, as_attachment=True, download_name=f"clip_{clip_id}.mp4")

    except Exception as e:
        cleanup_files([raw_video])
        return jsonify({"error": str(e)}), 500

def open_browser():
    time.sleep(1.5)
    webbrowser.open("https://youtube-clipper.onrender.com")

if __name__ == '__main__':
    print("YT Clipper Agent — Running on localhost:5000 / Ne fermez pas cette fenêtre")
    threading.Thread(target=open_browser).start()
    app.run(port=5000, debug=False)
