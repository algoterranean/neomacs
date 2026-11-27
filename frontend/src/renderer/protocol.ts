// JSON-RPC 2.0 over lines of text (ENGINE 4.1).  Either side may send requests.

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export class RemoteError extends Error {
  code: number;
  data: Json;
  constructor(error: { code?: number; message?: string; data?: Json }) {
    super(error.message ?? "error");
    this.code = error.code ?? 0;
    this.data = error.data ?? null;
  }
}

type Pending = { resolve: (value: Json) => void; reject: (error: Error) => void };
type RequestHandler = (...params: Json[]) => Json | Promise<Json>;
type NotificationHandler = (...params: Json[]) => void;

export class Rpc {
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private requests = new Map<string, RequestHandler>();
  private notifications = new Map<string, NotificationHandler>();
  private send: (line: string) => void;

  constructor(send: (line: string) => void) {
    this.send = send;
  }

  call(method: string, ...params: Json[]): Promise<Json> {
    const id = this.nextId++;
    const promise = new Promise<Json>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    return promise;
  }

  notify(method: string, ...params: Json[]): void {
    this.send(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  /** Answer requests the engine sends, such as `layout.move`. */
  onRequest(method: string, handler: RequestHandler): void {
    this.requests.set(method, handler);
  }

  onNotification(method: string, handler: NotificationHandler): void {
    this.notifications.set(method, handler);
  }

  /** Fail everything that is waiting: the connection it was sent on has gone. */
  abandon(reason: string): void {
    for (const { reject } of this.pending.values()) reject(new Error(reason));
    this.pending.clear();
  }

  handleLine(line: string): void {
    let msg: { id?: number; method?: string; params?: Json[]; result?: Json; error?: object };
    try {
      msg = JSON.parse(line);
    } catch {
      return; // not ours to repair
    }
    if (typeof msg.method === "string") {
      const params = msg.params ?? [];
      if (msg.id === undefined) {
        this.notifications.get(msg.method)?.(...params);
      } else {
        void this.answer(msg.id, msg.method, params);
      }
    } else if (typeof msg.id === "number") {
      const waiting = this.pending.get(msg.id);
      if (!waiting) return; // a reply to a request made before a reload
      this.pending.delete(msg.id);
      if (msg.error) waiting.reject(new RemoteError(msg.error));
      else waiting.resolve(msg.result ?? null);
    }
  }

  private async answer(id: number, method: string, params: Json[]): Promise<void> {
    const handler = this.requests.get(method);
    if (!handler) {
      this.reply(id, { error: { code: -32601, message: `method not found: ${method}` } });
      return;
    }
    try {
      this.reply(id, { result: await handler(...params) });
    } catch (error) {
      this.reply(id, { error: { code: -32000, message: String(error) } });
    }
  }

  private reply(id: number, body: object): void {
    this.send(JSON.stringify({ jsonrpc: "2.0", id, ...body }));
  }
}
