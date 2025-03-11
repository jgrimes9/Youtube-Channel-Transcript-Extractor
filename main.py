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
total_videos = 0  # Add this line
transcripts_count = 0
no_transcripts_count = 0
process_time = 0.0
stop_process = False  # Add this new global variable
start_time = None  # Add this line

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
    cache_key = (channel_id, client._developerKey) if client else (channel_id, "dummy")
    if cache_key in video_cache:
        logging.info("Using cached videos for channel: %s", channel_id)
        return video_cache[cache_key]
    
    if TEST_MODE:
        dummy_videos = []
        for i in range(1, 101):  # Generate 100 dummy videos
            dummy_videos.append({
                'video_id': f'dummy{i}',
                'title': f'Dummy Video {i}',
                'published_at': f'2022-{((i-1)//30 + 1):02d}-{((i-1)%30 + 1):02d}T00:00:00Z',
                'link': f'https://www.youtube.com/watch?v=dummy{i}'
            })
        logging.info("Returning 100 dummy videos for channel: %s", channel_id)
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
        video_num = int(video_id.replace('dummy', ''))
        dummy_transcript = f"This is a dummy transcript for video {video_num}. "
        dummy_transcript += "It contains some sample text that varies by video number. "
        dummy_transcript += f"The video discusses topic {video_num % 5} in detail."
        logging.info("Returning dummy transcript for video: %s", video_id)
        transcript_cache[cache_key] = dummy_transcript
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
    global progress, transcripts_list, no_transcript_list, channel_name, transcripts_count
    global no_transcripts_count, process_time, stop_process, start_time
    
    transcripts_list = []
    no_transcript_list = []
    start_time = time.time()
    process_time = 0.0  # Initialize to 0
    
    logging.info("Processing channel ID: %s", channel_id)
    
    if TEST_MODE:
        channel_name = "Dummy Channel"
        total_steps = 100  # Increased to 100 videos
        for step in range(total_steps):
            if stop_process:
                progress = -1
                process_time = round(time.time() - start_time, 2)
                return
            time.sleep(0.1)  # Reduced sleep time to make test mode faster but still visible
            if stop_process:  # Check again after sleep
                progress = -1
                process_time = round(time.time() - start_time, 2)
                return
            progress = int(((step + 1) / total_steps) * 100)
            transcripts_count = step + 1
            process_time = round(time.time() - start_time, 2)
            logging.info("Test mode progress: %d%%, Time: %.2fs", progress, process_time)
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
                if stop_process:
                    executor.shutdown(wait=False, cancel_futures=True)  # Cancel pending futures
                    progress = -1
                    process_time = round(time.time() - start_time, 2)  # Round to 2 decimal places
                    return
                video, transcript = future.result()
                if transcript:
                    video['transcript'] = transcript
                    transcripts_list.append(video)
                    transcripts_count = len(transcripts_list)
                else:
                    no_transcript_list.append(video)
                    no_transcripts_count = len(no_transcript_list)
                completed += 1
                progress = round((completed / total_videos) * 100)  # Round to whole number
                process_time = round(time.time() - start_time, 2)  # Update time with 2 decimal precision
                logging.info("Processed video %d/%d, progress: %d%%, time: %.2fs", 
                           completed, total_videos, progress, process_time)

        process_time = round(time.time() - start_time, 2)  # Ensure final time is rounded consistently
        logging.info("Processing complete: %d transcripts, %d without transcripts, time taken: %.2f seconds", 
                    transcripts_count, no_transcripts_count, process_time)
        
        create_files(transcripts_list, no_transcript_list)

@app.route('/process', methods=['POST'])
def process_channel():
    global progress, download_folder_name, channel_name, total_videos, transcripts_count, no_transcripts_count, stop_process
    # Reset all global variables
    progress = 0
    transcripts_count = 0
    no_transcripts_count = 0
    stop_process = False
    channel_name = ""
    total_videos = 0
    
    channel_id = request.form['channelId']
    download_folder_name = request.form.get('folderName', 'Results')
    user_api_key = request.form['apiKey']
    logging.info("Channel ID received: %s", channel_id)
    logging.info("Download folder name set to: %s", download_folder_name)
    logging.info("User API key received.")
    progress = 0  # Reset progress before starting.
    errors = []
    
    if TEST_MODE:
        channel_name = "Dummy Channel"
        total_videos = 100  # Updated to match new test video count
        client = None  # Dummy mode; client not used.
    else:
        # Try to build the client and test the API key.
        try:
            client = build('youtube', 'v3', developerKey=user_api_key)
            test_response = client.search().list(
                part='snippet',
                q='test',
                maxResults=1
            ).execute()
        except Exception as e:
            logging.error("Invalid API key: %s", e)
            errors.append("Invalid API key.")
        
        # Try to validate the channel ID.
        try:
            # Only attempt channel lookup if client is built
            channel_response = client.channels().list(
                part='snippet', 
                id=channel_id
            ).execute() if client else {}
            if not channel_response.get('items'):
                errors.append("Invalid channel ID.")
            else:
                channel_name = channel_response['items'][0]['snippet']['title']
                # Get initial video count
                playlist_response = client.channels().list(
                    part='statistics',
                    id=channel_id
                ).execute()
                if playlist_response.get('items'):
                    total_videos = int(playlist_response['items'][0]['statistics']['videoCount'])
        except Exception as e:
            logging.error("Error validating channel ID: %s", e)
            errors.append("Invalid channel ID.")
    
    if errors:
        return jsonify({"error": " ".join(errors)}), 400
    
    thread = threading.Thread(target=process_videos, args=(client, channel_id))
    thread.start()
    return jsonify({
        "message": "Channel processing started!",
        "channel_name": channel_name,
        "total_videos": total_videos
    })

@app.route('/progress')
def get_progress():
    global stop_process, process_time, start_time
    
    # Update process time if process is running
    if start_time is not None and progress < 100 and progress != -1:
        process_time = round(time.time() - start_time, 2)

    result = {
        "progress": progress,
        "transcripts_count": transcripts_count,
        "no_transcripts_count": no_transcripts_count,
        "process_time": process_time,
        "channel_name": channel_name,
        "total_videos": total_videos
    }
    
    if progress == 100 or progress == -1:
        stop_process = False
        result["channel_name"] = channel_name
        
    return jsonify(result)

@app.route('/stop_process', methods=['POST'])
def stop_process_route():
    global stop_process
    stop_process = True
    return jsonify({"message": "Process stop requested"})

@app.route('/download_all')
def download_all():
    zip_buffer = create_zip_archive(download_folder_name)
    zip_filename = f"{download_folder_name}.zip"
    return send_file(zip_buffer, as_attachment=True, download_name=zip_filename, mimetype="application/zip")

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the exception and return JSON response with error message
    import traceback
    error_message = ''.join(traceback.format_exception_only(type(e), e)).strip()
    logging.error("Unhandled Exception: %s", error_message)
    return jsonify({"error": error_message}), 500

if __name__ == '__main__':
    # Read PORT from the environment, default to 5000 if not set.
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
