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
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, COMM, TXXX
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

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
    if 'webcasts.com' in u: return 'webcasts'
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
        # Check the actual file extension at the END of the path (ignoring
        # the query string) -- not just whether the pattern appears anywhere
        # in the URL. Some platforms (webcasts.com) put ".m4a" mid-path as
        # part of a folder-like segment, e.g. ".../xyz_1.m4a/media_0.ts" --
        # that's a .ts segment, not an .m4a file, and must not be treated
        # as a downloadable "direct file" candidate.
        url = req.url
        path_only = url.split('?')[0].lower()
        if path_only.endswith(('.m3u8', '.mp4', '.mp3', '.m4a')):
            candidates[url] += 1
            if path_only.endswith('.m3u8'):
                m3u8_urls.append(url)

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

    # Direct files (the real recording) take priority over any .m3u8 --
    # some Q4 pages also load a captions/subtitles.m3u8 track alongside
    # the real mp4, which has no audio and must never be picked instead.
    direct = [(url, count) for url, count in candidates.items()
          if '.m3u8' not in url.lower() and count >= 1]
    if direct:
        best = max(direct, key=lambda x: x[1])
        return best[0], 'direct'

    # Only fall back to .m3u8 if it's not a captions/subtitles track
    real_m3u8 = [u for u in m3u8_urls if 'caption' not in u.lower() and 'subtitle' not in u.lower()]
    if real_m3u8:
        return real_m3u8[0], 'hls'

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

def do_webcasts_hls(m3u8_url, referer, output_path, q):
    """
    webcasts.com's master playlist URL carries a one-time-use token, but
    the segment list ("chunklist.m3u8") and every individual segment need
    no token at all -- just a Referer header matching the real event page.
    ffmpeg's own network fetch gets blocked by the CDN regardless of what
    headers we pass it, so segments are downloaded directly with requests
    instead, then stitched into an mp3 locally.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": referer,
    }
    base_dir = m3u8_url.split("?")[0].rsplit("/", 1)[0]
    chunklist_url = f"{base_dir}/chunklist.m3u8"

    q.put({'type': 'log', 'message': 'Fetching segment list...', 'level': 'info'})
    try:
        resp = req_lib.get(chunklist_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        q.put({'type': 'log', 'message': f'Chunklist fetch failed: {e}', 'level': 'error'})
        return False

    segment_names = [
        line.strip() for line in resp.text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not segment_names:
        q.put({'type': 'log', 'message': 'Chunklist had no segments.', 'level': 'error'})
        return False

    q.put({'type': 'log', 'message': f'Downloading {len(segment_names)} segments...', 'level': 'info'})
    temp_dir = tempfile.mkdtemp()
    segment_files = []
    for i, seg_name in enumerate(segment_names):
        seg_url = f"{base_dir}/{seg_name}"
        local_path = os.path.join(temp_dir, f"seg_{i:05d}.ts")
        try:
            seg_resp = req_lib.get(seg_url, headers=headers, timeout=30)
            seg_resp.raise_for_status()
            with open(local_path, 'wb') as f:
                f.write(seg_resp.content)
            segment_files.append(local_path)
        except Exception as e:
            q.put({'type': 'log', 'message': f'Segment {i+1} failed: {e}', 'level': 'warn'})
        if (i + 1) % 50 == 0 or i == len(segment_names) - 1:
            q.put({'type': 'log', 'message': f'{i+1}/{len(segment_names)} segments downloaded', 'level': 'info'})

    if not segment_files:
        q.put({'type': 'log', 'message': 'No segments downloaded successfully.', 'level': 'error'})
        return False

    concat_list_path = os.path.join(temp_dir, 'concat_list.txt')
    with open(concat_list_path, 'w') as f:
        for seg in segment_files:
            f.write(f"file '{seg}'\n")

    q.put({'type': 'log', 'message': 'Stitching segments into mp3...', 'level': 'info'})
    result = subprocess.run(
        ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_list_path,
         '-vn', '-acodec', 'libmp3lame', '-q:a', '2', output_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        q.put({'type': 'log', 'message': f'ffmpeg stitching failed: {result.stderr[-300:]}', 'level': 'error'})
        return False
    return True


def tag_mp3(path, ticker, company_name, quarter, published_date, q):
    """
    Writes ID3 tags onto the downloaded mp3 so this info survives even if
    the file is renamed or moved out of this app. Standard tags (title,
    artist, album, date) show up in Finder/iTunes/etc. Custom TXXX tags
    store the exact raw fields too, so another script could read them
    back out later without parsing the title text.
    """
    if not MUTAGEN_AVAILABLE:
        q.put({'type': 'log', 'message': 'mutagen not installed -- skipping tags (file still saved).', 'level': 'warn'})
        return
    if not any([ticker, company_name, quarter, published_date]):
        return  # nothing entered, nothing to tag

    try:
        audio = MP3(path, ID3=ID3)
        try:
            audio.add_tags()
        except Exception:
            pass  # tags already exist on this file

        title_parts = [p for p in [company_name, ticker, quarter, "Earnings Call"] if p]
        audio.tags.add(TIT2(encoding=3, text=" ".join(title_parts)))
        if company_name:
            audio.tags.add(TPE1(encoding=3, text=company_name))
        if ticker:
            audio.tags.add(TALB(encoding=3, text=ticker))
        if published_date:
            audio.tags.add(TDRC(encoding=3, text=published_date))

        comment_parts = []
        if company_name: comment_parts.append(f"Company: {company_name}")
        if ticker: comment_parts.append(f"Ticker: {ticker}")
        if quarter: comment_parts.append(f"Quarter: {quarter}")
        if published_date: comment_parts.append(f"Published: {published_date}")
        audio.tags.add(COMM(encoding=3, lang='eng', desc='desc', text=" | ".join(comment_parts)))

        audio.tags.add(TXXX(encoding=3, desc='ticker', text=ticker or ''))
        audio.tags.add(TXXX(encoding=3, desc='company_name', text=company_name or ''))
        audio.tags.add(TXXX(encoding=3, desc='quarter', text=quarter or ''))
        audio.tags.add(TXXX(encoding=3, desc='published_date', text=published_date or ''))

        audio.save()
        q.put({'type': 'log', 'message': 'Tagged mp3 with company info.', 'level': 'success'})
    except Exception as e:
        q.put({'type': 'log', 'message': f'Tagging failed (file still saved): {e}', 'level': 'warn'})


# ─── Main download worker ─────────────────────────────────────────────────────

def run_download(job_id, url, name, ticker=None, company_name=None, quarter=None, published_date=None):
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

        elif platform in ('media_server', 'q4inc', 'webcasts'):
            sessions = {
                'media_server': os.path.join(APP_DIR, 'sessions', 'media_server_session.json'),
                'q4inc': os.path.join(APP_DIR, 'sessions', 'q4inc_session.json'),
                'webcasts': os.path.join(APP_DIR, 'sessions', 'webcasts_session.json'),
            }
            session_file = sessions.get(platform, '')
            session_file = session_file if os.path.exists(session_file) else None

            # Not every Q4 event page autoplays without a click -- some
            # need the user to press play, same as edge.media-server.com
            # and webcasts.com. Always show the browser so this works
            # reliably across every Q4 company, not just the ones that
            # happen to autoplay.
            headless = False

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
            if platform == 'webcasts' and url_type == 'hls':
                # webcasts.com's signed master-playlist token is single-use;
                # ffmpeg's own network fetch gets blocked by the CDN
                # regardless of headers. Download segments directly instead,
                # authenticated by Referer only.
                success = do_webcasts_hls(media_url, referer=url, output_path=output_path, q=q)
            elif url_type == 'hls':
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
            tag_mp3(output_path, ticker, company_name, quarter, published_date, q)
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
    ticker = request.args.get('ticker', '').strip()
    company_name = request.args.get('company_name', '').strip()
    quarter = request.args.get('quarter', '').strip()
    published_date = request.args.get('published_date', '').strip()
    if not url:
        return Response('Missing URL', status=400)

    job_id = str(uuid.uuid4())
    q = queue.Queue()
    _jobs[job_id] = {'q': q, 'media_url': None, 'file': None}

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, name),
        kwargs={'ticker': ticker, 'company_name': company_name, 'quarter': quarter, 'published_date': published_date},
        daemon=True,
    )
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