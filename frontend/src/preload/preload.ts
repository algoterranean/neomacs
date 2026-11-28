// The only bridge between the sandboxed page and the main process (FRONTEND 3).

import { contextBridge, ipcRenderer, webUtils } from "electron";

contextBridge.exposeInMainWorld("neomacsHost", {
  send: (line: string) => ipcRenderer.send("neomacs:line", line),
  onLine: (callback: (line: string) => void) => {
    ipcRenderer.on("neomacs:line", (_event, line: string) => callback(line));
  },
  onConnect: (callback: (state: unknown) => void) => {
    ipcRenderer.on("neomacs:connect", (_event, state: unknown) => callback(state));
  },
  onStatus: (callback: (text: string) => void) => {
    ipcRenderer.on("neomacs:status", (_event, text: string) => callback(text));
  },
  attached: (frame: number, token: string) => ipcRenderer.send("neomacs:attached", frame, token),
  ready: () => ipcRenderer.send("neomacs:ready"),

  // What a sandboxed page may not do itself (FRONTEND 8.4, 10).
  clipboardRead: (which: string) => ipcRenderer.invoke("neomacs:clipboard-read", which) as Promise<string>,
  clipboardWrite: (which: string, text: string) => ipcRenderer.send("neomacs:clipboard-write", which, text),
  setMenu: (items: unknown) => ipcRenderer.send("neomacs:menu", items),
  onMenuCommand: (callback: (command: string) => void) => {
    ipcRenderer.on("neomacs:menu-command", (_event, command: string) => callback(command));
  },
  openFrame: (options: unknown) => ipcRenderer.send("neomacs:open-frame", options),
  // The workbench's needs of the system (WORKSPACE 5): a directory dialog,
  // context menus, and the file manager.
  chooseDirectory: () => ipcRenderer.invoke("neomacs:choose-directory") as Promise<string | null>,
  contextMenu: (items: { id: string; label: string }[]) => ipcRenderer.invoke("neomacs:context-menu", items) as Promise<string | null>,
  reveal: (path: string) => ipcRenderer.send("neomacs:reveal", path),
  platform: process.platform,
  /** The file-system paths of files dropped on the page. */
  pathsOf: (files: File[]) => files.map((file) => webUtils.getPathForFile(file)).filter((p) => p.length > 0),
});
