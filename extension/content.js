// content.js
// Runs on the downloader page (127.0.0.1:8001)
// Bridges postMessage from the page to the background service worker

// Tell the page the extension is ready
window.postMessage('webcast-helper-ready', '*');

// Listen for job ID from the page
window.addEventListener('message', (e) => {
  if (e.data?.type === 'webcast-set-job') {
    chrome.runtime.sendMessage({
      type: 'SET_JOB',
      jobId: e.data.jobId,
    });
  }
});

// Listen for ready signal from background
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'EXTENSION_READY') {
    window.postMessage('webcast-helper-ready', '*');
  }
});
