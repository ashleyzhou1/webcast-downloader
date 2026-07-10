"""
save_login_session.py

Opens a visible browser window so you can manually log in to a
webcast platform (enter your email, click through registration, etc.),
then saves the resulting session (cookies + local storage) to a file.

That saved file can then be reused by find_media_url.py to inspect
webcast pages without needing to log in again every time.

Note: each platform (Q4 Inc, ChorusCall, edge.media-server.com, etc.)
likely needs its own saved session. Run this once per platform, save each to a
differently-named file.

Usage:
    python save_login_session.py <login_page_url> <output_path.json>

    Example:
    python save_login_session.py "https://events.q4inc.com/attendee/529869698" "q4inc_session.json"

Then, once you've logged in and the browser window is closed, use the
saved file with find_media_url.py:
    python find_media_url.py <other_q4inc_url> q4inc_session.json
"""

import sys
from playwright.sync_api import sync_playwright


def save_login_session(login_url: str, output_path: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # visible window, required for manual login
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening {login_url} ...")
        page.goto(login_url)

        print("\n" + "=" * 60)
        print("A browser window has opened. Please log in manually now.")
        print("Once you're logged in and can see the webcast content,")
        print("come back here and press Enter to save your session.")
        print("=" * 60 + "\n")
        input("Press Enter once logged in...")

        context.storage_state(path=output_path)
        print(f"\nSession saved to: {output_path}")
        print("You can now use this with find_media_url.py for other")
        print("pages on this same platform.")

        browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python save_login_session.py <login_page_url> <output_path.json>")
        sys.exit(1)

    login_url = sys.argv[1]
    output_path = sys.argv[2]
    save_login_session(login_url, output_path)