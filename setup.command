#!/bin/bash
cd "$(dirname "$0")"
echo "Setting up Webcast Downloader..."
pip3 install flask requests yt-dlp playwright
playwright install chromium
echo ""
echo "Setup complete! You can now double-click WebcastDownloader.command to launch."
read -p "Press Enter to close..."
