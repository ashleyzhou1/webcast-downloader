"""
download.py

Given a direct URL to a media file (audio like mp3/wav/m4a, or video like
mp4), downloads it locally. If it's a video file, extracts just the audio
track using ffmpeg, since that's all transcribe.py needs.

Usage (standalone):
    python download.py https://example.com/path/to/webcast.mp3
    python download.py https://example.com/path/to/webcast.mp4

Usage (as a module):
    from download import download_media
    audio_path = download_media("https://example.com/webcast.mp3")
"""

import sys
import subprocess
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

# File extensions we treat as "audio" vs "video" vs "document". Anything
# else, we still attempt to download but warn that it's an unrecognized type.
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt"}

DEFAULT_HEADERS = {
    # Some servers block requests with no User-Agent, assuming it's a bot.
    # A common browser UA avoids that without being deceptive about intent.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

def _guess_extension(url: str, content_type: str = "") -> str:
    """Figure out the file extension from the URL path, falling back to
    the Content-Type header if the URL itself doesn't have a clear one."""
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext:
        return ext

    # Fallback: infer from Content-Type header if the URL had no extension
    content_type = content_type.lower()
    if "mp3" in content_type or "mpeg" in content_type:
        return ".mp3"
    if "mp4" in content_type:
        return ".mp4"
    if "wav" in content_type:
        return ".wav"
    if "pdf" in content_type:
        return ".pdf"
    if "msword" in content_type:
        return ".doc"
    if "wordprocessingml" in content_type:
        return ".docx"
    return ""

def _guess_referer(url: str) -> str:
    """Some CDNs reject stream requests unless the Referer header matches
    the page that would normally be playing the media."""
    if "webcasts.com" in url:
        return "https://event.webcasts.com/"
    return None


def download_hls_stream(url: str, output_dir: str = "../samples", output_path: str = None) -> Path:
    """
    Download and stitch an HLS stream (.m3u8 playlist) into a single mp3
    using ffmpeg directly. Platforms like webcasts.com serve audio as
    segmented, signed, time-limited HLS streams -- capturing each segment
    live via a browser is racy. ffmpeg fetches and stitches every segment
    itself in one shot, using the same signed token in the .m3u8 URL.
    """
    if output_path:
        # Caller gave an exact file path to use -- skip auto-naming.
        local_path = Path(output_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        url_filename = unquote(Path(urlparse(url).path).stem) or "downloaded_media"
        if url_filename.lower() in ("chunklist", "playlist"):
            url_filename = "webcast_audio"
        local_path = output_dir / f"{url_filename}.mp3"

    cmd = ["ffmpeg", "-y"]
    referer = _guess_referer(url)
    if referer:
        cmd += ["-headers", f"Referer: {referer}\r\n"]
    cmd += ["-i", url, "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(local_path)]

    print(f"Downloading HLS stream via ffmpeg: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to download HLS stream:\n{result.stderr}")

    print(f"Downloaded HLS stream to: {local_path}")
    return local_path

def download_file(url: str, output_dir: str = "../samples") -> Path:
    """Download the raw file at `url` into `output_dir`. Returns the local
    path. Raises requests.HTTPError if the download fails."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading: {url}")
    response = requests.get(url, headers=DEFAULT_HEADERS, stream=True, timeout=60)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    ext = _guess_extension(url, content_type)
    if not ext:
        print(f"Warning: could not determine file extension for {url} "
              f"(Content-Type was '{content_type}'). Defaulting to .bin")
        ext = ".bin"

    # Use the original filename from the URL if there is one, else a generic name
    # unquote() converts URL-encoded characters like %20 back to real ones (space)
    url_filename = unquote(Path(urlparse(url).path).stem) or "downloaded_media"
    local_path = output_dir / f"{url_filename}{ext}"

    total_bytes = 0
    with open(local_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            total_bytes += len(chunk)

    print(f"Downloaded {total_bytes / (1024*1024):.1f} MB to: {local_path}")
    return local_path


def extract_audio_from_video(video_path: Path) -> Path:
    """Use ffmpeg to pull just the audio track out of a video file,
    saving it as an mp3 alongside the original. Returns the audio path."""
    audio_path = video_path.with_suffix(".mp3")
    print(f"Extracting audio track from {video_path.name} -> {audio_path.name}")

    # -vn = no video, -acodec mp3 = encode audio as mp3, -y = overwrite if exists
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path), "-vn", "-acodec", "mp3", "-y", str(audio_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed to extract audio:\n{result.stderr}")

    print(f"Audio extracted to: {audio_path}")
    return audio_path


def download_media(url: str, output_dir: str = "../samples") -> Path:
    """
    Main entry point. Given a direct URL to an audio or video file,
    downloads it and returns a local path to an audio file ready for
    transcribe.py.

    If the URL points to a video file, the audio track is automatically
    extracted via ffmpeg after download.

    Does not handle embedded players / pages without a direct file link.
    """
    if urlparse(url).path.lower().endswith(".m3u8"):
        return download_hls_stream(url, output_dir=output_dir)
    local_path = download_file(url, output_dir=output_dir)
    ext = local_path.suffix.lower()

    if ext in VIDEO_EXTENSIONS:
        audio_path = extract_audio_from_video(local_path)
        return audio_path
    elif ext in AUDIO_EXTENSIONS:
        return local_path
    elif ext in DOCUMENT_EXTENSIONS:
        print(f"Downloaded a document file ({ext}). No transcription needed; "
              f"text can be extracted directly from this file.")
        return local_path
    else:
        print(f"Warning: '{ext}' is not a recognized audio, video, or document "
              f"extension. Returning the file as-is; downstream scripts may or "
              f"may not handle it.")
        return local_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python download.py <url> [output_dir]")
        sys.exit(1)

    url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "../samples"

    audio_path = download_media(url, output_dir=output_dir)
    print(f"\nReady for transcription: {audio_path}")
    print(f"Next step: python transcribe.py {audio_path}")