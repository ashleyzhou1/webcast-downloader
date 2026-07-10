"""
find_media_url.py

Automates the manual DevTools process we used all week to find downloadable
audio/video URLs on webcast player pages:
  1. Open the page in a real browser (Playwright)
  2. Capture all network requests as the page loads and plays
  3. Look for either:
     a) A direct media file requested with status 206 (range request),
        same filename repeated multiple times -> e.g. Oracle, Meta, Cisco,
        Qualcomm, IBM (Q4 Inc/ChorusCall-hosted direct mp4s)
     b) A .m3u8 HLS manifest URL -> e.g. SanDisk, Broadcom, ASML, TSMC,
        Alibaba (media-server.com, CloudFront, hinet.net HLS streams)
  4. Return the best candidate URL(s) found, ready to hand off to
     download.py (for direct files) or ffmpeg (for .m3u8 streams)

IMPORTANT ASSUMPTIONS / LIMITATIONS (be upfront about these in findings):
  - Assumes the browser session is logged in via a saved session
    file created with save_login_session.py
  - When multiple .m3u8 files are found (e.g. TSMC's "playlist" vs
    "chunklist"), this script prefers the one with "playlist" in the name.
  - --debug is required for user to click play

Requires: playwright (pip install playwright && playwright install chromium)

Usage (standalone):
    python find_media_url.py <webcast_page_url> [storage_state.json] [--debug]

    If storage_state.json is omitted, the script tries to auto-detect the
    right saved session based on the URL's domain (see PLATFORM_SESSIONS
    below).

Usage (as a module):
    from find_media_url import find_media_url
    result = find_media_url("https://events.q4inc.com/attendee/529869698", storage_state_path="q4inc_session.json")
"""

import sys
import re
import time
from collections import defaultdict
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


# How long to let the page sit (loading + playing) before we stop
# capturing network requests. Long enough to catch some segments after
# play starts, short enough to not waste time.
CAPTURE_SECONDS = 45

# Extensions that, if requested multiple times with the SAME filename
# and a 206 status, indicate a real direct media file (range requests).
DIRECT_MEDIA_EXTENSIONS = {".mp4", ".mp3", ".m4a", ".wav"}

# Matches numbered HLS segment filenames like "..._00001.ts", "..._00042.ts"
SEGMENT_PATTERN = re.compile(r"^(.+)_(\d+)\.ts$")

# Maps a domain (or substring of one) found in the page URL to the saved
# session file that should be used for it. Add an entry here every time
# you create a new session with save_login_session.py for a new platform.
#
# Example: if you ran
#   python save_login_session.py "https://events.q4inc.com/attendee/X" "q4inc_session.json"
# then any URL containing "q4inc.com" will automatically use that file.
PLATFORM_SESSIONS = {
    "q4inc.com": "q4inc_session.json",
    "choruscall.com": "choruscall_session.json",
    "edge.media-server.com": "media_server_session.json",
    "webcasts.com": "webcasts_session.json",
    "veracast.com": "veracast_session.json",
}


def _guess_session_for_url(page_url: str) -> str:
    """Look up which saved session file matches this URL's platform,
    based on the PLATFORM_SESSIONS table above. Returns None if no
    match is found (caller should fall back to no session, or ask)."""
    url_lower = page_url.lower()
    for domain_substring, session_file in PLATFORM_SESSIONS.items():
        if domain_substring in url_lower:
            return session_file
    return None


def find_media_url(page_url: str, headless: bool = True, storage_state_path: str = None) -> dict:
    """
    Open page_url in a browser, capture network traffic, and try to find
    a downloadable media URL using the patterns learned this week.

    Args:
        page_url: the webcast page URL to inspect
        headless: run the browser without a visible window (default True)
        storage_state_path: path to a saved login session file (cookies +
            local storage), created once via save_login_session.py. If
            provided, the browser reuses that session instead of starting
            logged out. If None, the browser starts with NO login -- most
            of the 20 companies we tested require login, so this will
            likely fail to find media without a storage_state_path.

    Returns a dict:
        {
            "page_url": the input URL,
            "direct_file_candidates": [...],  # repeated filename, 206 status
            "m3u8_candidates": [...],         # .m3u8 manifest URLs found
            "recommended_url": the best single guess, or None,
            "recommended_type": "direct_file" | "m3u8" | None,
        }
    """
    requests_seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        if storage_state_path:
            context = browser.new_context(storage_state=storage_state_path)
        else:
            print(
                "Warning: no storage_state_path given -- browser will start "
                "logged out. Most webcast platforms require login; this run "
                "will likely fail to find a media URL. See save_login_session.py."
            )
            context = browser.new_context()

        page = context.new_page()

        def on_response(response):
            requests_seen.append({
                "url": response.url,
                "status": response.status,
                "resource_type": response.request.resource_type,
            })

        page.on("response", on_response)

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
                "This run will likely find nothing unless the page autoplays. "
                "Use --debug to run with a visible window so you can click play."
            )

        print(f"Capturing network traffic for {CAPTURE_SECONDS}s ...")
        time.sleep(CAPTURE_SECONDS)

        browser.close()

    return _analyze_requests(page_url, requests_seen)


def _analyze_requests(page_url: str, requests_seen: list) -> dict:
    """Apply the heuristics learned this week to the captured requests."""

    # --- Look for numbered .ts segments (Nvidia-style HLS, signature
    # doesn't propagate through ffmpeg's manifest chain) ---
    segment_matches = []
    for r in requests_seen:
        parsed = urlparse(r["url"])
        filename_match = SEGMENT_PATTERN.match(parsed.path.split("/")[-1])
        if filename_match:
            base_path = parsed.path.rsplit("_", 1)[0]  # path without _00001 part
            segment_matches.append({
                "url": r["url"],
                "base_url": f"{parsed.scheme}://{parsed.netloc}{base_path}",
                "query_string": parsed.query,
                "segment_number": filename_match.group(2),
                "num_digits": len(filename_match.group(2)),
            })

    # --- Look for .m3u8 manifests ---
    m3u8_urls = [
        r["url"] for r in requests_seen
        if urlparse(r["url"]).path.lower().endswith(".m3u8")
    ]
    # Dedup while preserving order
    m3u8_urls = list(dict.fromkeys(m3u8_urls))

    # Prefer "playlist" over "chunklist" if both are present (TSMC finding)
    playlist_matches = [u for u in m3u8_urls if "playlist" in u.lower()]
    m3u8_candidates = playlist_matches if playlist_matches else m3u8_urls

    # --- Look for repeated direct media files with 206 status ---
    by_url_206 = defaultdict(int)
    for r in requests_seen:
        ext = urlparse(r["url"]).path.lower()
        if r["status"] == 206 and any(ext.endswith(e) for e in DIRECT_MEDIA_EXTENSIONS):
            by_url_206[r["url"]] += 1

    # A real direct file shows up multiple times (range requests for
    # different byte chunks of the SAME file)
    direct_file_candidates = [url for url, count in by_url_206.items() if count >= 2]

    # --- Decide on a recommendation ---
    # Priority: direct file > numbered segments > m3u8 manifest.
    # Segments are preferred over the manifest itself because we found
    # (Nvidia/Veracast case) that signed manifests often don't propagate
    # their signature to nested segment requests when read by ffmpeg,
    # while the segments work fine when fetched directly with the same
    # signed query string re-applied.
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

    segment_info = segment_matches[0] if segment_matches else None

    return {
        "page_url": page_url,
        "direct_file_candidates": direct_file_candidates,
        "m3u8_candidates": m3u8_candidates,
        "segment_info": segment_info,
        "recommended_url": recommended_url,
        "recommended_type": recommended_type,
    }


def print_next_steps(result: dict, output_path: str = "output.mp4"):
    """Print the exact command the person should run next, based on what
    was found -- mirrors the manual workflow used this week."""
    if result["recommended_type"] == "direct_file":
        print(f"\nFound a direct media file. Recommended next step:")
        print(f'  python download.py "{result["recommended_url"]}" "../samples"')
    elif result["recommended_type"] == "m3u8":
        print(f"\nFound an HLS manifest. Recommended next step:")
        print(f'  ffmpeg -i "{result["recommended_url"]}" "{output_path}"')
    elif result["recommended_type"] == "numbered_segments":
        info = result["segment_info"]
        print(
            f"\nFound numbered HLS segments with a signed query string that "
            f"doesn't propagate through ffmpeg's manifest handling (like "
            f"Nvidia/Veracast). Use the segment-stitching workaround instead:"
        )
        print(f'  python download_hls_segments.py \\')
        print(f'    "{info["base_url"]}" \\')
        print(f'    "{info["query_string"]}" \\')
        print(f'    <num_segments> \\')
        print(f'    "{output_path}" \\')
        print(f'    1 {info["num_digits"]}')
        print(
            f"\n  Note: <num_segments> must be filled in manually -- check the "
            f"m3u8 manifest content or count segments in DevTools to find the total."
        )
    else:
        print(
            "\nNo confident candidate found. This may be a signed/session-bound "
            "URL or a platform this script doesn't yet handle well. Manual "
            "DevTools inspection is the fallback."
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python find_media_url.py <webcast_page_url> [storage_state.json] [--debug]")
        print("\nIf you haven't created a saved login session yet, run:")
        print("  python save_login_session.py <login_page_url> <output_path.json>")
        print("\nAdd --debug to see a visible browser window (helps diagnose why")
        print("the play button click might be failing).")
        print(
            "\nIf you don't pass a session file explicitly, the script will try "
            "to auto-detect one based on the URL's domain (see PLATFORM_SESSIONS "
            "in the script)."
        )
        sys.exit(1)

    url = sys.argv[1]
    debug_mode = "--debug" in sys.argv
    remaining_args = [a for a in sys.argv[2:] if a != "--debug"]
    storage_state = remaining_args[0] if remaining_args else None

    # Auto-detect session file from the URL's domain if not explicitly given
    if storage_state is None:
        guessed = _guess_session_for_url(url)
        if guessed:
            print(f"No session file given -- auto-detected '{guessed}' based on URL domain.")
            storage_state = guessed
        else:
            print(
                "No session file given and could not auto-detect one for this "
                "domain. Add an entry to PLATFORM_SESSIONS, or pass a session "
                "file explicitly as the second argument."
            )

    result = find_media_url(url, headless=not debug_mode, storage_state_path=storage_state)

    print(f"\n--- Results for {url} ---")
    print(f"Direct file candidates found: {len(result['direct_file_candidates'])}")
    for u in result["direct_file_candidates"]:
        print(f"  {u}")
    print(f"\n.m3u8 candidates found: {len(result['m3u8_candidates'])}")
    for u in result["m3u8_candidates"]:
        print(f"  {u}")

    print_next_steps(result)