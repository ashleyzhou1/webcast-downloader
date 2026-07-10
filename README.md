# Earnings Call Audio Downloader

A tool for downloading earnings call recordings as mp3 files.

---

## What you need

- A Mac computer
- Google Chrome installed
- This folder

---

## First-Time Setup (do this once only)

### Step 1: Open the folder

You will receive a zip file. Double-click it to unzip. You will see a folder called `webcast-downloader`. Move it somewhere easy to find, like your Desktop.

### Step 2: Run the setup

Inside the folder, find **`setup.command`** and double-click it.

1. Right-click (or Control-click) the file
2. Click **Open**
3. Click **Open** again in the popup

A black terminal window will open and text will scroll automatically. It is installing the necessary tools. When you see **"Setup complete"**, press Enter to close the window.

### Step 3: Set up platform sessions

Inside the folder, find **`setup_sessions.command`** and double-click to open.

A browser window will open for each platform. Log in with your company credentials when prompted, then press Enter in the terminal window after each one. This saves your login so the app can access the webcasts on your behalf.

You only need to do this once. If a download stops working weeks later, run this again, as the session may have expired.

---

## How to Use

### Step 1: Launch the app

Inside the `webcast-downloader` folder, double-click **`WebcastDownloader.command`**.

A black terminal window will flash open briefly, then **Chrome will open automatically** showing the downloader.

**Important: do not close the black terminal window** since the app stops working if you close it.

### Step 2: Download an earnings call

1. **Paste the webcast URL** into the first field — this is the link to the company's earnings call page
2. **Type a filename** in the second field — this is what your mp3 will be called, for example `uber-q1-2026`
3. Click **Download Audio**
4. For some platforms, a browser window will open automatically — **click the play button** when it does, then come back and wait
5. Click **Save mp3** to save the file to your computer when finished downloading

The download usually takes 1-5 minutes depending on the length of the call.

---

## Platform Guide

Different companies host their earnings calls on different platforms. Here is what to expect:

| Company examples | What happens |
|---|---|
| Palantir, Dell (YouTube) | Downloads automatically — nothing to do |
| Intuit, KLA (ON24) | Downloads automatically — nothing to do |
| AppLovin (direct link) | Downloads automatically — nothing to do |
| Uber, Micron (Q4 Inc) | Downloads automatically in background — nothing to do |
| ARM, Intel, Applied Materials, SanDisk (edge.media-server.com) | A browser window opens — click play, then wait |

---

## Troubleshooting

**Download failed with "session" or "login" error**
- Your session has expired — re-run `setup_sessions.command`

**Download is slow**
- This is normal — a 1-hour call can take 3-5 minutes to download

---