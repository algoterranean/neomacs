// The frame around the views (FRONTEND 5.6): the prompt palette, the completion
// popup, and the message strip.  Everything shown is text set with textContent.

export class Chrome {
  /** Where the main view goes. */
  readonly main: HTMLDivElement;
  private palette: HTMLDivElement;
  private label: HTMLSpanElement;
  private promptSlot: HTMLDivElement;
  private popup: HTMLDivElement;
  private strip: HTMLDivElement;
  private messageEl: HTMLSpanElement;
  private keysEl: HTMLSpanElement;
  private toasts: HTMLDivElement;
  private popupKey = "";
  private searchBox: HTMLDivElement;
  private searchLabel: HTMLSpanElement;
  private searchSlot: HTMLDivElement;

  constructor(root: HTMLElement) {
    const div = (className: string) => {
      const el = document.createElement("div");
      el.className = className;
      return el;
    };
    this.main = div("nm-main");
    this.palette = div("nm-palette");
    this.palette.hidden = true;
    const row = div("nm-palette-row");
    this.label = document.createElement("span");
    this.label.className = "nm-palette-label";
    this.promptSlot = div("nm-palette-input");
    row.append(this.label, this.promptSlot);
    this.popup = div("nm-popup");
    this.popup.hidden = true;
    this.palette.append(row, this.popup);
    this.strip = div("nm-strip");
    this.messageEl = document.createElement("span");
    this.messageEl.className = "nm-message";
    this.keysEl = document.createElement("span");
    this.keysEl.className = "nm-keys";
    this.strip.append(this.messageEl, this.keysEl);
    this.toasts = div("nm-toasts");
    // The search box: a small window over the top right corner of the main area.
    this.searchBox = div("nm-searchbox");
    this.searchBox.hidden = true;
    this.searchLabel = document.createElement("span");
    this.searchLabel.className = "nm-searchbox-label";
    this.searchSlot = div("nm-searchbox-input");
    this.searchBox.append(this.searchLabel, this.searchSlot);
    this.main.append(this.palette, this.searchBox, this.toasts);
    root.replaceChildren(this.main, this.strip);
  }

  /** Show a prompt with this label around the given minibuffer view, or hide it. */
  setPrompt(label: string | null, view: HTMLElement | null): void {
    const shown = label !== null && view !== null;
    this.palette.hidden = !shown;
    if (!shown) {
      this.promptSlot.replaceChildren();
      return;
    }
    if (this.label.textContent !== label) this.label.textContent = label;
    if (this.promptSlot.firstChild !== view) this.promptSlot.replaceChildren(view);
  }

  /** Show the search box with this label around the given minibuffer view, or hide it. */
  setSearch(label: string | null, view: HTMLElement | null): void {
    const shown = label !== null && view !== null;
    this.searchBox.hidden = !shown;
    if (!shown) {
      this.searchSlot.replaceChildren();
      return;
    }
    if (this.searchLabel.textContent !== label) this.searchLabel.textContent = label;
    this.searchBox.classList.toggle("nm-searchbox-failing", label.startsWith("No match"));
    if (this.searchSlot.firstChild !== view) this.searchSlot.replaceChildren(view);
  }

  get promptShown(): boolean {
    return !this.palette.hidden;
  }

  /** The completion candidates under the prompt.  `null` hides them. */
  setPopup(items: [string, string][] | null, selected: number): void {
    const key = items === null ? "" : JSON.stringify([items, selected]);
    if (key === this.popupKey) return;
    this.popupKey = key;
    this.popup.hidden = items === null || items.length === 0;
    if (!items) {
      this.popup.replaceChildren();
      return;
    }
    const rows = items.map(([text, annotation], index) => {
      const row = document.createElement("div");
      row.className = index === selected ? "nm-popup-item nm-selected" : "nm-popup-item";
      const name = document.createElement("span");
      name.className = "nm-popup-text";
      name.textContent = text;
      const note = document.createElement("span");
      note.className = "nm-popup-note";
      note.textContent = annotation;
      row.append(name, note);
      return row;
    });
    this.popup.replaceChildren(...rows);
    this.popup.querySelector(".nm-selected")?.scrollIntoView({ block: "nearest" });
  }

  /** The latest message.  Warnings and errors also appear as a toast for a while. */
  setMessage(text: string, kind: string): void {
    this.messageEl.textContent = text;
    this.messageEl.dataset["kind"] = kind;
    if (kind === "warning" || kind === "error") {
      const toast = document.createElement("div");
      toast.className = `nm-toast nm-toast-${kind}`;
      toast.textContent = text;
      this.toasts.append(toast);
      setTimeout(() => toast.remove(), 6000);
      while (this.toasts.childElementCount > 4) this.toasts.firstElementChild?.remove();
    }
  }

  /** A pending key sequence or prefix argument. */
  setKeys(text: string): void {
    if (this.keysEl.textContent !== text) this.keysEl.textContent = text;
  }

  snapshot(): Record<string, unknown> {
    return {
      prompt: this.palette.hidden ? null : this.label.textContent,
      promptText: this.palette.hidden ? null : (this.promptSlot.querySelector(".nm-lines")?.textContent ?? ""),
      popup: this.popup.hidden ? null : [...this.popup.querySelectorAll(".nm-popup-text")].map((el) => el.textContent),
      popupSelected: this.popup.querySelector(".nm-selected .nm-popup-text")?.textContent ?? null,
      message: this.messageEl.textContent,
      messageKind: this.messageEl.dataset["kind"] ?? null,
      keys: this.keysEl.textContent,
      toasts: this.toasts.childElementCount,
      search: this.searchBox.hidden
        ? null
        : {
            label: this.searchLabel.textContent,
            text: this.searchSlot.querySelector(".nm-content")?.textContent ?? "",
            rect: (() => {
              const r = this.searchBox.getBoundingClientRect();
              return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
            })(),
          },
    };
  }
}
