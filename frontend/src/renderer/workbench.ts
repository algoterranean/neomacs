// The workbench (WORKSPACE.md section 5): the frame around the main area.
//
//   meta bar
//   project sidebar | details sidebar | main area | options sidebar
//   status bar
//
// These are this frontend's own widgets.  What they show is the engine's: the
// regions that are open and how wide, the list of projects, the items of the
// status bar.  Everything a click does is a command sent to the engine, which
// answers with new state; nothing here changes what is shown on its own.

import { type FileEntry, FileTree } from "./filetree.ts";
import type { FrameModel, RegionState, StatusItem, WorkspaceInfo } from "./model.ts";

export interface WorkbenchHost {
  /** Run a command in the engine, as a key would. */
  command(name: string, args?: (string | number | boolean | null)[]): void;
  /** A status item was clicked: the engine runs what it put there. */
  statusItem(side: "left" | "right", index: number): void;
  /** Give the keyboard back to the text: a click elsewhere took the browser's focus. */
  refocus(): void;
  /** Tell the engine a region was shown, hidden or resized. */
  region(name: string, shown: boolean | null, width: number | null): void;
  /** Ask the user for a directory with the system's own dialog. */
  chooseDirectory(): Promise<string | null>;
  /** Show a native context menu and resolve to the id chosen, if any. */
  contextMenu(items: { id: string; label: string }[]): Promise<string | null>;
  /** Show a path in the system's file manager. */
  reveal(path: string): void;
  /** The entries of a directory of a project, for the file tree. */
  listFiles(workspace: number, directory: string): Promise<FileEntry[]>;
  /** The window's title bar is the meta bar, with the window's buttons at its left. */
  ownTitleBar: boolean;
}

const SIDEBARS = ["project", "details", "options"] as const;
type Sidebar = (typeof SIDEBARS)[number];

const TOGGLES: [region: string, label: string, title: string, command: string][] = [
  ["project", "▌", "Project sidebar", "view.toggle_project_sidebar"],
  ["details", "▍▏", "Details sidebar", "view.toggle_details_sidebar"],
  ["bottom", "▁", "Bottom panel", "view.toggle_bottom_panel"],
  ["options", "▐", "Options sidebar", "view.toggle_options_sidebar"],
];

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, label?: string): HTMLElementTagNameMap[K] {
  const made = document.createElement(tag);
  made.className = className;
  if (label !== undefined) made.setAttribute("aria-label", label);
  return made;
}

export class Workbench {
  private host: WorkbenchHost;
  private metaBar: HTMLDivElement;
  private centre: HTMLButtonElement;
  private toggles = new Map<string, HTMLButtonElement>();
  private body: HTMLDivElement;
  private sidebars = new Map<Sidebar, HTMLDivElement>();
  private resizers = new Map<Sidebar, HTMLDivElement>();
  private projectList: HTMLDivElement;
  private statusBar: HTMLDivElement;
  private statusLeft: HTMLDivElement;
  private statusRight: HTMLDivElement;
  private drawn = { regions: "", workspaces: "", status: "", files: "" };
  /** The file tree, in the upper half of the details sidebar. */
  readonly files: FileTree;
  private dragging: Sidebar | null = null;

  /** `main` is where the views go; `messages` is the message strip, which sits in the status bar. */
  constructor(root: HTMLElement, main: HTMLElement, messages: HTMLElement, host: WorkbenchHost) {
    this.host = host;

    // -- meta bar: a command palette field, and a toggle for each region --
    this.metaBar = el("div", "nm-meta-bar", "Meta bar");
    this.metaBar.setAttribute("role", "toolbar");
    this.metaBar.classList.toggle("nm-own-title-bar", host.ownTitleBar);
    this.centre = el("button", "nm-meta-centre");
    this.centre.type = "button";
    this.centre.title = "Run a command";
    this.centre.addEventListener("click", () => host.command("execute_command"));
    const toggles = el("div", "nm-meta-toggles");
    for (const [region, label, title, command] of TOGGLES) {
      const button = el("button", "nm-meta-toggle", title);
      button.type = "button";
      button.textContent = label;
      button.title = title;
      button.dataset["region"] = region;
      button.addEventListener("click", () => host.command(command));
      this.toggles.set(region, button);
      toggles.append(button);
    }
    this.metaBar.append(el("div", "nm-meta-left"), this.centre, toggles);

    // -- the sidebars, each with an edge to drag --
    const project = el("div", "nm-sidebar nm-project-sidebar", "Project sidebar");
    project.setAttribute("role", "navigation");
    this.projectList = el("div", "nm-project-list");
    this.projectList.setAttribute("role", "listbox");
    this.projectList.setAttribute("aria-label", "Projects");
    const add = el("button", "nm-project-add", "Add a project");
    add.type = "button";
    add.textContent = "+";
    add.title = "Add a project";
    add.addEventListener("click", () => {
      void host.chooseDirectory().then((path) => {
        if (path) host.command("project.add", [path]);
      });
    });
    project.append(this.heading("Projects"), this.projectList, add);

    // The details sidebar is split at half its height: files above, git below.
    // The git half is empty until its stage (WORKSPACE W-5).
    const details = el("div", "nm-sidebar nm-details-sidebar", "Details sidebar");
    details.setAttribute("role", "complementary");
    this.files = new FileTree({
      list: (workspace, directory) => host.listFiles(workspace, directory),
      command: (name, args) => host.command(name, args),
      contextMenu: (items) => host.contextMenu(items),
      reveal: (path) => host.reveal(path),
    });
    const files = el("div", "nm-details-pane nm-files-pane", "Files");
    const filesHeading = this.heading("Files");
    filesHeading.classList.add("nm-pane-heading");
    filesHeading.append(this.files.tools);
    const filesBody = el("div", "nm-pane-body");
    filesBody.append(this.files.element);
    files.append(filesHeading, filesBody);
    const git = el("div", "nm-details-pane nm-git-pane", "Git");
    git.append(this.heading("Git"), el("div", "nm-pane-body"));
    details.append(files, el("div", "nm-pane-divider"), git);

    const options = el("div", "nm-sidebar nm-options-sidebar", "Options sidebar");
    options.setAttribute("role", "complementary");
    options.append(this.heading("Options"));

    this.sidebars.set("project", project).set("details", details).set("options", options);
    for (const name of SIDEBARS) this.resizers.set(name, this.resizer(name));

    main.classList.add("nm-main-area");
    this.body = el("div", "nm-body");
    this.body.append(
      project,
      this.resizers.get("project") as HTMLDivElement,
      details,
      this.resizers.get("details") as HTMLDivElement,
      main,
      this.resizers.get("options") as HTMLDivElement,
      options,
    );

    // -- status bar: the frame at the left, messages, the focused window at the right --
    this.statusBar = el("div", "nm-status-bar", "Status bar");
    this.statusLeft = el("div", "nm-status-items nm-status-left");
    this.statusRight = el("div", "nm-status-items nm-status-right");
    this.statusBar.append(this.statusLeft, messages, this.statusRight);

    root.replaceChildren(this.metaBar, this.body, this.statusBar);

    // A click on any of this takes the browser's focus from the element the
    // text is typed into, and with it the input method.  Nothing here is
    // typed into, so the focus goes straight back.
    for (const region of [this.metaBar, project, details, options, this.statusBar]) {
      region.addEventListener("mouseup", () => host.refocus());
      region.addEventListener("dragend", () => host.refocus());
    }
  }

  private heading(text: string): HTMLDivElement {
    const heading = el("div", "nm-sidebar-heading");
    heading.textContent = text;
    return heading;
  }

  /** The edge of a sidebar: drag it to change the sidebar's width. */
  private resizer(name: Sidebar): HTMLDivElement {
    const bar = el("div", "nm-sidebar-resizer");
    bar.setAttribute("role", "separator");
    bar.setAttribute("aria-orientation", "vertical");
    bar.dataset["region"] = name;
    bar.addEventListener("pointerdown", (event) => {
      const sidebar = this.sidebars.get(name) as HTMLDivElement;
      // The sidebar's own font size: its width is set in its own ems.
      const em = parseFloat(getComputedStyle(sidebar).fontSize) || 14;
      const startX = event.clientX;
      const startWidth = sidebar.getBoundingClientRect().width;
      const sign = name === "options" ? -1 : 1; // the options sidebar grows to the left
      let width = startWidth;
      this.dragging = name;
      bar.setPointerCapture(event.pointerId);
      let moved = false;
      const move = (e: PointerEvent) => {
        moved = true;
        width = Math.max(6 * em, Math.min(60 * em, startWidth + sign * (e.clientX - startX)));
        sidebar.style.width = `${width / em}em`;
      };
      const finish = (e: PointerEvent) => {
        bar.removeEventListener("pointermove", move);
        bar.removeEventListener("pointerup", finish);
        bar.removeEventListener("pointercancel", finish);
        this.dragging = null;
        if (e.type === "pointercancel") {
          // Called off: the width is what the engine last said.
          this.drawn.regions = "";
          this.host.refocus();
          return;
        }
        // The engine keeps the width; it comes back in `regions`.  A press
        // and release with no movement is not a resize.
        if (moved) this.host.region(name, null, Math.round((width / em) * 100) / 100);
      };
      bar.addEventListener("pointermove", move);
      bar.addEventListener("pointerup", finish);
      bar.addEventListener("pointercancel", finish);
      event.preventDefault();
    });
    return bar;
  }

  /** Bring the regions into line with what the engine last said. */
  update(frame: FrameModel): void {
    this.updateRegions(frame.regions);
    this.updateWorkspaces(frame.workspaces);
    this.updateStatus(frame.statusLeft, frame.statusRight);
    this.updateFiles(frame);
  }

  /** The file tree shows the current project, and reads it again when told it is stale. */
  private updateFiles(frame: FrameModel): void {
    const current = frame.workspaces.find((w) => w.current) ?? null;
    this.files.setWorkspace(current);
    const key = `${frame.resets}:${frame.filesChanged}`;
    if (key === this.drawn.files) return;
    const first = this.drawn.files === "";
    this.drawn.files = key;
    if (!first) this.files.refresh();
  }

  /** Give the keyboard to the file tree (`view.focus_files`). */
  focusFiles(): void {
    this.files.setActive(true);
  }

  private updateRegions(regions: Record<string, RegionState>): void {
    const key = JSON.stringify(regions);
    if (key === this.drawn.regions) return;
    this.drawn.regions = key;
    const shown = (name: string) => regions[name]?.shown === true;
    this.metaBar.hidden = !shown("meta");
    for (const name of SIDEBARS) {
      const sidebar = this.sidebars.get(name) as HTMLDivElement;
      sidebar.hidden = !shown(name);
      (this.resizers.get(name) as HTMLDivElement).hidden = !shown(name);
      const width = regions[name]?.width;
      if (typeof width === "number" && this.dragging !== name) sidebar.style.width = `${width}em`;
    }
    // With the status bar hidden its strip is still where messages are shown.
    this.statusBar.classList.toggle("nm-status-plain", !shown("status"));
    for (const [region, button] of this.toggles) {
      button.classList.toggle("nm-on", shown(region));
      button.setAttribute("aria-pressed", shown(region) ? "true" : "false");
    }
  }

  private updateWorkspaces(workspaces: WorkspaceInfo[]): void {
    const key = JSON.stringify(workspaces);
    if (key === this.drawn.workspaces) return;
    this.drawn.workspaces = key;
    const current = workspaces.find((w) => w.current);
    this.centre.textContent = current ? current.name : "Neomacs"; // text, never markup
    const rows = workspaces.map((workspace, index) => {
      const row = el("div", workspace.current ? "nm-project nm-current" : "nm-project");
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", workspace.current ? "true" : "false");
      row.dataset["id"] = String(workspace.id);
      row.title = workspace.root ?? "";
      const name = el("span", "nm-project-name");
      name.textContent = workspace.name;
      const mark = el("span", "nm-project-unsaved");
      mark.textContent = workspace.unsaved > 0 ? "●" : "";
      if (workspace.unsaved > 0) mark.title = `${workspace.unsaved} unsaved`;
      row.append(name, mark);
      row.addEventListener("click", () => this.host.command("project.switch", [workspace.id]));
      if (workspace.root !== null) {
        const root = workspace.root;
        row.addEventListener("contextmenu", (event) => {
          event.preventDefault();
          const items = [
            { id: "rename", label: "Rename…" },
            { id: "reveal", label: "Reveal in File Manager" },
            { id: "remove", label: "Remove from the List" },
          ];
          void this.host.contextMenu(items).then((chosen) => {
            if (chosen === "rename") this.host.command("project.rename", [workspace.id]);
            else if (chosen === "remove") this.host.command("project.remove", [workspace.id]);
            else if (chosen === "reveal") this.host.reveal(root);
          });
        });
        // Rows drag to another place in the list.
        row.draggable = true;
        row.addEventListener("dragstart", (event) => event.dataTransfer?.setData("application/x-neomacs-project", String(workspace.id)));
        row.addEventListener("dragover", (event) => {
          if (event.dataTransfer?.types.includes("application/x-neomacs-project")) event.preventDefault();
        });
        row.addEventListener("drop", (event) => {
          const moved = Number(event.dataTransfer?.getData("application/x-neomacs-project"));
          if (moved && moved !== workspace.id) this.host.command("project.move", [moved, index]);
          event.preventDefault();
        });
      }
      return row;
    });
    this.projectList.replaceChildren(...rows);
  }

  private updateStatus(left: StatusItem[], right: StatusItem[]): void {
    const key = JSON.stringify([left, right]);
    if (key === this.drawn.status) return;
    this.drawn.status = key;
    const draw = (items: StatusItem[], side: "left" | "right") =>
      items.map((item, index) => {
        const command = item.command;
        const made = el(command ? "button" : "span", command ? "nm-status-item nm-status-button" : "nm-status-item");
        made.textContent = item.text; // text, never markup
        if (item.title) made.title = item.title;
        if (command) {
          (made as HTMLButtonElement).type = "button";
          // The engine is told which item, and runs what it put there, in the view it describes.
          made.addEventListener("click", () => this.host.statusItem(side, index));
        }
        return made;
      });
    this.statusLeft.replaceChildren(...draw(left, "left"));
    this.statusRight.replaceChildren(...draw(right, "right"));
  }

  /** What is drawn, for tests: where each region is, and what the lists hold. */
  snapshot(): Record<string, unknown> {
    const rect = (element: Element) => {
      const r = element.getBoundingClientRect();
      return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    };
    const where = (element: HTMLElement) => (element.hidden ? null : rect(element));
    return {
      meta: where(this.metaBar),
      project: where(this.sidebars.get("project") as HTMLElement),
      details: where(this.sidebars.get("details") as HTMLElement),
      options: where(this.sidebars.get("options") as HTMLElement),
      status: rect(this.statusBar),
      statusPlain: this.statusBar.classList.contains("nm-status-plain"),
      files: rect(this.body.querySelector(".nm-files-pane") as Element),
      git: rect(this.body.querySelector(".nm-git-pane") as Element),
      tree: this.files.snapshot(),
      centre: this.centre.textContent,
      toggles: Object.fromEntries([...this.toggles].map(([name, button]) => [name, button.classList.contains("nm-on")])),
      projects: [...this.projectList.children].map((row) => ({
        name: row.querySelector(".nm-project-name")?.textContent ?? "",
        current: row.classList.contains("nm-current"),
        unsaved: (row.querySelector(".nm-project-unsaved")?.textContent ?? "") !== "",
        rect: rect(row),
      })),
      statusLeft: [...this.statusLeft.children].map((item) => item.textContent),
      statusRight: [...this.statusRight.children].map((item) => item.textContent),
    };
  }
}
