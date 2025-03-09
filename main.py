print("Starting Flask app...")

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file, if any

import os
import re
import logging
import threading
import time
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Set test mode based on environment variable.
TEST_MODE = os.environ.get('TEST_MODE', 'false').lower() == 'true'
logging.info("TEST_MODE: %s", TEST_MODE)

# In test mode, we don't use the YouTube API; in production, the user's API key is used.
# Global variables for progress and summary.
progress = 0
transcripts_list = []
no_transcript_list = []
download_folder_name = "Results"  # default folder name

channel_name = ""
transcripts_count = 0
no_transcripts_count = 0
process_time = 0.0

# In-memory caches for optimization.
video_cache = {}           # key: (channel_id, api_key) -> videos list
video_details_cache = {}   # key: (video_id, api_key) -> details
transcript_cache = {}      # key: (video_id, api_key) -> transcript

@app.route('/')
def index():
    return render_template('index.html')

def parse_iso8601_duration(iso_duration):
    """
    Convert an ISO 8601 duration string (e.g. 'PT23M44S') to a human-readable 'HH:MM:SS' format.
    """
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(iso_duration)
    if not match:
        return iso_duration  # Fallback: return raw string if parsing fails.
    
    hours, minutes, seconds = match.groups()
    hours = int(hours) if hours else 0
    minutes = int(minutes) if minutes else 0
    seconds = int(seconds) if seconds else 0
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def get_videos(client, channel_id):
    """Fetch all videos from a channel using pagination (or return dummy data in test mode)."""
    # Use caching keyed on (channel_id, api_key)
    cache_key = (channel_id, client._developerKey) if client else (channel_id, "dummy")
    if cache_key in video_cache:
        logging.info("Using cached videos for channel: %s", channel_id)
        return video_cache[cache_key]
    
    if TEST_MODE:
        dummy_videos = [
            {
                'video_id': 'dummy1',
                'title': 'Dummy Video 1',
                'published_at': '2022-01-01T00:00:00Z',
                'link': 'https://www.youtube.com/watch?v=dummy1'
            },
            {
                'video_id': 'dummy2',
                'title': 'Dummy Video 2',
                'published_at': '2022-01-02T00:00:00Z',
                'link': 'https://www.youtube.com/watch?v=dummy2'
            },
            {
                'video_id': 'dummy3',
                'title': 'Dummy Video 3',
                'published_at': '2022-01-03T00:00:00Z',
                'link': 'https://www.youtube.com/watch?v=dummy3'
            }
        ]
        logging.info("Returning dummy videos for channel: %s", channel_id)
        video_cache[cache_key] = dummy_videos
        return dummy_videos
    else:
        videos = []
        next_page_token = None
        while True:
            logging.info("Fetching videos with next_page_token: %s", next_page_token)
            request_obj = client.search().list(
                part='snippet',
                channelId=channel_id,
                maxResults=50,
                order='date',
                pageToken=next_page_token
            )
            response = request_obj.execute()
            for item in response.get('items', []):
                video_id = item['id'].get('videoId')
                if video_id:
                    videos.append({
                        'video_id': video_id,
                        'title': item['snippet']['title'],
                        'published_at': item['snippet']['publishedAt'],
                        'link': f"https://www.youtube.com/watch?v={video_id}"
                    })
            next_page_token = response.get('nextPageToken')
            if not next_page_token:
                break
        logging.info("Total videos fetched: %d", len(videos))
        video_cache[cache_key] = videos
        return videos

def get_video_details(client, video_ids):
    """Fetch additional metadata (view count, duration) for given video IDs in chunks (or return dummy details in test mode)."""
    details = {}
    # Use caching keyed by (video_id, api_key)
    for vid in video_ids:
        cache_key = (vid, client._developerKey) if client else (vid, "dummy")
        if cache_key in video_details_cache:
            details[vid] = video_details_cache[cache_key]
    uncached_ids = [vid for vid in video_ids if (vid, client._developerKey if client else "dummy") not in video_details_cache]
    
    if TEST_MODE:
        for vid in uncached_ids:
            detail = {'view_count': '1000', 'duration': '00:05:00'}
            cache_key = (vid, "dummy")
            video_details_cache[cache_key] = detail
            details[vid] = detail
        logging.info("Returning dummy video details.")
        return details
    else:
        for i in range(0, len(uncached_ids), 50):
            chunk = uncached_ids[i:i+50]
            if not chunk:
                continue
            logging.info("Fetching details for chunk: %s", chunk)
            request_obj = client.videos().list(
                part='statistics,contentDetails',
                id=','.join(chunk)
            )
            response = request_obj.execute()
            for item in response.get('items', []):
                raw_duration = item['contentDetails'].get('duration', 'N/A')
                readable_duration = parse_iso8601_duration(raw_duration)
                detail = {
                    'view_count': item['statistics'].get('viewCount'),
                    'duration': readable_duration
                }
                cache_key = (item['id'], client._developerKey)
                video_details_cache[cache_key] = detail
                details[item['id']] = detail
        return details

def get_transcript(video_id, client):
    """Retrieve transcript for a video; return dummy transcript in test mode if applicable."""
    cache_key = (video_id, client._developerKey) if client else (video_id, "dummy")
    if TEST_MODE:
        dummy_transcripts = {
            'dummy1': "This is a dummy transcript for video 1.",
            'dummy2': "This is a dummy transcript for video 2.",
            'dummy3': "This is a dummy transcript for video 3."
        }
        logging.info("Returning dummy transcript for video: %s", video_id)
        transcript_cache[cache_key] = dummy_transcripts.get(video_id, "No transcript available.")
        return transcript_cache[cache_key]
    else:
        if cache_key in transcript_cache:
            logging.info("Using cached transcript for video: %s", video_id)
            return transcript_cache[cache_key]
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'en-US', 'en-GB'])
            if transcript and 'is_generated' in transcript[0]:
                logging.info("Transcript for video %s is auto-generated: %s", video_id, transcript[0]['is_generated'])
            full_transcript = ' '.join([segment['text'] for segment in transcript])
            transcript_cache[cache_key] = full_transcript
            return full_transcript
        except Exception as e:
            logging.error("Transcript not available for video %s: %s", video_id, e)
            return None

def create_files(transcripts_list, no_transcript_list):
    """Generate result files in a dedicated 'results' folder."""
    results_folder = os.path.join(os.getcwd(), 'results')
    if not os.path.exists(results_folder):
        os.makedirs(results_folder)
    
    transcripts_path = os.path.join(results_folder, 'transcripts.txt')
    with open(transcripts_path, 'w', encoding='utf-8') as f:
        for video in transcripts_list:
            f.write(f"Title: {video['title']}\n")
            f.write(f"Published At: {video['published_at']}\n")
            f.write(f"Video ID: {video['video_id']}\n")
            f.write(f"View Count: {video.get('view_count', 'N/A')}\n")
            f.write(f"Duration: {video.get('duration', 'N/A')}\n")
            f.write(f"Video Link: {video.get('link', 'N/A')}\n")
            f.write("Transcript:\n")
            f.write(video['transcript'] + "\n")
            f.write("=" * 40 + "\n")
    
    df_with = pd.DataFrame(transcripts_list)
    with pd.ExcelWriter(os.path.join(results_folder, 'videos_with_transcripts.xlsx')) as writer:
        df_with.to_excel(writer, index=False)
    
    df_without = pd.DataFrame(no_transcript_list)
    with pd.ExcelWriter(os.path.join(results_folder, 'videos_without_transcripts.xlsx')) as writer:
        df_without.to_excel(writer, index=False)

def create_zip_archive(folder_name):
    """
    Create an in-memory ZIP archive of all result files from the 'results' folder.
    folder_name: The desired folder name inside the ZIP archive.
    """
    results_folder = os.path.join(os.getcwd(), 'results')
    zip_buffer = io.BytesIO()
    files_to_include = ["transcripts.txt", "videos_with_transcripts.xlsx", "videos_without_transcripts.xlsx"]
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file in files_to_include:
            file_path = os.path.join(results_folder, file)
            if os.path.exists(file_path):
                zip_file.write(file_path, arcname=os.path.join(folder_name, file))
            else:
                logging.warning("File not found: %s", file_path)
    zip_buffer.seek(0)
    return zip_buffer

def process_videos(client, channel_id):
    """Process videos: fetch list, merge metadata, get transcripts concurrently, update progress, and record summary info."""
    global progress, transcripts_list, no_transcript_list, channel_name, transcripts_count, no_transcripts_count, process_time
    logging.info("Processing channel ID: %s", channel_id)
    start_time = time.time()
    
    if TEST_MODE:
        channel_name = "Dummy Channel"
    else:
        try:
            channel_response = client.channels().list(part='snippet', id=channel_id).execute()
            if channel_response.get('items'):
                channel_name = channel_response['items'][0]['snippet']['title']
            else:
                channel_name = "Unknown Channel"
        except Exception as e:
            logging.error("Error fetching channel details for %s: %s", channel_id, e)
            channel_name = "Unknown Channel"
    
    videos = get_videos(client, channel_id)
    if not videos:
        logging.info("No videos found for channel %s", channel_id)
        progress = 100
        return

    video_ids = [video['video_id'] for video in videos]
    details = get_video_details(client, video_ids)
    for video in videos:
        if video['video_id'] in details:
            video.update(details[video['video_id']])
    
    transcripts_list = []
    no_transcript_list = []
    total_videos = len(videos)
    logging.info("Total videos to process: %d", total_videos)

    def process_video(video):
        transcript = get_transcript(video['video_id'], client)
        return video, transcript

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_video, video): video for video in videos}
        completed = 0
        for future in as_completed(futures):
            video, transcript = future.result()
            if transcript:
                video['transcript'] = transcript
                transcripts_list.append(video)
            else:
                no_transcript_list.append(video)
            completed += 1
            progress = int((completed / total_videos) * 100)
            logging.info("Processed video %d/%d, progress: %d%%", completed, total_videos, progress)
    
    transcripts_count = len(transcripts_list)
    no_transcripts_count = len(no_transcript_list)
    process_time = time.time() - start_time
    logging.info("Processing complete: %d transcripts, %d without transcripts, time taken: %.2f seconds", transcripts_count, no_transcripts_count, process_time)
    
    create_files(transcripts_list, no_transcript_list)

@app.route('/process', methods=['POST'])
def process_channel():
    global progress, download_folder_name
    channel_id = request.form['channelId']
    download_folder_name = request.form.get('folderName', 'Results')
    user_api_key = request.form['apiKey']
    logging.info("Channel ID received: %s", channel_id)
    logging.info("Download folder name set to: %s", download_folder_name)
    logging.info("User API key received.")
    progress = 0  # Reset progress before starting.
    
    if TEST_MODE:
        client = None  # Dummy mode; client not used.
    else:
        try:
            client = build('youtube', 'v3', developerKey=user_api_key)
        except Exception as e:
            logging.error("Error building YouTube client: %s", e)
            return jsonify({"error": "Invalid API key or error initializing YouTube client."}), 400

    thread = threading.Thread(target=process_videos, args=(client, channel_id))
    thread.start()
    return jsonify({"message": "Channel processing started!"})

@app.route('/progress')
def get_progress():
    result = {"progress": progress}
    if progress == 100:
        result["channel_name"] = channel_name
        result["transcripts_count"] = transcripts_count
        result["no_transcripts_count"] = no_transcripts_count
        result["process_time"] = process_time
    return jsonify(result)

@app.route('/download_all')
def download_all():
    zip_buffer = create_zip_archive(download_folder_name)
    zip_filename = f"{download_folder_name}.zip"
    return send_file(zip_buffer, as_attachment=True, download_name=zip_filename, mimetype="application/zip")

if __name__ == '__main__':
    # Read PORT from the environment, default to 5000 if not set.
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
