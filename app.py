"""
app.py

Earnings Call Audio Downloader
A standalone local web app that lets a non-technical user download
earnings call audio as an mp3 by pasting a webcast URL.

Supported platforms:
- YouTube (yt-dlp)
- ON24 (yt-dlp)
- Direct mp4/mp3 CDN links (Q4 CDN, AppLovin, etc.)
- HLS streams via ffmpeg
- Q4 Inc event pages (Playwright headless — automatic)
- edge.media-server.com (Playwright visible — user clicks play)

To run:
    pip install -r requirements.txt
    python app.py

Then open: http://127.0.0.1:8001
"""

from flask import Flask, render_template, Response, request, stream_with_context
import os, sys, json, re, subprocess, tempfile, threading, queue, uuid, time, webbrowser
from collections import defaultdict
import requests as req_lib

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Path setup
APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(APP_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)

# Active download jobs
_jobs = {}

CAPTURE_SECONDS = 45
MEDIA_PATTERNS = [r'\.m3u8', r'\.mp4', r'\.mp3', r'\.m4a']


def open_in_chrome(url):
    """Open URL in Chrome on Mac, fallback to default browser."""
    try:
        subprocess.Popen(['open', '-a', 'Google Chrome', url])
    except Exception:
        webbrowser.open(url)


# ─── CORS ─────────────────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/ext-ping')
def ext_ping():
    return {'ok': True}

@app.route('/api/media-found', methods=['POST', 'OPTIONS'])
def media_found():
    if request.method == 'OPTIONS':
        return Response('', 200)
    data = request.get_json()
    job_id = data.get('jobId')
    url = data.get('url', '')
    if job_id and job_id in _jobs:
        job = _jobs[job_id]
        if not job.get('media_url'):
            job['media_url'] = url
            job['q'].put({'type': 'log', 'message': 'Extension captured media URL', 'level': 'success'})
    return Response('ok', 200)


# ─── Platform detection ───────────────────────────────────────────────────────

def detect_platform(url):
    u = url.lower()
    if 'youtube.com' in u or 'youtu.be' in u: return 'youtube'
    if 'on24.com' in u: return 'on24'
    if 'edge.media-server.com' in u: return 'media_server'
    if 'q4cdn.com' in u: return 'direct'
    if 'q4inc.com' in u or 'events.q4inc.com' in u: return 'q4inc'
    if u.endswith('.mp4') or u.endswith('.mp3') or u.endswith('.m4a'): return 'direct'
    if '.m3u8' in u: return 'hls'
    return 'unknown'

def normalize_on24(url):
    eventid = re.search(r'eventid=(\d+)', url)
    key = re.search(r'key=([A-F0-9]+)', url, re.IGNORECASE)
    if eventid and key:
        return f"https://event.on24.com/wcc/r/{eventid.group(1)}/{key.group(1)}"
    return url


# ─── Playwright media finder ──────────────────────────────────────────────────

def find_media_url_inline(page_url, session_file=None, headless=True, q=None):
    """
    Open a webcast page with Playwright and capture media URLs from network traffic.
    Returns (media_url, url_type) or (None, None).
    """
    if not PLAYWRIGHT_AVAILABLE:
        if q:
            q.put({'type': 'log', 'message': 'Playwright not installed. Run setup.command first.', 'level': 'error'})
        return None, None

    candidates = defaultdict(int)
    m3u8_urls = []

    def handle_request(req):
        url = req.url
        for pattern in MEDIA_PATTERNS:
            if re.search(pattern, url, re.IGNORECASE):
                candidates[url] += 1
                if '.m3u8' in url.lower():
                    m3u8_urls.append(url)
                break

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_args = {}
        if session_file and os.path.exists(session_file):
            context_args['storage_state'] = session_file
        context = browser.new_context(**context_args)
        page = context.new_page()
        page.on('request', handle_request)

        try:
            page.goto(page_url, timeout=30000)
        except Exception:
            pass

        if q:
            q.put({'type': 'log', 'message': f'Capturing network traffic for {CAPTURE_SECONDS}s...', 'level': 'info'})
        time.sleep(CAPTURE_SECONDS)
        browser.close()

    if m3u8_urls:
        return m3u8_urls[0], 'hls'

    direct = [(url, count) for url, count in candidates.items()
              if '.m3u8' not in url.lower() and count >= 2]
    if direct:
        best = max(direct, key=lambda x: x[1])
        return best[0], 'direct'

    if candidates:
        best_url = max(candidates.items(), key=lambda x: x[1])[0]
        url_type = 'hls' if '.m3u8' in best_url.lower() else 'direct'
        return best_url, url_type

    return None, None


# ─── Download helpers ─────────────────────────────────────────────────────────

def do_ytdlp(url, output_path, q):
    q.put({'type': 'log', 'message': 'Using yt-dlp...', 'level': 'info'})
    result = subprocess.run(
        ['yt-dlp', '-x', '--audio-format', 'mp3', '-o', output_path, url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        q.put({'type': 'log', 'message': f'yt-dlp error: {result.stderr[-300:]}', 'level': 'error'})
    return result.returncode == 0

def do_ffmpeg(url, output_path, q):
    q.put({'type': 'log', 'message': 'Downloading via ffmpeg...', 'level': 'info'})
    result = subprocess.run(
        ['ffmpeg', '-i', url, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', output_path, '-y'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        q.put({'type': 'log', 'message': f'ffmpeg error: {result.stderr[-300:]}', 'level': 'error'})
    return result.returncode == 0

def do_direct(url, output_path, q):
    q.put({'type': 'log', 'message': 'Downloading direct file...', 'level': 'info'})
    try:
        r = req_lib.get(url, stream=True, timeout=300)
        r.raise_for_status()
        raw = output_path.replace('.mp3', '_raw.mp4')
        with open(raw, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        result = subprocess.run(
            ['ffmpeg', '-i', raw, '-vn', '-acodec', 'libmp3lame', '-q:a', '2', output_path, '-y'],
            capture_output=True, text=True
        )
        if os.path.exists(raw):
            os.remove(raw)
        return result.returncode == 0
    except Exception as e:
        q.put({'type': 'log', 'message': f'Download error: {e}', 'level': 'error'})
        return False


# ─── Main download worker ─────────────────────────────────────────────────────

def run_download(job_id, url, name):
    job = _jobs[job_id]
    q = job['q']

    def log(msg, level='info'):
        q.put({'type': 'log', 'message': msg, 'level': level})

    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, f"{name}.mp3")
    platform = detect_platform(url)
    log(f"Platform: {platform}")
    success = False

    try:
        if platform == 'youtube':
            log("YouTube — using yt-dlp")
            success = do_ytdlp(url, output_path, q)

        elif platform == 'on24':
            clean = normalize_on24(url)
            log("ON24 — using yt-dlp")
            success = do_ytdlp(clean, output_path, q)

        elif platform == 'direct':
            success = do_direct(url, output_path, q)

        elif platform == 'hls':
            success = do_ffmpeg(url, output_path, q)

        elif platform in ('media_server', 'q4inc'):
            sessions = {
                'media_server': os.path.join(APP_DIR, 'sessions', 'media_server_session.json'),
                'q4inc': os.path.join(APP_DIR, 'sessions', 'q4inc_session.json'),
            }
            session_file = sessions.get(platform, '')
            session_file = session_file if os.path.exists(session_file) else None

            # Q4 Inc autoplays — headless works fine, no user action needed
            # edge.media-server.com needs visible browser — user must click play
            headless = (platform == 'q4inc')

            if headless:
                log("Finding media URL in background...", 'info')
            else:
                log("Opening browser — please click PLAY when it opens...", 'warn')

            media_url, url_type = find_media_url_inline(
                url,
                session_file=session_file,
                headless=headless,
                q=q
            )

            if not media_url:
                log("Could not find media URL. Did you click play?", 'error')
                q.put({'type': 'error'})
                return

            log("Found media URL!", 'success')
            if url_type == 'hls':
                success = do_ffmpeg(media_url, output_path, q)
            else:
                success = do_direct(media_url, output_path, q)

        else:
            log("Unknown platform — trying yt-dlp...")
            success = do_ytdlp(url, output_path, q)
            if not success:
                log("Trying direct download...")
                success = do_direct(url, output_path, q)

        if success and os.path.exists(output_path):
            job['file'] = output_path
            log(f"Done! {name}.mp3 is ready.", 'success')
            q.put({'type': 'done', 'jobId': job_id})
        else:
            log("Download failed.", 'error')
            q.put({'type': 'error'})

    except Exception as e:
        log(f"Unexpected error: {e}", 'error')
        q.put({'type': 'error'})


# ─── SSE download endpoint ────────────────────────────────────────────────────

@app.route('/api/download')
def api_download():
    url = request.args.get('url', '').strip()
    name = request.args.get('name', 'download').strip()
    if not url:
        return Response('Missing URL', status=400)

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    _jobs[job_id] = {'q': q, 'media_url': None, 'file': None}

    thread = threading.Thread(target=run_download, args=(job_id, url, name), daemon=True)
    thread.start()

    def generate():
        yield f"data: {json.dumps({'type': 'job_id', 'jobId': job_id})}\n\n"
        while True:
            try:
                item = q.get(timeout=300)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get('type') in ('done', 'error'):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error'})}\n\n"
                break

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

@app.route('/api/file/<job_id>')
def api_file(job_id):
    job = _jobs.get(job_id)
    if not job or not job.get('file') or not os.path.exists(job['file']):
        return Response('File not found', status=404)
    name = os.path.basename(job['file'])
    with open(job['file'], 'rb') as f:
        data = f.read()
    return Response(
        data, mimetype='audio/mpeg',
        headers={
            'Content-Disposition': f'attachment; filename="{name}"',
            'Content-Length': str(len(data)),
        }
    )


# ─── Launch ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = 8001
    url = f'http://127.0.0.1:{port}'

    # Single instance check
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    already_running = sock.connect_ex(('127.0.0.1', port)) == 0
    sock.close()

    if already_running:
        open_in_chrome(url)
        sys.exit(0)

    print(f"Starting Earnings Call Downloader at {url}")
    threading.Timer(1.5, lambda: open_in_chrome(url)).start()
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)