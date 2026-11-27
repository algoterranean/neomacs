// The Electron main process: one engine per app instance, one connection per window.

import { app, BrowserWindow, ipcMain, net as electronNet, protocol, type WebContents } from "electron";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { Control, type Engine, LineSocket, startEngine } from "./engine.ts";
import { runSmoke } from "./smoke.ts";

const SCHEME = "neomacs-app";
const smoke = process.argv.includes("--smoke");

interface WindowState {
  window: BrowserWindow;
  socket: LineSocket | null;
  frame: number | null;
  token: string | null;
  file: string | null; // to visit once the frame exists
  attachedWaiters: (() => void)[];
}

let engine: Engine | null = null;
let control: Control | null = null;
let quitting = false;
const windows = new Map<number, WindowState>();

protocol.registerSchemesAsPrivileged([
  { scheme: SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true } },
]);

function repoRoot(): string {
  return path.resolve(app.getAppPath(), "..");
}

function pythonPath(): string {
  const fromEnv = process.env["NEOMACS_PYTHON"];
  if (fromEnv) return fromEnv;
  const venv = path.join(repoRoot(), ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}

function stateOf(contents: WebContents): WindowState | undefined {
  return windows.get(contents.id);
}

/** Open this window's connection to the engine and tell the page how to attach. */
function connect(state: WindowState): void {
  if (!engine || state.window.isDestroyed()) return;
  const contents = state.window.webContents;
  const live = state.socket !== null && !state.socket.closed;
  if (!live) {
    const socket = new LineSocket(engine.socketPath);
    state.socket = socket;
    socket.onLine = (line) => {
      if (!state.window.isDestroyed()) contents.send("neomacs:line", line);
    };
    socket.onClose = () => {
      if (quitting || state.window.isDestroyed() || state.socket !== socket) return;
      // The connection dropped.  Come back and reattach to the same frame.
      setTimeout(() => connect(state), 200);
    };
  }
  contents.send("neomacs:connect", { live: live && state.frame !== null, frame: state.frame, token: state.token });
}

function createWindow(file: string | null): WindowState {
  const window = new BrowserWindow({
    width: 1000,
    height: 700,
    show: !smoke,
    backgroundColor: "#1e1e1e",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
      offscreen: smoke,
      backgroundThrottling: !smoke,
    },
  });
  const state: WindowState = { window, socket: null, frame: null, token: null, file, attachedWaiters: [] };
  const id = window.webContents.id;
  windows.set(id, state);
  // The page shows only what the engine sends: it never navigates or opens windows.
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.on("closed", () => {
    windows.delete(id);
    const socket = state.socket;
    state.socket = null;
    socket?.write(JSON.stringify({ jsonrpc: "2.0", method: "ui.detach", params: [] }));
    setTimeout(() => socket?.destroy(), 100);
  });
  void window.loadURL(`${SCHEME}://app/index.html`);
  return state;
}

ipcMain.on("neomacs:ready", (event) => {
  const state = stateOf(event.sender);
  if (state) connect(state);
});

ipcMain.on("neomacs:line", (event, line: string) => {
  stateOf(event.sender)?.socket?.write(line);
});

ipcMain.on("neomacs:attached", (event, frame: number, token: string) => {
  const state = stateOf(event.sender);
  if (!state) return;
  state.frame = frame;
  state.token = token;
  if (state.file !== null && control) {
    const file = state.file;
    state.file = null;
    // A JSON string is also a Python string literal.
    void control.eval(`ed.frames[${frame}].window.show(ed.buffers.visit(${JSON.stringify(file)}))`);
  }
  for (const waiter of state.attachedWaiters.splice(0)) waiter();
});

export function whenAttached(state: WindowState): Promise<void> {
  if (state.frame !== null) return Promise.resolve();
  return new Promise((resolve) => state.attachedWaiters.push(resolve));
}

app.on("window-all-closed", () => app.quit());

app.on("before-quit", () => {
  quitting = true;
  control?.notify("engine.exit");
  const child = engine?.process;
  setTimeout(() => child?.kill(), 500);
});

async function main(): Promise<void> {
  await app.whenReady();
  const rendererDir = path.join(__dirname, "renderer");
  protocol.handle(SCHEME, (request) => {
    const url = new URL(request.url);
    const target = path.normalize(path.join(rendererDir, decodeURIComponent(url.pathname)));
    if (!target.startsWith(rendererDir + path.sep)) return new Response("not found", { status: 404 });
    return electronNet.fetch(pathToFileURL(target).toString());
  });

  try {
    engine = await startEngine(pythonPath(), repoRoot());
  } catch (error) {
    console.error(String(error));
    app.exit(1);
    return;
  }
  control = new Control(engine.socketPath);
  engine.process.on("exit", () => {
    if (quitting) return;
    for (const state of windows.values()) {
      if (!state.window.isDestroyed()) {
        state.window.webContents.send("neomacs:status", "The engine has stopped.");
      }
    }
  });

  const file = process.argv.slice(app.isPackaged ? 1 : 2).find((arg) => !arg.startsWith("-")) ?? null;
  const state = createWindow(file !== null ? path.resolve(file) : null);
  if (smoke) {
    const code = await runSmoke(state, control, whenAttached);
    quitting = true;
    control.notify("engine.exit");
    setTimeout(() => {
      engine?.process.kill();
      app.exit(code);
    }, 200);
  }
}

void main();
