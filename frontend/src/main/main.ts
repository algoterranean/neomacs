// The Electron main process: one engine per app instance, one connection per window.
// It also holds what a sandboxed page may not: the clipboard, native menus and
// dialogs, and the files behind image resources (FRONTEND 3, 8.4, 9, 10).

import { app, BrowserWindow, clipboard, dialog, ipcMain, type Menu, net as electronNet, protocol, type WebContents } from "electron";
import { existsSync, mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { Control, type Engine, LineSocket, startEngine } from "./engine.ts";
import { type EngineMenuItem, install as installMenu } from "./menu.ts";
import { runSmoke } from "./smoke.ts";

const SCHEME = "neomacs-app";
const RESOURCES = "neomacs-resource";
const smoke = process.argv.includes("--smoke");

// The smoke test's engines keep to directories of their own: they neither load
// the configuration of whoever runs the test nor write among their files.
function engineEnvironment(): Record<string, string> {
  if (!smoke) return {};
  const root = mkdtempSync(path.join(os.tmpdir(), "neomacs-smoke-"));
  return {
    NEOMACS_CONFIG_DIR: path.join(root, "config"),
    NEOMACS_DATA_DIR: path.join(root, "data"),
    NEOMACS_CACHE_DIR: path.join(root, "cache"),
  };
}
const engineEnv = engineEnvironment();

export interface WindowState {
  window: BrowserWindow;
  socket: LineSocket | null;
  frame: number | null;
  token: string | null;
  file: string | null; // to visit once the frame exists
  attachedWaiters: (() => void)[];
  // Files the engine has defined as resources for this window, by id.  Only
  // these are served to the page (FRONTEND 9).
  resources: Map<number, string>;
  menu: EngineMenuItem[];
}

/** What the smoke test may look at and poke. */
export interface Shell {
  windows: Map<number, WindowState>;
  engine: () => Engine | null;
  control: () => Control | null;
  appMenu: () => Menu | null;
  /** How to answer the "engine has stopped" dialog: true starts a new one. */
  answerEngineGone: ((detail: string) => boolean) | null;
  engineRestarts: number;
  lastEngineGoneDetail: string;
}

let engine: Engine | null = null;
let control: Control | null = null;
let quitting = false;
let restarting = false;
let appMenu: Menu | null = null;
const windows = new Map<number, WindowState>();
const shell: Shell = {
  windows,
  engine: () => engine,
  control: () => control,
  appMenu: () => appMenu,
  answerEngineGone: null,
  engineRestarts: 0,
  lastEngineGoneDetail: "",
};

protocol.registerSchemesAsPrivileged([
  { scheme: SCHEME, privileges: { standard: true, secure: true, supportFetchAPI: true } },
  { scheme: RESOURCES, privileges: { standard: true, secure: true } },
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

/**
 * Lines go to the page unparsed, with one exception: a resource definition
 * names a file, and which files the page may load is decided here, from what
 * the engine said, never from anything the page says.
 */
function noteResources(state: WindowState, line: string): void {
  if (!line.includes('"resource_define"') && !line.includes('"reset"')) return;
  let message: { method?: string; params?: unknown[] };
  try {
    message = JSON.parse(line) as typeof message;
  } catch {
    return;
  }
  if (message.method !== "redraw" || !Array.isArray(message.params?.[0])) return;
  for (const event of message.params[0] as unknown[][]) {
    if (event[0] === "reset") state.resources.clear();
    else if (event[0] === "resource_define") {
      const source = event[3] as { path?: unknown } | undefined;
      if (typeof event[1] === "number" && typeof source?.path === "string") state.resources.set(event[1], source.path);
    }
  }
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
      noteResources(state, line);
      if (!state.window.isDestroyed()) contents.send("neomacs:line", line);
    };
    socket.onClose = () => {
      if (quitting || restarting || state.window.isDestroyed() || state.socket !== socket) return;
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
  const state: WindowState = {
    window, socket: null, frame: null, token: null, file, attachedWaiters: [], resources: new Map(), menu: [],
  };
  const id = window.webContents.id;
  windows.set(id, state);
  // The page shows only what the engine sends: it never navigates or opens windows.
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.on("focus", () => showMenuOf(state));
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

function showMenuOf(state: WindowState): void {
  if (state.window.isDestroyed()) return;
  appMenu = installMenu(state.window, state.menu, (command) => {
    if (!state.window.isDestroyed()) state.window.webContents.send("neomacs:menu-command", command);
  });
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
    void control.eval(`ed.frames[${frame}].window.show(ed.buffers.visit(${JSON.stringify(file)}))`).catch(() => {});
  }
  for (const waiter of state.attachedWaiters.splice(0)) waiter();
});

// The clipboard.  On Linux "primary" is the selection; elsewhere there is one clipboard.
const clipboardType = (which: string) => (which === "primary" && process.platform === "linux" ? "selection" : "clipboard");
// The selection type exists on Linux only, and this version's declarations leave it out.
const board = clipboard as unknown as {
  readText(type?: string): string | Promise<string>;
  writeText(text: string, type?: string): void | Promise<void>;
};
ipcMain.handle("neomacs:clipboard-read", async (event, which: string) => (stateOf(event.sender) ? await board.readText(clipboardType(which)) : ""));
ipcMain.on("neomacs:clipboard-write", (event, which: string, text: string) => {
  if (stateOf(event.sender) && typeof text === "string") void board.writeText(text, clipboardType(which));
});

ipcMain.on("neomacs:menu", (event, items: EngineMenuItem[]) => {
  const state = stateOf(event.sender);
  if (!state || !Array.isArray(items)) return;
  state.menu = items;
  if (smoke || state.window.isFocused() || windows.size === 1) showMenuOf(state);
});

ipcMain.on("neomacs:open-frame", (event) => {
  if (stateOf(event.sender)) createWindow(null);
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

/** Start an engine and watch it.  When it stops unasked, say so and offer another. */
async function launchEngine(): Promise<void> {
  const started = await startEngine(pythonPath(), repoRoot(), engineEnv);
  engine = started;
  control = new Control(started.socketPath);
  started.process.on("exit", (code, signal) => {
    if (quitting || engine !== started) return;
    void engineGone(started, code, signal);
  });
}

async function engineGone(gone: Engine, code: number | null, signal: string | null): Promise<void> {
  restarting = true;
  const tail = gone.log().trim().split("\n").slice(-25).join("\n");
  const detail = `The engine stopped (${signal ?? `exit code ${String(code)}`}).\n\n${tail || "It wrote nothing before stopping."}`;
  shell.lastEngineGoneDetail = detail;
  for (const state of windows.values()) {
    if (!state.window.isDestroyed()) state.window.webContents.send("neomacs:status", "The engine has stopped.");
  }
  let again: boolean;
  if (shell.answerEngineGone) {
    again = shell.answerEngineGone(detail);
  } else {
    const choice = await dialog.showMessageBox({
      type: "error",
      message: "The Neomacs engine has stopped",
      // A new engine has none of the old one's state.
      detail: `${detail}\n\nA new engine starts empty: buffers that were not saved are lost.`,
      buttons: ["Start a New Engine", "Quit"],
      defaultId: 0,
      cancelId: 1,
    });
    again = choice.response === 0;
  }
  if (!again) {
    app.quit();
    return;
  }
  try {
    await launchEngine();
  } catch (error) {
    dialog.showErrorBox("Neomacs could not start its engine", String(error));
    app.exit(1);
    return;
  }
  shell.engineRestarts += 1;
  restarting = false;
  for (const state of windows.values()) {
    // The frames went with the old engine: attach afresh.
    state.socket?.destroy();
    state.socket = null;
    state.frame = null;
    state.token = null;
    state.resources.clear();
    connect(state);
  }
}

async function main(): Promise<void> {
  await app.whenReady();
  const rendererDir = path.join(__dirname, "renderer");
  protocol.handle(SCHEME, (request) => {
    const url = new URL(request.url);
    const target = path.normalize(path.join(rendererDir, decodeURIComponent(url.pathname)));
    if (!target.startsWith(rendererDir + path.sep)) return new Response("not found", { status: 404 });
    return electronNet.fetch(pathToFileURL(target).toString());
  });
  // neomacs-resource://frame-<n>/<id>: a file the engine defined for that frame, and nothing else.
  protocol.handle(RESOURCES, (request) => {
    const url = new URL(request.url);
    // The host is "frame-N": a bare number would be read as an address.
    const named = /^frame-(\d+)$/.exec(url.hostname);
    const frame = named ? Number(named[1]) : -1;
    const id = Number(url.pathname.replace(/^\//, ""));
    const state = [...windows.values()].find((w) => w.frame === frame);
    const file = Number.isInteger(id) ? state?.resources.get(id) : undefined;
    if (!file) return new Response("not found", { status: 404 });
    return electronNet.fetch(pathToFileURL(file).toString());
  });

  try {
    await launchEngine();
  } catch (error) {
    console.error(String(error));
    app.exit(1);
    return;
  }

  const file = process.argv.slice(app.isPackaged ? 1 : 2).find((arg) => !arg.startsWith("-")) ?? null;
  const state = createWindow(file !== null ? path.resolve(file) : null);
  if (smoke) {
    const code = await runSmoke(state, control as Control, whenAttached, shell);
    quitting = true;
    control?.notify("engine.exit");
    setTimeout(() => {
      engine?.process.kill();
      app.exit(code);
    }, 200);
  }
}

void main();
