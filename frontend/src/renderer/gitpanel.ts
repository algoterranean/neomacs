// The git panel (WORKSPACE.md section 5.5): the lower half of the details
// sidebar.  What it shows is what the engine last said of the project's
// repository; everything it does is a command sent to the engine, which
// answers with the repository as it then is.

export interface GitRow {
  path: string; // relative to the top of the repository, "/" between its parts
  state: string;
}

export interface GitInfo {
  top: string;
  branch: string | null;
  /** The commit HEAD is at; null before the first. */
  head: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  staged: GitRow[];
  changes: GitRow[];
  untracked: GitRow[];
  conflicted: GitRow[];
}

export interface GitPanelHost {
  command(name: string, args?: (string | number | boolean | null)[]): void;
  /** Give the keyboard back to the text. */
  refocus(): void;
}

type GroupName = "conflicted" | "staged" | "changes" | "untracked";

const GROUPS: [name: GroupName, title: string][] = [
  ["conflicted", "Merge Conflicts"],
  ["staged", "Staged"],
  ["changes", "Changes"],
  ["untracked", "Untracked"],
];
const LETTERS: Record<string, string> = { modified: "M", added: "A", deleted: "D", renamed: "R", copied: "C", untracked: "U", conflicted: "!" };

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string): HTMLElementTagNameMap[K] {
  const made = document.createElement(tag);
  made.className = className;
  return made;
}

export class GitPanel {
  /** What goes beside the pane's heading: the branch, and a button to ask git again. */
  readonly tools: HTMLDivElement;
  /** The panel itself, which fills the pane's body. */
  readonly element: HTMLDivElement;
  private host: GitPanelHost;
  private branch: HTMLSpanElement;
  private box: HTMLDivElement;
  private message: HTMLTextAreaElement;
  private commitButton: HTMLButtonElement;
  private list: HTMLDivElement;
  private hint: HTMLDivElement;
  private info: GitInfo | null = null;
  private drawn = "";
  // A commit was asked for, of this repository when HEAD was here: the
  // message goes when HEAD has moved, which is when it has been made.
  private committing: { top: string; head: string | null } | null = null;

  constructor(host: GitPanelHost) {
    this.host = host;
    this.tools = el("div", "nm-pane-tools");
    this.branch = el("span", "nm-git-branch");
    const refresh = el("button", "nm-pane-tool");
    refresh.type = "button";
    refresh.textContent = "⟳";
    refresh.title = "Ask git again";
    refresh.setAttribute("aria-label", "Ask git again");
    refresh.addEventListener("click", () => host.command("git.refresh"));
    this.tools.append(this.branch, refresh);

    this.element = el("div", "nm-git-panel");
    this.hint = el("div", "nm-pane-hint");
    this.box = el("div", "nm-git-commit");
    this.message = el("textarea", "nm-git-message");
    this.message.rows = 2;
    this.message.placeholder = "Message (⌘Enter or Ctrl+Enter)";
    this.message.setAttribute("aria-label", "Commit message");
    this.message.spellcheck = false;
    this.message.addEventListener("input", () => {
      this.committing = null; // written since: it is a new message, to be kept
      this.enable();
    });
    this.commitButton = el("button", "nm-git-commit-button");
    this.commitButton.type = "button";
    this.commitButton.textContent = "Commit";
    this.commitButton.addEventListener("click", () => this.commit());
    this.box.append(this.message, this.commitButton);
    this.list = el("div", "nm-git-list");
    this.list.setAttribute("role", "list");
    this.list.setAttribute("aria-label", "Changes");
    this.element.append(this.hint, this.box, this.list);
    this.update(null, false);
  }

  /** Whether the message box has the keyboard: keys are then its own, not the engine's. */
  get typing(): boolean {
    return document.activeElement === this.message;
  }

  /**
   * A key pressed while the message box has the keyboard.  True if it was
   * taken here; false leaves it to the box, as any text field takes keys.
   */
  key(event: KeyboardEvent): boolean {
    if (event.key === "Escape") {
      this.host.refocus();
      return true;
    }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      this.commit();
      return true;
    }
    return false;
  }

  private enable(): void {
    const ready = (this.info?.staged.length ?? 0) > 0 && this.message.value.trim() !== "";
    this.commitButton.disabled = !ready;
    this.commitButton.title = ready ? "Commit what is staged" : (this.info?.staged.length ?? 0) > 0 ? "Write a message first" : "Nothing is staged";
  }

  private commit(): void {
    if (this.commitButton.disabled || !this.info) return;
    this.committing = { top: this.info.top, head: this.info.head };
    this.host.command("git.commit", [this.message.value]);
    this.host.refocus();
  }

  /** Show the repository as the engine last described it; `project` says whether there is a project at all. */
  update(info: GitInfo | null, project: boolean): void {
    const key = JSON.stringify([info, project]);
    if (key === this.drawn) return;
    this.drawn = key;
    this.info = info;
    this.box.hidden = info === null;
    this.list.hidden = info === null;
    this.hint.hidden = info !== null;
    this.hint.textContent = project ? "This project is not in a git repository." : "Add a project to see its changes.";
    this.branch.textContent = info === null ? "" : `${info.branch ?? "detached"}${info.ahead ? ` ↑${info.ahead}` : ""}${info.behind ? ` ↓${info.behind}` : ""}`;
    this.branch.title = info?.upstream ? `Follows ${info.upstream}` : "";
    if (this.committing && info !== null && info.top === this.committing.top && info.head !== this.committing.head) {
      // The commit was made: the message has done its work.  (One that
      // failed leaves HEAD where it was, and the message with it.)
      this.committing = null;
      this.message.value = "";
    }
    this.enable();
    if (info === null) {
      this.list.replaceChildren();
      return;
    }
    const groups: HTMLElement[] = [];
    for (const [name, title] of GROUPS) {
      const rows = info[name];
      if (rows.length === 0) continue; // a group with nothing in it is not shown
      const group = el("div", "nm-git-group");
      group.dataset["group"] = name;
      const heading = el("div", "nm-git-group-heading");
      const label = el("span", "nm-git-group-title");
      label.textContent = `${title} (${rows.length})`;
      heading.append(label);
      if (name === "staged") heading.append(this.button("−", "Unstage all", "unstage_all", () => this.host.command("git.unstage_all")));
      if (name === "changes") heading.append(this.button("+", "Stage all changes", "stage_all", () => this.host.command("git.stage_all", ["changes"])));
      if (name === "untracked") heading.append(this.button("+", "Stage all new files", "stage_all", () => this.host.command("git.stage_all", ["untracked"])));
      group.append(heading, ...rows.map((row) => this.row(name, row, info)));
      groups.push(group);
    }
    if (groups.length === 0) {
      const clean = el("div", "nm-pane-hint");
      clean.textContent = "Nothing has changed.";
      groups.push(clean);
    }
    this.list.replaceChildren(...groups);
  }

  private button(text: string, title: string, action: string, run: () => void): HTMLButtonElement {
    const button = el("button", "nm-git-action");
    button.type = "button";
    button.textContent = text;
    button.title = title;
    button.dataset["action"] = action;
    button.setAttribute("aria-label", title);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      run();
    });
    return button;
  }

  private row(group: GroupName, row: GitRow, info: GitInfo): HTMLElement {
    const item = el("div", `nm-git-row nm-git-is-${row.state}`);
    item.setAttribute("role", "listitem");
    item.dataset["path"] = row.path;
    item.title = `${row.path} (${row.state})`;
    const slash = row.path.lastIndexOf("/");
    const name = el("span", "nm-git-name");
    name.textContent = row.path.slice(slash + 1); // text, never markup
    const where = el("span", "nm-git-where");
    where.textContent = slash >= 0 ? row.path.slice(0, slash) : "";
    const actions = el("span", "nm-git-actions");
    if (group === "staged") {
      actions.append(this.button("−", "Unstage", "unstage", () => this.host.command("git.unstage", [row.path])));
    } else {
      if (group !== "conflicted") actions.append(this.button("↶", group === "untracked" ? "Delete" : "Discard changes", "discard", () => this.host.command("git.discard", [row.path])));
      actions.append(this.button("+", group === "conflicted" ? "Mark as resolved" : "Stage", "stage", () => this.host.command("git.stage", [row.path])));
    }
    const letter = el("span", "nm-git-letter");
    letter.textContent = LETTERS[row.state] ?? "";
    item.append(name, where, actions, letter);
    item.addEventListener("click", () => {
      if (group === "conflicted") {
        // Both sides are in the file itself, between git's markers.
        const separator = info.top.includes("\\") && !info.top.includes("/") ? "\\" : "/";
        this.host.command("files.find", [info.top.replace(/[\\/]+$/, "") + separator + row.path.split("/").join(separator)]);
      } else {
        this.host.command("git.diff", [row.path, group === "staged"]);
      }
    });
    return item;
  }

  /**
   * An editing command chosen from the application menu while the message
   * box has the keyboard: it is about the message, not the text behind.
   * True if it was done here.
   */
  edit(command: string, clipboard: () => Promise<string>): boolean {
    const own: Record<string, string> = { undo: "undo", redo: "redo", kill_selection: "cut", copy_selection: "copy", select_all: "selectAll" };
    if (command === "yank") {
      void clipboard().then((text) => {
        if (this.typing) document.execCommand("insertText", false, text);
      });
      return true;
    }
    const action = own[command];
    if (action === undefined) return false;
    document.execCommand(action);
    return true;
  }

  /** What is drawn, for tests. */
  snapshot(): Record<string, unknown> {
    const rect = (element: Element | null) => {
      const r = element?.getBoundingClientRect();
      return r ? [r.left, r.top, r.width, r.height] : null;
    };
    return {
      hint: this.hint.hidden ? null : this.hint.textContent,
      branch: this.branch.textContent,
      message: this.message.value,
      messageRect: rect(this.message),
      commitEnabled: !this.commitButton.disabled,
      commitRect: rect(this.commitButton),
      typing: this.typing,
      groups: [...this.list.querySelectorAll<HTMLElement>(".nm-git-group")].map((group) => ({
        name: group.dataset["group"],
        title: group.querySelector(".nm-git-group-title")?.textContent ?? "",
        tool: rect(group.querySelector(".nm-git-group-heading .nm-git-action")),
        rows: [...group.querySelectorAll<HTMLElement>(".nm-git-row")].map((row) => ({
          path: row.dataset["path"],
          letter: row.querySelector(".nm-git-letter")?.textContent ?? "",
          rect: rect(row),
          actions: Object.fromEntries([...row.querySelectorAll<HTMLElement>(".nm-git-action")].map((b) => [b.dataset["action"], rect(b)])),
        })),
      })),
    };
  }
}
