// Keyboard and input-method input (FRONTEND 8.1, 8.2).
//
// A key press becomes one `input.key`.  Text an input method composes is drawn
// locally and sent as `input.text` only when it is committed.  The document is
// never contenteditable: an EditContext receives the text.

import { translateKey } from "./keys.ts";
import type { Rpc } from "./protocol.ts";
import type { ViewElement } from "./view.ts";

// Minimal declarations for the EditContext API, in case the DOM library in use lacks them.
interface TextUpdateEventLike extends Event {
  text: string;
}
interface CharacterBoundsUpdateEventLike extends Event {
  rangeStart: number;
  rangeEnd: number;
}
interface EditContextLike extends EventTarget {
  text: string;
  updateText(start: number, end: number, text: string): void;
  updateSelection(start: number, end: number): void;
  updateControlBounds(bounds: DOMRect): void;
  updateSelectionBounds(bounds: DOMRect): void;
  updateCharacterBounds(start: number, bounds: DOMRect[]): void;
}

export interface InputOptions {
  mac: boolean;
  optionIsMeta: boolean;
}

export class Input {
  private rpc: Rpc;
  private target: HTMLElement;
  private focusedView: () => ViewElement | null;
  private context: EditContextLike | null = null;
  private composing = false;
  keysSent = 0;
  textsSent = 0;

  constructor(target: HTMLElement, rpc: Rpc, options: InputOptions, focusedView: () => ViewElement | null) {
    this.rpc = rpc;
    this.target = target;
    this.focusedView = focusedView;
    target.tabIndex = 0;
    window.addEventListener(
      "keydown",
      (event) => {
        const key = translateKey(event, options);
        if (key === null) return; // a bare modifier, or the input method's business
        event.preventDefault();
        event.stopPropagation();
        const view = this.focusedView();
        if (view) view.repeating = event.repeat;
        this.keysSent += 1;
        rpc.notify("input.key", key);
        if (key === "C-g") rpc.notify("input.interrupt");
      },
      true,
    );
    window.addEventListener("keyup", () => {
      const view = this.focusedView();
      if (view) view.repeating = false;
    });
    this.attachEditContext();
    window.addEventListener("focus", () => {
      target.focus({ preventScroll: true });
      rpc.notify("input.focus", true);
    });
    window.addEventListener("blur", () => rpc.notify("input.focus", false));
    target.focus({ preventScroll: true });
  }

  private attachEditContext(): void {
    const ctor = (window as unknown as { EditContext?: new () => EditContextLike }).EditContext;
    if (!ctor) return;
    const context = new ctor();
    this.context = context;
    (this.target as unknown as { editContext: EditContextLike }).editContext = context;
    context.addEventListener("compositionstart", () => {
      this.composing = true;
    });
    context.addEventListener("textupdate", (event) => {
      const text = (event as TextUpdateEventLike).text;
      if (this.composing) {
        // Still being composed: drawn here, and the engine is told nothing.
        this.focusedView()?.setPreedit(context.text);
      } else {
        this.commit(text);
      }
    });
    context.addEventListener("compositionend", () => {
      this.composing = false;
      const text = context.text;
      this.focusedView()?.setPreedit("");
      this.commit(text);
    });
    context.addEventListener("characterboundsupdate", (event) => {
      const rect = this.focusedView()?.cursorRect();
      const update = event as CharacterBoundsUpdateEventLike;
      if (rect) {
        const count = Math.max(1, update.rangeEnd - update.rangeStart);
        context.updateCharacterBounds(update.rangeStart, new Array<DOMRect>(count).fill(rect));
      }
    });
  }

  private commit(text: string): void {
    const context = this.context;
    if (context) {
      context.updateText(0, context.text.length, "");
      context.updateSelection(0, 0);
    }
    if (text.length > 0) {
      this.textsSent += 1;
      this.rpc.notify("input.text", text);
    }
  }

  /** After a render: tell the input method where the cursor is, for its candidate window. */
  cursorMoved(): void {
    const context = this.context;
    const rect = this.focusedView()?.cursorRect();
    if (!context || !rect) return;
    context.updateControlBounds(this.target.getBoundingClientRect());
    context.updateSelectionBounds(rect);
  }

  get hasEditContext(): boolean {
    return this.context !== null;
  }
}
