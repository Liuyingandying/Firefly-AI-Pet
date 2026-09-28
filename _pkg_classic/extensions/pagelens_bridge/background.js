// Firefly PageLens Bridge — background service worker.
//
// WebSocket client to the Firefly desktop bridge (127.0.0.1:17321) plus a
// contextMenus fallback so right-click selection works even where content
// scripts cannot run (e.g. the built-in PDF viewer, platform permitting).

const WS_URL = "ws://127.0.0.1:17321";
const KEEPALIVE_MS = 20000;

let socket = null;
let reconnectDelay = 1000;
let keepalive = null;

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  try {
    socket = new WebSocket(WS_URL);
  } catch (err) {
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    reconnectDelay = 1000;
    send({ type: "bridge_hello", payload: {} });
    // Delivery deduplication is per connection: Firefly may have restarted.
    lastSentPdfKey = "";
    restoreActivePdf();
    if (keepalive) clearInterval(keepalive);
    keepalive = setInterval(() => send({ type: "bridge_ping" }), KEEPALIVE_MS);
  };
  socket.onclose = () => {
    if (keepalive) clearInterval(keepalive);
    scheduleReconnect();
  };
  socket.onerror = () => {
    try { socket.close(); } catch (_) { /* ignore */ }
  };
}

function scheduleReconnect() {
  setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 5000);
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(message));
    return true;
  } catch (_) { return false; /* connection will be re-established */ }
}

function reportSelection(text, url, page, source) {
  send({
    type: "selection",
    payload: {
      text,
      url,
      page: Number.isInteger(page) && page > 0 ? page : -1,
      // Explicit explain requests carry "user_action"; plain content-script
      // highlights stay "ambient". The desktop only opens the explain
      // surface for user_action.
      source: source === "user_action" ? "user_action" : "ambient",
    },
  });
}

// Content-script selections (HTML pages) — plain highlights are ambient.
chrome.runtime.onMessage.addListener((message) => {
  if (message && message.type === "selection" && message.text) {
    reportSelection(message.text, message.url || location.href, message.page, message.source);
  }
});

// Right-click selection fallback (best-effort in the built-in PDF viewer).
chrome.contextMenus.create(
  {
    id: "firefly-explain",
    title: "用 Firefly 解释选区",
    contexts: ["selection"],
  },
  () => void chrome.runtime.lastError,
);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "firefly-explain" && info.selectionText) {
    // Right-click explain is an explicit user action.
    reportSelection(info.selectionText, tab ? tab.url || "" : "", -1, "user_action");
  }
});

// ---------------------------------------------------------------------------
// Ambient PDF v1.0: detect PDFs opened in any tab (no viewer access).
// The native viewer is sealed to content scripts, but tabs.url/title are
// available here — that is enough for "which paper is the user reading".
// No chrome.debugger, no viewer DOM reads, no scroll observation.
// ---------------------------------------------------------------------------

const PDF_URL_RE = /\.pdf(\?|#|$)/i;
let latestPdfState = null;
let lastSentPdfKey = "";
let pdfRevision = 0;

function flushPdfState() {
  if (!latestPdfState) return;
  const key = JSON.stringify([latestPdfState.url, latestPdfState.title]);
  if (key === lastSentPdfKey) return;
  if (send({ type: "pdf_opened", payload: latestPdfState })) {
    lastSentPdfKey = key;
  }
}

function maybeReportPdf(tab) {
  if (!tab || !tab.url || !tab.title) return;
  if (!PDF_URL_RE.test(tab.url)) return;
  latestPdfState = { url: tab.url, title: tab.title };
  pdfRevision += 1;
  flushPdfState();
}

function restoreActivePdf() {
  const revision = pdfRevision;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    // A newer tab event wins over this asynchronous query snapshot.
    if (!chrome.runtime.lastError && revision === pdfRevision) {
      maybeReportPdf(tabs && tabs[0]);
    }
    flushPdfState();
  });
}

chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.title) maybeReportPdf(tab);
});
chrome.tabs.onActivated.addListener(({ tabId }) => {
  chrome.tabs.get(tabId, (tab) => {
    if (!chrome.runtime.lastError) maybeReportPdf(tab);
  });
});

restoreActivePdf();
connect();
