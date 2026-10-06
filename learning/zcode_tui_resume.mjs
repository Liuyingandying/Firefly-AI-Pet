// Keep Z Code's interactive TUI visible while asking its own /resume command
// to project persisted messages. The CLI --resume flag restores runtime state
// but version 0.16.9 starts the TUI with an empty transcript.
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const [entry, sessionId, workspace] = process.argv.slice(2);
if (!entry || !/^sess_[A-Za-z0-9-]+$/.test(sessionId ?? "") || !workspace) {
  process.stderr.write("Z Code TUI resume arguments are invalid.\n");
  process.exit(2);
}

const runtimeRoot = resolve(dirname(entry), "..");
const requireFromRuntime = createRequire(join(runtimeRoot, "package.json"));
const pty = requireFromRuntime("node-pty");
const node = process.execPath;
// Windows Terminal and the nested ConPTY each have their own code page.
// Both must be UTF-8 or the TUI glyphs and Chinese text become mojibake.
if (process.platform === "win32") {
  spawnSync(process.env.ComSpec || "cmd.exe", ["/d", "/c", "chcp 65001 >nul"], {
    stdio: "inherit",
  });
}
const childScript = join(dirname(fileURLToPath(import.meta.url)), "zcode_tui_child.cmd");
if (/["%\r\n]/.test(childScript)) {
  throw new Error("Unsupported metacharacter in Firefly TUI launcher path");
}
const child = pty.spawn(process.env.ComSpec || "cmd.exe", [], {
  name: "xterm-256color",
  // Nested ConPTY corrupts later CJK redraws in Windows Terminal. The
  // bundled WinPTY backend preserves the rendered teaching transcript.
  useConpty: process.env.FIREFLY_TUI_PTY_BACKEND === "conpty",
  cols: process.stdout.columns || 120,
  rows: process.stdout.rows || 30,
  cwd: workspace,
  env: {
    ...process.env,
    FIREFLY_TUI_NODE: node,
    FIREFLY_TUI_ENTRY: entry,
    FIREFLY_TUI_SESSION: sessionId,
  },
});
setTimeout(() => child.write(`call "${childScript}"\r`), 300);

let recentOutput = "";
let resumeSent = false;
let ready = false;
let sendTimer;
let clearTimer;
const fallbackTimer = setTimeout(() => {
  if (!resumeSent) {
    resumeSent = true;
    debug("startup fallback resume");
    child.write(`/resume ${sessionId}\r`);
  }
}, 6000);
const readyPhrase = `Resumed session ${sessionId}`;
const renderedReadyPhrase = `Resumed session ${sessionId.replace(/^sess_/, "sess")}`;
function debug(message) {
  if (process.env.FIREFLY_TUI_DEBUG_LOG) {
    appendFileSync(process.env.FIREFLY_TUI_DEBUG_LOG, `${message}\n`, "utf8");
  }
}
function plainTerminalText(value) {
  // Z Code may insert SGR colour codes in the middle of the resume line.
  return value
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
}
child.onData((data) => {
  process.stdout.write(data);
  recentOutput = (recentOutput + data).slice(-20000);
  if (!resumeSent && recentOutput.includes("\x1b[?1049h")) {
    resumeSent = true;
    debug("alternate-screen detected");
    sendTimer = setTimeout(() => child.write(`/resume ${sessionId}\r`), 2000);
  }
  const plain = plainTerminalText(recentOutput);
  if (!ready && plain.includes("Resumed session")) {
    debug(`resume text: ${JSON.stringify(plain.slice(plain.indexOf("Resumed session"), plain.indexOf("Resumed session") + 110))}`);
  }
  if (!ready && (plain.includes(readyPhrase) || plain.includes(renderedReadyPhrase))) {
    // The slash command leaves its text in Z Code's editor. Clear that
    // editor before forwarding the first student answer.
    if (!clearTimer) {
      clearTimer = setTimeout(() => {
        child.write("\x01\x0b"); // Home, then delete to end of input.
        ready = true;
        debug("ready after input clear");
      }, 800);
    }
    recentOutput = "";
  }
});

if (process.stdin.isTTY) process.stdin.setRawMode(true);
process.stdin.resume();
process.stdin.on("data", (data) => {
  debug(`input length=${data.length} ready=${ready}`);
  if (ready) child.write(data.toString());
  else if (data.length === 1 && data[0] === 3) child.kill();
});
process.stdout.on("resize", () => {
  child.resize(process.stdout.columns || 120, process.stdout.rows || 30);
});
child.onExit(({ exitCode }) => {
  clearTimeout(sendTimer);
  clearTimeout(clearTimer);
  clearTimeout(fallbackTimer);
  if (!ready) process.stderr.write("\nZ Code 未完成课程会话恢复。\n");
  process.exit(exitCode || (ready ? 0 : 1));
});
