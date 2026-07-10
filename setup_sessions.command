#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "Setting up Q4 Inc session (Uber, Micron, etc.)..."
echo "A browser will open — log in if prompted, then press Enter here."
python3 src/save_login_session.py "https://events.q4inc.com/attendee/700790269" "sessions/q4inc_session.json"
echo ""
echo "Setting up Media Server session (ARM, INTC, AMAT, SanDisk, etc.)..."
echo "A browser will open — navigate to any edge.media-server.com webcast, click play, then press Enter here."
python3 src/save_login_session.py "https://edge.media-server.com/mmc/p/parfpki9/" "sessions/media_server_session.json"
echo ""
echo "Sessions saved. You are ready to use the downloader."
read -p "Press Enter to close..."
