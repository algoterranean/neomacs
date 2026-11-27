// The only bridge between the sandboxed page and the main process (FRONTEND 3).

import { contextBridge, ipcRenderer } from "electron";

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
});
