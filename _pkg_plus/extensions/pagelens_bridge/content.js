// Firefly PageLens Bridge — content script.
//
// Ambient Reading Mode (HTML pages only). Reports:
//   - selection: text the user selected (mouseup + quiet period)
//   - page_context: current url/title/heading + a bounded slice of the
//     visible text (never the whole page), throttled so it only fires when
//     something actually changed.
// NOTE: the browser's built-in PDF viewer is an extension page that content
// scripts cannot reach — native-viewer PDFs are intentionally out of scope;
// the contextMenus fallback in background.js remains as best-effort.

let lastSelection = "";
let lastContextKey = "";

// ---------------------------------------------------------------------------
// selection
// ---------------------------------------------------------------------------

function currentSelection() {
  const sel = window.getSelection();
  if (!sel) return "";
  return sel.toString().trim();
}

function detectPage() {
  // Normal pages: unknown page number.
  return -1;
}

function sendSelection() {
  const text = currentSelection();
  if (!text || text === lastSelection || text.length > 2000) return;
  lastSelection = text;
  chrome.runtime.sendMessage({
    type: "selection",
    text,
    url: location.href,
    page: detectPage(),
    // A mouseup highlight is ambient context, never an explicit explain
    // request. The desktop updates compact context only; the explanation
    // surface opens solely for "user_action" (right-click explain entry).
    source: "ambient",
  });
}

let settleTimer = null;
document.addEventListener("mouseup", () => {
  if (settleTimer) clearTimeout(settleTimer);
  settleTimer = setTimeout(sendSelection, 250);
});

// ---------------------------------------------------------------------------
// page context (heading + bounded visible text)
// ---------------------------------------------------------------------------

function currentHeading() {
  // Nearest heading above the selection anchor; fall back to the first
  // heading on the page.
  const sel = window.getSelection();
  let node = sel && sel.anchorNode ? sel.anchorNode : null;
  for (let depth = 0; node && depth < 8; node = node.parentElement, depth += 1) {
    if (!node || node.nodeType !== 1) continue;
    const prev = node.querySelector
      ? node.querySelector("h1,h2,h3,h4")
      : null;
    if (prev) return prev.innerText.trim();
    const tag = node.tagName || "";
    if (/^H[1-4]$/.test(tag)) return node.innerText.trim();
  }
  const first = document.querySelector("h1,h2,h3");
  return first ? first.innerText.trim() : "";
}

function visibleTextSlice() {
  // Bounded visible text: the element around the selection anchor, or the
  // element at the viewport center. Never the whole page body.
  const sel = window.getSelection();
  let node = sel && sel.anchorNode ? sel.anchorNode : null;
  for (let depth = 0; node && depth < 6; node = node.parentElement, depth += 1) {
    if (!node || node.nodeType !== 1) continue;
    if (node.innerText && node.innerText.trim().length >= 40) {
      return node.innerText.trim();
    }
  }
  const cx = Math.floor(window.innerWidth / 2);
  const cy = Math.floor(window.innerHeight / 2);
  const center = document.elementFromPoint(cx, cy);
  if (center && center.innerText) {
    const t = center.innerText.trim();
    if (t.length >= 40) return t;
  }
  return document.body ? document.body.innerText.trim() : "";
}

function sendPageContext() {
  const heading = currentHeading().slice(0, 120);
  const text = visibleTextSlice().slice(0, 1500);
  const key = `${location.href}|${heading}|${text.length}`;
  if (key === lastContextKey) return; // nothing changed — do not spam
  lastContextKey = key;
  chrome.runtime.sendMessage({
    type: "page_context",
    url: location.href,
    title: document.title || "",
    heading,
    text,
  });
}

// Throttle: page context refreshes at most every 5 s and only on change.
let contextTimer = null;
function schedulePageContext() {
  if (contextTimer) return;
  contextTimer = setTimeout(() => {
    contextTimer = null;
    sendPageContext();
  }, 5000);
}

window.addEventListener("scroll", schedulePageContext, { passive: true });
document.addEventListener("selectionchange", schedulePageContext);
document.addEventListener("DOMContentLoaded", schedulePageContext);
// Fire once shortly after load so the desktop has an initial context.
setTimeout(schedulePageContext, 3000);