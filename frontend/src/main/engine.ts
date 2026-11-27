// Starting the engine and talking to it from the main process (FRONTEND 3, DISTRIBUTION 7).

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const MAX_SOCKET_PATH = 100;

/** A short, private path for this app instance's engine socket. */
export function socketPath(): string {
  const uid = typeof process.getuid === "function" ? process.getuid() : 0;
  const candidates = [path.join(os.tmpdir(), `neomacs-${uid}`), `/tmp/neomacs-${uid}`];
  for (const dir of candidates) {
    const candidate = path.join(dir, `app-${process.pid}.sock`);
    if (Buffer.byteLength(candidate) > MAX_SOCKET_PATH) continue;
    try {
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      return candidate;
    } catch {
      continue;
    }
  }
  throw new Error("no usable directory for the engine socket");
}

export interface Engine {
  process: ChildProcess;
  socketPath: string;
}

/** Start the engine as a child and resolve once it is listening. */
export function startEngine(python: string, cwd: string): Promise<Engine> {
  const sock = socketPath();
  const child = spawn(python, ["-m", "neomacs", "--listen", sock, "--exit-with-parent"], {
    cwd,
    stdio: ["ignore", "pipe", "pipe"],
  });
  return new Promise((resolve, reject) => {
    let out = "";
    let err = "";
    child.stdout?.on("data", (chunk: Buffer) => {
      out += chunk.toString();
      if (out.includes("NEOMACS-LISTENING")) resolve({ process: child, socketPath: sock });
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      err = (err + chunk.toString()).slice(-4000);
    });
    child.on("error", reject);
    child.on("exit", (code) => reject(new Error(`the engine exited (${code}) before listening\n${err}`)));
  });
}

/** A socket carrying newline-delimited messages. */
export class LineSocket {
  private socket: net.Socket;
  private buffer = "";
  closed = false;
  onLine: (line: string) => void = () => {};
  onClose: () => void = () => {};

  constructor(sock: string) {
    this.socket = net.createConnection(sock);
    this.socket.setEncoding("utf8");
    this.socket.on("data", (chunk: string) => {
      this.buffer += chunk;
      let newline: number;
      while ((newline = this.buffer.indexOf("\n")) >= 0) {
        const line = this.buffer.slice(0, newline);
        this.buffer = this.buffer.slice(newline + 1);
        if (line.length > 0) this.onLine(line);
      }
    });
    const done = () => {
      if (this.closed) return;
      this.closed = true;
      this.onClose();
    };
    this.socket.on("close", done);
    this.socket.on("error", done);
  }

  write(line: string): void {
    if (!this.closed) this.socket.write(line + "\n");
  }

  destroy(): void {
    this.socket.destroy();
  }
}

/** The main process's own connection, for the few things it asks the engine itself. */
export class Control {
  private socket: LineSocket;
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();

  constructor(sock: string) {
    this.socket = new LineSocket(sock);
    this.socket.onLine = (line) => {
      const msg = JSON.parse(line) as { id?: number; result?: unknown; error?: { message?: string } };
      if (typeof msg.id !== "number") return;
      const waiting = this.pending.get(msg.id);
      if (!waiting) return;
      this.pending.delete(msg.id);
      if (msg.error) waiting.reject(new Error(msg.error.message ?? "error"));
      else waiting.resolve(msg.result);
    };
    this.socket.onClose = () => {
      for (const { reject } of this.pending.values()) reject(new Error("the engine connection closed"));
      this.pending.clear();
    };
  }

  call(method: string, ...params: unknown[]): Promise<unknown> {
    const id = this.nextId++;
    const promise = new Promise<unknown>((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.socket.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    return promise;
  }

  notify(method: string, ...params: unknown[]): void {
    this.socket.write(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  eval(source: string): Promise<unknown> {
    return this.call("engine.eval", source);
  }
}
