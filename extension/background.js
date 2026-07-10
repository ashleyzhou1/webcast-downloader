// background.js
// Intercepts network requests across all tabs to find media URLs
// and reports them to the local Earnings Call Downloader app

const SERVER = 'http://127.0.0.1:8001';

// Patterns that indicate a media file
const MEDIA_PATTERNS = [
  /\.m3u8/i,
  /\.mp4/i,
  /\.mp3/i,
  /\.m4a/i,
  /media-server\.com/i,
  /akamaized\.net.*\.(m3u8|mp4)/i,
  /cloudfront\.net.*\.(m3u8|mp4)/i,
  /q4cdn\.com.*\.(mp4|mp3)/i,
  /static\.events\.q4inc\.com.*\.(mp4|mp3)/i,
];

// Current active job: { jobId, tabId }
let activeJob = null;

// URLs already reported for this job
const reported = new Set();

// Listen for messages from the web page
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'SET_JOB') {
    activeJob = { jobId: msg.jobId, tabId: sender.tab?.id };
    reported.clear();
    console.log('[Helper] Active job set:', activeJob);
    sendResponse({ ok: true });
  }
  sendResponse({});
});

// Also listen for postMessage from the page via content script injection
// We intercept webRequest for all tabs
chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (!activeJob) return;
    if (details.tabId < 0) return;

    const url = details.url;
    const isMedia = MEDIA_PATTERNS.some(p => p.test(url));
    if (!isMedia) return;
    if (reported.has(url)) return;

    // Only report 200 or 206 responses (successful media fetches)
    if (![200, 206].includes(details.statusCode)) return;

    reported.add(url);
    console.log(`[Helper] Captured: ${url.substring(0, 100)}`);

    fetch(`${SERVER}/api/media-found`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jobId: activeJob.jobId,
        url,
        tabId: details.tabId,
        statusCode: details.statusCode,
      })
    }).catch(err => console.error('[Helper] Report failed:', err));
  },
  { urls: ['<all_urls>'] }
);

// Signal to the page that the extension is ready
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url?.includes('127.0.0.1:8001')) {
    chrome.tabs.sendMessage(tabId, { type: 'EXTENSION_READY' }).catch(() => {});
  }
});

console.log('[Helper] Webcast Downloader Helper ready');
