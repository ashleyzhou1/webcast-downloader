"""
find_media_url.py

Automates the manual DevTools process to find downloadable audio/video URLs
on webcast player pages using Playwright to capture network traffic.

Usage (standalone):
    python find_media_url.py <url> [session.json] [--debug] [--download output.mp3]

Usage (as a module):
    from find_media_url import find_media_url
    result = find_media_url("https://events.q4inc.com/attendee/529869698", storage_state_path="q4inc_session.json")
"""

import sys
import re
import time
import json
import subprocess
import threading
import tempfile
import os
from collections import defaultdict
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from download import download_hls_stream

CAPTURE_SECONDS = 45
DIRECT_MEDIA_EXTENSIONS = [".mp4", ".mp3", ".m4a", ".wav", "/media"]
SEGMENT_PATTERN = re.compile(r"^(.+?)(_(\d+))\.ts$")

PLATFORM_SESSIONS = {
    "q4inc.com": "sessions/q4inc_session.json",
    "events.q4inc.com": "sessions/q4inc_session.json",
    "media-server.com": "sessions/media_server_session.json",
    "webcasts.com": "sessions/webcasts_session.json",
}


def _get_auto_session(url):
    url_lower = url.lower()
    for domain_substring, session_file in PLATFORM_SESSIONS.items():
        if domain_substring in url_lower:
            return session_file
    return None


def find_media_url(page_url: str, headless: bool = True, storage_state_path: str = None) -> dict:
    requests_seen = []

    # Check for --download flag
    auto_output = None
    for i, arg in enumerate(sys.argv):
        if arg == '--download' and i + 1 < len(sys.argv):
            auto_output = sys.argv[i + 1]
            break

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        if storage_state_path:
            context = browser.new_context(storage_state=storage_state_path)
        else:
            print("Warning: no storage_state_path given -- browser will start logged out.")
            context = browser.new_context()

        page = context.new_page()

        # State for download tracking
        download_triggered = [False]
        ffmpeg_done = [False]
        segments_data = []  # for webcasts.com segment stitching

        def on_response(response):
            requests_seen.append({
                "url": response.url,
                "status": response.status,
                "resource_type": response.request.resource_type,
            })
            url = response.url

            # Debug logging
            if any(x in url for x in ['.m3u8', '.mp4', '.m4a', 'cdn', 'stream', 'media', 'video', 'audio']):
                print(f"[DEBUG] {response.status} {url[:120]}")

            # Capture webcasts.com audio segments
            if auto_output and '/media' in url and 'webcasts.com' in url and response.status == 200:
                try:
                    data = response.body()
                    if data and len(data) > 1000:
                        segments_data.append(data)
                        print(f"[SEGMENT] {len(segments_data)} captured ({len(data)} bytes)")
                except Exception as e:
                    print(f"[SEGMENT FAIL] {e}")

        page.on("response", on_response)
        context.on("response", on_response)

        print(f"Opening {page_url} ...")
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

        if not headless:
            print("\n" + "=" * 60)
            print("Browser window is open. Please click Play on the webcast now.")
            print("=" * 60 + "\n")
            print("Waiting 60 seconds for you to click play...")
            time.sleep(60)
        else:
            print(
                "Warning: running headless with no way to manually click play. "
                "Use --debug to run with a visible window so you can click play."
            )

        print(f"Capturing network traffic for {CAPTURE_SECONDS}s ...")
        time.sleep(CAPTURE_SECONDS)

        # Stitch webcasts.com segments if we captured any
        if auto_output and segments_data:
            print(f"\nStitching {len(segments_data)} segments into mp3...")
            tmp = tempfile.mktemp(suffix='.ts')
            with open(tmp, 'wb') as f:
                for seg in segments_data:
                    f.write(seg)
            subprocess.run([
                'ffmpeg', '-i', tmp,
                '-vn', '-acodec', 'libmp3lame', '-q:a', '2', auto_output, '-y'
            ])
            os.remove(tmp)
            print(f"Saved to {auto_output}")

        browser.close()

    result = _analyze_requests(page_url, requests_seen)

    # If we found a signed .m3u8 URL and --download was used, fetch it via
    # ffmpeg right now -- before the signed token has a chance to expire.
    # Skip this if the webcasts.com in-browser segment capture above already
    # succeeded in producing the output file.
    if auto_output and result["m3u8_candidates"] and not os.path.exists(auto_output):
        m3u8_url = result["m3u8_candidates"][0]
        print(f"\nAuto-downloading HLS stream immediately (token may expire soon)...")
        try:
            download_hls_stream(m3u8_url, output_path=auto_output)
        except Exception as e:
            print(f"Auto-download failed: {e}")

    return result


def _analyze_requests(page_url: str, requests_seen: list) -> dict:
    """Apply heuristics to find the best media URL from captured requests."""

    # Numbered HLS segments (Nvidia/Veracast style)
    segment_matches = []
    for r in requests_seen:
        parsed = urlparse(r["url"])
        filename_match = SEGMENT_PATTERN.match(parsed.path.split("/")[-1])
        if filename_match:
            base_path = parsed.path.rsplit("_", 1)[0]
            segment_matches.append({
                "url": r["url"],
                "base_url": f"{parsed.scheme}://{parsed.netloc}{base_path}",
                "query_string": parsed.query,
                "segment_number": filename_match.group(3),
                "num_digits": len(filename_match.group(3)),
            })

    # .m3u8 manifests
    m3u8_urls = [
        r["url"] for r in requests_seen
        if urlparse(r["url"]).path.lower().endswith(".m3u8")
    ]
    m3u8_urls = list(dict.fromkeys(m3u8_urls))
    playlist_matches = [u for u in m3u8_urls if "playlist" in u.lower()]
    m3u8_candidates = playlist_matches if playlist_matches else m3u8_urls

    # Direct media files (status 200 or 206)
    by_url = defaultdict(int)
    for r in requests_seen:
        ext = urlparse(r["url"]).path.lower()
        if r["status"] in (200, 206) and any(ext.endswith(e) for e in DIRECT_MEDIA_EXTENSIONS):
            by_url[r["url"]] += 1
    direct_file_candidates = [url for url, count in by_url.items() if count >= 1]

    # Pick best recommendation
    recommended_url = None
    recommended_type = None
    if direct_file_candidates:
        recommended_url = direct_file_candidates[0]
        recommended_type = "direct_file"
    elif segment_matches:
        recommended_url = segment_matches[0]["url"]
        recommended_type = "numbered_segments"
    elif m3u8_candidates:
        recommended_url = m3u8_candidates[0]
        recommended_type = "m3u8"

    return {
        "page_url": page_url,
        "direct_file_candidates": direct_file_candidates,
        "m3u8_candidates": m3u8_candidates,
        "segment_info": segment_matches[0] if segment_matches else None,
        "recommended_url": recommended_url,
        "recommended_type": recommended_type,
    }


def print_next_steps(result: dict):
    direct = result["direct_file_candidates"]
    m3u8 = result["m3u8_candidates"]
    seg = result["segment_info"]

    if not direct and not m3u8 and not seg:
        print(
            "No confident candidate found. This may be a signed/session-bound URL "
            "or a platform this script doesn't yet handle well. "
            "Manual DevTools inspection is the fallback."
        )
        return

    if direct:
        print(f"Found a direct media file. Recommended next step:")
        print(f'  python download.py "{direct[0]}" "../samples"')
        return

    if seg:
        base = seg["base_url"]
        qs = seg["query_string"]
        num_digits = seg["num_digits"]
        print("Found numbered HLS segments with a signed query string.")
        print(f"  python download_hls_segments.py \\")
        print(f'    "{base}" \\')
        print(f'    "{qs}" \\')
        print(f"    <num_segments> \\")
        print(f'    "output.mp4" \\')
        print(f"    1 {num_digits}")
        print("  Note: <num_segments> must be filled in manually.")
        return

    if m3u8:
        print(f"Found an HLS manifest. Recommended next step:")
        print(f'  ffmpeg -i "{m3u8[0]}" "output.mp4"')


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python find_media_url.py <url> [session.json] [--debug] [--download output.mp3]")
        sys.exit(1)

    url = sys.argv[1]
    storage_state = None
    debug_mode = "--debug" in sys.argv

    skip_next = False
    for i, arg in enumerate(sys.argv[2:], 2):
        if skip_next:
            skip_next = False
            continue
        if arg == '--download':
            skip_next = True
            continue
        if arg.endswith(".json"):
            storage_state = arg
            break

    if not storage_state:
        auto = _get_auto_session(url)
        if auto:
            if os.path.exists(auto):
                storage_state = auto
                print(f"Auto-detected session: {auto}")
            else:
                print(f"No session file given -- auto-detected '{auto}' based on URL domain.")

    result = find_media_url(url, headless=not debug_mode, storage_state_path=storage_state)

    print(f"\n--- Results for {url} ---")
    print(f"Direct file candidates found: {len(result['direct_file_candidates'])}")
    for u in result["direct_file_candidates"]:
        print(f"  {u}")
    print(f"\n.m3u8 candidates found: {len(result['m3u8_candidates'])}")
    for u in result["m3u8_candidates"]:
        print(f"  {u}")

    print_next_steps(result)