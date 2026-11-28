// The file tree (WORKSPACE.md section 5.4): the upper half of the details
// sidebar.  A directory of the current project is read from the engine when
// it is opened, and again whenever the engine says listings may be stale.
// Everything a row does is a command sent to the engine.

import type { WorkspaceInfo } from "./model.ts";

export interface FileEntry {
  name: string;
  kind: "directory" | "file";
  /** "conflicted", "modified", "staged", "untracked", or null. */
  git: string | null;
  ignored: boolean;
}

export interface FileTreeHost {
  /** The entries of a directory of a project, relative to its root ("" is the root). */
  list(workspace: number, directory: string): Promise<FileEntry[]>;
  command(name: string, args?: (string | number | boolean | null)[]): void;
  contextMenu(items: { id: string; label: string }[]): Promise<string | null>;
  reveal(path: string): void;
  /** Tell the engine which directories of a project are open, so that it can keep them. */
  folders(workspace: number, open: string[]): void;
}

interface Row {
  path: string; // relative to the root, "/" between its parts
  entry: FileEntry;
  depth: number;
}

const GIT_MARKS: Record<string, string> = { conflicted: "!", modified: "M", staged: "S", untracked: "U" };
const TYPE_AHEAD_MS = 700;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string): HTMLElementTagNameMap[K] {
  const made = document.createElement(tag);
  made.className = className;
  return made;
}

export class FileTree {
  /** The buttons that go beside the pane's heading. */
  readonly tools: HTMLDivElement;
  /** The tree itself, which fills the pane's body. */
  readonly element: HTMLDivElement;
  /** Whether the keyboard is the tree's: arrows move through it instead of the text. */
  active = false;
  private host: FileTreeHost;
  private workspace: WorkspaceInfo | null = null;
  // For the project on show: the listing of each directory read so far.
  private listings = new Map<string, FileEntry[]>();
  // For every project seen: which of its directories are open.
  private expanded = new Map<number, Set<string>>();
  private rows: Row[] = [];
  private rowEls = new Map<string, HTMLDivElement>();
  private selected: string | null = null;
  private epoch = 0; // goes up when the project changes: answers for the old one are dropped
  private typed = "";
  private typedAt = 0;
  /** How many listings have been asked for, for tests. */
  requests = 0;

  constructor(host: FileTreeHost) {
    this.host = host;
    this.tools = el("div", "nm-pane-tools");
    const tool = (label: string, title: string, run: () => void) => {
      const button = el("button", "nm-pane-tool");
      button.type = "button";
      button.textContent = label;
      button.title = title;
      button.setAttribute("aria-label", title);
      button.addEventListener("click", run);
      this.tools.append(button);
    };
    tool("+", "New file", () => this.newIn("files.new_file"));
    tool("▣", "New folder", () => this.newIn("files.new_folder"));
    tool("⟳", "Read the files again", () => host.command("files.refresh"));
    tool("⊟", "Close every folder", () => this.collapseAll());

    this.element = el("div", "nm-file-tree");
    this.element.setAttribute("role", "tree");
    this.element.setAttribute("aria-label", "Files");
    this.element.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const row = (event.target as Element).closest<HTMLElement>(".nm-file");
      this.menu(row ? (this.rows.find((r) => r.path === row.dataset["path"]) ?? null) : null);
    });
    this.render();
  }

  private get open(): Set<string> {
    const id = this.workspace?.id ?? -1;
    let set = this.expanded.get(id);
    if (!set) {
      // First sight of this project: the folders the engine kept for it, if any.
      set = new Set(this.workspace?.id === id ? (this.workspace.folders ?? []) : []);
      this.expanded.set(id, set);
    }
    return set;
  }

  /** The full path of something in the tree, as the engine's commands take it. */
  private absolute(path: string): string {
    const root = this.workspace?.root ?? "";
    if (path === "") return root;
    const separator = root.includes("\\") && !root.includes("/") ? "\\" : "/";
    return root.replace(/[\\/]+$/, "") + separator + path.split("/").join(separator);
  }

  /** The project on show is `workspace`: draw its tree, reading it if it is another's. */
  setWorkspace(workspace: WorkspaceInfo | null): void {
    const same = workspace?.id === this.workspace?.id && workspace?.root === this.workspace?.root;
    this.workspace = workspace;
    if (same) return;
    this.epoch += 1;
    this.listings.clear();
    this.selected = null;
    this.render();
    this.refresh();
  }

  /** Read again every directory that is open. */
  refresh(): void {
    if (!this.workspace || this.workspace.root === null) return;
    this.load("");
    for (const directory of this.open) this.load(directory);
  }

  private load(directory: string): void {
    const workspace = this.workspace;
    if (!workspace || workspace.root === null) return;
    const epoch = this.epoch;
    this.requests += 1;
    this.host.list(workspace.id, directory).then(
      (entries) => {
        if (epoch !== this.epoch) return;
        this.listings.set(directory, entries);
        this.render();
      },
      () => {
        // It has gone, or cannot be read: it is not open any more.
        if (epoch !== this.epoch) return;
        this.listings.delete(directory);
        if (this.open.delete(directory)) this.report();
        this.render();
      },
    );
  }

  private collect(directory: string, depth: number, into: Row[]): void {
    for (const entry of this.listings.get(directory) ?? []) {
      const path = directory ? `${directory}/${entry.name}` : entry.name;
      into.push({ path, entry, depth });
      if (entry.kind === "directory" && this.open.has(path)) this.collect(path, depth + 1, into);
    }
  }

  private render(): void {
    this.rows = [];
    this.rowEls.clear();
    const workspace = this.workspace;
    if (!workspace || workspace.root === null) {
      const hint = el("div", "nm-pane-hint");
      hint.textContent = "Add a project to see its files.";
      this.element.replaceChildren(hint);
      return;
    }
    this.collect("", 0, this.rows);
    if (this.selected !== null && !this.rows.some((row) => row.path === this.selected)) this.selected = null;
    const made = this.rows.map((row) => {
      const { entry, path } = row;
      const directory = entry.kind === "directory";
      const item = el("div", "nm-file" + (directory ? " nm-file-directory" : "") + (entry.ignored ? " nm-file-ignored" : ""));
      if (entry.git) item.classList.add(`nm-git-${entry.git}`);
      item.dataset["path"] = path;
      item.style.paddingLeft = `${0.6 + row.depth * 0.9}em`;
      item.setAttribute("role", "treeitem");
      item.setAttribute("aria-level", String(row.depth + 1));
      if (directory) item.setAttribute("aria-expanded", this.open.has(path) ? "true" : "false");
      item.title = this.absolute(path);
      const twist = el("span", "nm-file-twist");
      twist.textContent = directory ? (this.open.has(path) ? "▾" : "▸") : "";
      const name = el("span", "nm-file-name");
      name.textContent = entry.name; // text, never markup
      const mark = el("span", "nm-file-mark");
      mark.textContent = entry.git ? (directory ? "●" : (GIT_MARKS[entry.git] ?? "")) : "";
      if (entry.git) mark.title = entry.git;
      item.append(twist, name, mark);
      item.addEventListener("click", () => {
        this.select(path, false);
        this.activate(row, false);
      });
      this.rowEls.set(path, item);
      return item;
    });
    this.element.replaceChildren(...made);
    this.markSelected();
  }

  private markSelected(): void {
    for (const [path, item] of this.rowEls) {
      const on = path === this.selected;
      item.classList.toggle("nm-selected", on);
      item.setAttribute("aria-selected", on ? "true" : "false");
    }
    this.element.classList.toggle("nm-keyboard", this.active);
  }

  private select(path: string | null, reveal = true): void {
    this.selected = path;
    this.markSelected();
    if (reveal && path !== null) this.rowEls.get(path)?.scrollIntoView({ block: "nearest" });
  }

  /** Open a file, or open or close a directory.  `stay` keeps the keyboard in the tree. */
  private activate(row: Row, stay: boolean): void {
    if (row.entry.kind === "directory") {
      this.toggle(row.path);
      return;
    }
    if (!stay) this.setActive(false);
    this.host.command("files.find", [this.absolute(row.path)]);
  }

  private toggle(path: string, open?: boolean): void {
    const want = open ?? !this.open.has(path);
    if (want) {
      this.open.add(path);
      this.load(path); // what it holds may have changed since it was last open
    } else {
      // Closing a directory closes those inside it too.
      for (const other of [...this.open]) if (other === path || other.startsWith(`${path}/`)) this.open.delete(other);
    }
    this.report();
    this.render();
  }

  /** The engine keeps which folders are open, for the next time the editor starts. */
  private report(): void {
    const workspace = this.workspace;
    if (workspace && workspace.root !== null) this.host.folders(workspace.id, [...this.open].sort());
  }

  private collapseAll(): void {
    this.open.clear();
    this.report();
    this.render();
  }

  /** The directory a new file goes in: the selected one, or the one the selected file is in. */
  private targetDirectory(row: Row | null): string {
    if (!row) return "";
    if (row.entry.kind === "directory") return row.path;
    return row.path.includes("/") ? row.path.slice(0, row.path.lastIndexOf("/")) : "";
  }

  private newIn(command: string, row: Row | null = this.rows.find((r) => r.path === this.selected) ?? null): void {
    if (!this.workspace || this.workspace.root === null) return;
    const directory = this.targetDirectory(row);
    if (directory && !this.open.has(directory)) {
      this.open.add(directory);
      this.report();
    }
    this.setActive(false); // the name is asked for in the prompt
    this.host.command(command, [this.absolute(directory)]);
  }

  private menu(row: Row | null): void {
    if (!this.workspace || this.workspace.root === null) return;
    if (row) this.select(row.path, false);
    const items = [
      { id: "new_file", label: "New File…" },
      { id: "new_folder", label: "New Folder…" },
      ...(row
        ? [
            { id: "rename", label: "Rename…" },
            { id: "delete", label: "Delete" },
            { id: "copy", label: "Copy Path" },
          ]
        : [{ id: "refresh", label: "Read the Files Again" }]),
      { id: "reveal", label: "Reveal in File Manager" },
    ];
    void this.host.contextMenu(items).then((chosen) => {
      const path = this.absolute(row?.path ?? "");
      if (chosen === "new_file") this.newIn("files.new_file", row);
      else if (chosen === "new_folder") this.newIn("files.new_folder", row);
      else if (chosen === "rename") this.host.command("files.rename", [path]);
      else if (chosen === "delete") this.host.command("files.delete", [path]);
      else if (chosen === "copy") this.host.command("files.copy_path", [path]);
      else if (chosen === "refresh") this.host.command("files.refresh");
      else if (chosen === "reveal") this.host.reveal(path);
    });
  }

  // -- the keyboard -----------------------------------------------------------------

  /** Give the tree the keyboard, or take it away. */
  setActive(active: boolean): void {
    this.active = active && this.workspace !== null && this.workspace.root !== null;
    if (this.active && this.selected === null && this.rows.length > 0) this.selected = (this.rows[0] as Row).path;
    this.markSelected();
    if (this.active && this.selected !== null) this.rowEls.get(this.selected)?.scrollIntoView({ block: "nearest" });
  }

  /**
   * A key pressed while the tree has the keyboard.  True if it was the tree's;
   * false hands it on to the engine, as any key is when the text has it.
   */
  key(event: KeyboardEvent): boolean {
    if (!this.active) return false;
    if (event.ctrlKey || event.metaKey || event.altKey) {
      // A command for the editor: it gets the key, and the keyboard with it.
      this.setActive(false);
      return false;
    }
    const index = this.rows.findIndex((row) => row.path === this.selected);
    const row = index >= 0 ? (this.rows[index] as Row) : null;
    const go = (to: number) => {
      const target = this.rows[Math.max(0, Math.min(this.rows.length - 1, to))];
      if (target) this.select(target.path);
    };
    switch (event.key) {
      case "Escape":
        this.setActive(false);
        return true;
      case "ArrowDown":
        go(index + 1);
        return true;
      case "ArrowUp":
        go(index < 0 ? 0 : index - 1);
        return true;
      case "Home":
        go(0);
        return true;
      case "End":
        go(this.rows.length - 1);
        return true;
      case "ArrowRight":
        if (row?.entry.kind === "directory") {
          if (!this.open.has(row.path)) this.toggle(row.path, true);
          else if (this.rows[index + 1]?.path.startsWith(`${row.path}/`)) go(index + 1);
        }
        return true;
      case "ArrowLeft":
        if (row?.entry.kind === "directory" && this.open.has(row.path)) this.toggle(row.path, false);
        else if (row?.path.includes("/")) this.select(row.path.slice(0, row.path.lastIndexOf("/")));
        return true;
      case "Enter":
        if (row) this.activate(row, false);
        return true;
      default:
        break;
    }
    if ([...event.key].length === 1 && event.key !== " ") {
      // Typing the start of a name goes to the next row that has it.
      const now = performance.now();
      this.typed = now - this.typedAt > TYPE_AHEAD_MS ? event.key : this.typed + event.key;
      this.typedAt = now;
      const want = this.typed.toLowerCase();
      const from = this.typed.length === 1 ? index + 1 : Math.max(0, index);
      for (let step = 0; step < this.rows.length; step++) {
        const candidate = this.rows[(from + step) % this.rows.length] as Row;
        if (candidate.entry.name.toLowerCase().startsWith(want)) {
          this.select(candidate.path);
          break;
        }
      }
      return true;
    }
    return true; // any other bare key is not one for the text while the tree has the keyboard
  }

  /** What is drawn, for tests. */
  snapshot(): Record<string, unknown> {
    return {
      active: this.active,
      requests: this.requests,
      hint: this.element.querySelector(".nm-pane-hint")?.textContent ?? null,
      rows: this.rows.map((row) => {
        const item = this.rowEls.get(row.path) as HTMLDivElement;
        const r = item.getBoundingClientRect();
        return {
          path: row.path,
          kind: row.entry.kind,
          depth: row.depth,
          open: row.entry.kind === "directory" && this.open.has(row.path),
          git: row.entry.git,
          ignored: row.entry.ignored,
          selected: item.classList.contains("nm-selected"),
          color: getComputedStyle(item.querySelector(".nm-file-name") as Element).color,
          rect: [r.left, r.top, r.width, r.height],
        };
      }),
    };
  }
}
