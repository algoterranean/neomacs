# Projects and the workbench design

Status: draft 2, 2026-11-27. Stages W-1 (projects in the engine) and W-2
(the frame) are built; section 13 says how they differ from what is
described before it. Stages W-3 to W-6 are not built. Section 2 holds the decisions taken. Nothing is open.

This describes two things that arrive together: **projects**, each with its
own buffers, windows and layout, and the **workbench**, the frame around the
main area in the Electron frontend that makes it look and work like VS Code:
a meta bar, three sidebars and a status bar, laid out as `CLAUDE.md` names
them. It assumes ENGINE.md (4, 8) and FRONTEND.md (5, 13).

## 1. Goals and non-goals

Goals:

- Several projects open in one editor, switched from the project sidebar.
  Each keeps its own open buffers, window splits, tabs and panels, and comes
  back as it was left.
- A frame a VS Code user finds their way around: files in a tree, changes in
  a git panel, tabs over each editor, one status bar, a command palette in
  the title bar.
- Everything a sidebar does is a command, so a key or a line of Python can
  do it too.
- What was open yesterday is open today.

Non-goals, in this change (W6):

- Quick open by file name, and search across a project.
- An integrated terminal, language servers, a settings screen, a minimap,
  breadcrumbs, an extensions marketplace view.
- Watching the disk for changes (ENGINE 16 defers it).
- The workbench in the terminal frontend. It gets the project commands
  through prompts, and nothing is taken away from it.

## 2. Decisions taken

All by the user, 2026-11-27, unless a reason says otherwise.

| # | Decision | Reason |
|---|---|---|
| W1 | The sidebars are native Electron widgets, not buffers shown in regions. | Closest to how VS Code feels: rows that hover, buttons on a row, context menus. |
| W2 | The engine owns what the sidebars show and do: the project list, directory listings, git state and every action. The frontend draws and asks. | One source of truth; commands, keys and Python reach the same actions as the mouse (ENGINE D5 and D17 are the same split). |
| W3 | The details sidebar is split at half its height: the file tree above, git status below. The project sidebar lists projects. | |
| W4 | Modelines stay. The status bar is one strip for the frame and shows frame-wide things. | `CLAUDE.md`: the status bar is not the modeline. |
| W5 | Editor tabs are per window, as in VS Code. | |
| W6 | In scope: tabs, git actions with side-by-side diffs, session restore. Out: section 1's non-goals. | |
| W7 | The meta bar holds a command palette field and toggles for the sidebars. | |
| W8 | The options sidebar is built as a region that can be shown and hidden, and is empty. | Its contents are to be decided later. |
| W9 | One engine holds every project. The kill ring, configuration, extensions and key bindings are shared. | Proposed here. Switching is then instant and nothing has to be handed between processes. |
| W10 | The user-facing word is "project". In engine code the class is `Workspace`. | `neomacs/project.py` already means a directory's `.neomacs/settings.toml` (ENGINE D31). |
| W11 | The two sides of a diff are not kept level row for row in this change. They scroll together by matching lines. | Level rows need filler the protocol cannot ask for yet; that is a design of its own. |
| W12 | Each frame has its own current project. Two Electron windows can show two projects. | |
| W13 | A project with unsaved buffers cannot be removed until they are saved or killed. | |
| W14 | A panel docked at the bottom sits under the main area only; the sidebars run the full height beside it. | As VS Code has it by default. |
| W15 | The centre of the meta bar shows the project's name, as the button that opens the command palette. | |

## 3. Words

| Word | Meaning |
|---|---|
| project | What the user sees: a directory, and what is open in it. |
| `Workspace` | The engine's object for a project. |
| arrangement | One frame's layout for one project: the window tree, each window's tabs, the panels, which view has the keyboard. |
| region | A fixed part of the frame: meta bar, project sidebar, details sidebar, main area, options sidebar, status bar. The names are `CLAUDE.md`'s and are used exactly, in identifiers and CSS classes too. |
| workbench | The regions together. A capability a frontend claims (4.1). |

`layout.ts` has a class `Workspace` today, the arranger of the main area. It
is renamed `MainArea`, so the word has one meaning.

## 4. Projects

### 4.1 The model

```python
ed.workspaces  # every project, in sidebar order
ed.workspaces.add(path)  # make a directory a project
ed.workspaces.remove(ws)
ed.workspaces.of(buffer)  # the project a buffer belongs to
frame.workspace  # the project this frame is showing
frame.switch(ws)
```

A `Workspace` has an id, a name (the directory's name unless renamed), a
root directory, and for each frame an arrangement.

- **Where a buffer belongs.** A buffer visiting a file belongs to the
  project whose root is the nearest ancestor of the file. A file under no
  project's root, and a buffer with no file, belong to the project that was
  current when they were opened: opening `~/.zshrc` while working in a
  project does not take the user out of it. Buffers the editor writes
  itself (`*messages*`, `*help*`, and the rest named like that) belong to
  none and can be shown in any.
- **Opening a file that is in another project** switches to that project
  and shows it there. A buffer is only ever shown in its own project.
- **No project.** A built-in project called "No project" has no root,
  cannot be removed, and is what a fresh editor shows. What is opened while
  it is current, and is under no root, belongs to it. Its details sidebar
  is empty.
- **Membership follows the file.** Saving a buffer under a new name moves
  it to the project its new place is in. Adding a project whose root
  contains files that are already open moves those buffers into it.
- **Nested roots.** The nearest root wins, as with project settings
  (EXTENSIONS 8.1). A project's settings file and a project in this sense
  are found independently: one is a file in a directory, the other is a
  directory the user added.

### 4.2 Switching

`frame.switch(ws)`:

1. Stores the frame's window tree, panels, focused view and main window as
   the old project's arrangement for this frame.
2. Loads the new project's arrangement for this frame; if it has none, a
   single window on the project's most recent buffer, or an empty one.
3. Sends the frontend everything again (`full_sync`, ENGINE 4.6).

Consequences:

- Views of a project that is not showing stay alive. Their selections
  follow edits, as any view's do, so a buffer shared between two projects'
  arrangements (an editor buffer such as `*messages*`) is safe.
- Unsaved buffers of a project that is not showing stay open, stay unsaved,
  and stay in the recovery journal (ENGINE 11.1).
- A command running in one project that switches away keeps its pinned view
  and buffer (ENGINE 7.7); only what the frame shows changes.
- Prompts do not survive a switch: an open prompt is cancelled first.
- Timers, tasks and processes belong to the editor, not to a project, and
  carry on.

### 4.3 What narrows to the project

- `buffers.switch` offers the current project's buffers and the editor's
  own. With a prefix argument it offers every buffer; choosing one from
  another project switches to that project.
- Tabs (section 7) list only buffers of the window's project.
- `files.find` starts in the project's root, not the engine's working
  directory.
- `shell_command` runs in the project's root when the buffer has no file.

### 4.4 Commands

| Command | Does |
|---|---|
| `project.add` | Ask for a directory and make it a project; switch to it. |
| `project.switch` | Ask for a project by name, with completion. |
| `project.next`, `project.previous` | Step through the project sidebar. |
| `project.rename` | Change the name shown. |
| `project.remove` | Take a project out of the list. Refused while it has unsaved buffers (W13). Its buffers are killed; its files are not touched. |

## 5. The workbench

### 5.1 Regions

```
+-----------------------------------------------------------------------+
| meta bar                                                              |
+----------+----------+--------------------------------------+----------+
|          | files    |  tab  tab  tab                       |          |
| project  |          |                                      | options  |
| sidebar  +----------+             main area                | sidebar  |
|          | git      |                                      |          |
|          |          +--------------------------------------+          |
|          |          |  bottom panel                        |          |
+----------+----------+--------------------------------------+----------+
| status bar                                                            |
+-----------------------------------------------------------------------+
```

- The main area is what the frame is today: the window tree and the panels
  docked around it (ENGINE 8.5). A panel docked at the bottom is under the
  main area only, between the sidebars (W14).
- Each sidebar has a width, kept in ems, and can be hidden. Dragging its
  inner edge resizes it. Hidden sidebars give their space to the main area.
- The engine holds which regions are shown and how wide, per frame, so that
  a command or a key can toggle one: `view.toggle_project_sidebar`,
  `view.toggle_details_sidebar`, `view.toggle_options_sidebar`,
  `view.toggle_bottom_panel`. The frontend reports a drag.

### 5.2 Meta bar

- On macOS it is the window's title bar: the native bar is hidden, the
  traffic lights sit inset at its left, and the bar drags the window. On
  other platforms it is a strip under the native frame.
- Centre: a field showing the project's name. Clicking it, or `s-P`, opens
  the command palette (`execute_command`, which is the prompt that exists).
- Right: a toggle button for each sidebar and for the bottom panel, lit
  when the region is shown.

### 5.3 Project sidebar

- One row for each project: its name, and a dot when it has unsaved
  buffers. The current one is marked. "No project" is last.
- Click switches. A "+" at the bottom opens the system's directory dialog
  and runs `project.add`. A row's context menu has Rename, Remove, and
  Reveal in the file manager.
- Rows can be dragged to reorder.

### 5.4 Details sidebar: files

- A tree of the project's root, read a directory at a time as it is
  expanded. Which directories are expanded is remembered for each project.
- Directories first, then files, by name. Names matched by `.gitignore` are
  shown dimmed; `.git` is not shown.
- A file takes the colour of its git state (modified, untracked, staged,
  conflicted), and a directory that of the most pressing state inside it.
- Click opens the file in the focused window. Up, down, left, right, Return
  and typing a name's first letters work when the tree has the keyboard,
  which `view.focus_files` (`s-E`) gives it and Escape takes away.
- Context menu: New File, New Folder, Rename, Delete, Copy Path, Reveal.
  Delete moves to the system trash where there is one and asks first.
- The tree is read again after a save, a file action, a git action, when
  the window regains focus, and by a refresh button. Nothing watches the
  disk.

### 5.5 Details sidebar: git

- Shown when the project's root is in a git repository; otherwise it says
  so and offers nothing.
- Heading: the branch, and how far ahead and behind its upstream.
- Three groups, each hidden when empty: Staged, Changes, Untracked. A row
  is a path and a letter for its state.
- On a row: a button to stage or unstage, and one to discard, which asks
  first. On a group's heading: stage all, unstage all.
- A commit box at the top: a message, and a Commit button that is enabled
  when something is staged and the message is not empty.
- Click on a row opens its diff (section 8).
- Read again at the same moments as the file tree.

### 5.6 Options sidebar

A region with a heading and nothing in it. Hidden by default.

### 5.7 Status bar

One strip across the bottom of the frame.

| Left | Middle | Right |
|---|---|---|
| project name; branch with ahead and behind; running tasks with their progress | the latest message; keys typed so far | line and column; selection count; mode; encoding; line ending |

- The right-hand part describes the focused window. The same facts stay in
  that window's modeline (W4): the modeline is what tells two windows apart
  when both are in view.
- The message strip under the main area today goes: its text is the middle
  of the status bar. Warnings and errors still raise a toast.
- Clicking the branch focuses the git panel; clicking the position runs
  `goto_line`; clicking the mode runs a prompt to choose one.

## 6. Protocol

A frontend that draws the workbench claims the capability `workbench` when
it attaches. Everything below is sent only to a frontend that claimed it
(STABILITY 5.1), so the terminal frontend sees none of it.

Engine to frontend, in `redraw`:

| Event | Meaning |
|---|---|
| `["regions", {name: {"shown": bool, "width": ems}}]` | Which regions are shown: `meta`, `project`, `details`, `options`, `status`, and `bottom` for the bottom panel. The sidebars have a width. |
| `["workspaces", [{"id", "name", "root", "current", "unsaved"}]]` | The project list, in order. |
| `["status", left, right]` | The status bar's items at each end: `{"text", "command", "title"}`, the last two optional. Clicking an item with a `command` runs it. The middle of the bar is the `message` events there have always been. |
| `["git", {"branch", "ahead", "behind", "staged", "changes", "untracked", "conflicted"} or null]` | The current project's git state; `null` when it is not in a repository. Each list is of `{"path", "state"}`. |
| `["files_changed", workspace]` | Listings the frontend holds for this project may be out of date: ask again. |

One notification outside `redraw`: `ui.focus("files")` asks the frontend to
give the keyboard to its file tree.

`win_info` gains `tabs`: `[{"title", "modified", "active", "path"}]`, the last
only for a buffer with a file (section 7). It is absent when the `tabs`
option is off.

Frontend to engine:

| Method | Kind | Purpose |
|---|---|---|
| `files.list(workspace, directory)` | request | The entries of a directory, given relative to the project's root (`""` is the root): `[{"name", "kind", "git", "ignored"}]`. `kind` is `directory` or `file`; `git` is `conflicted`, `modified`, `staged`, `untracked` or null. |
| `ui.region(name, shown, width)` | notification | The user hid, showed or resized a region. Either of the last two may be `null` for "as it is". |
| `ui.status(side, index)` | notification | The user clicked an item of the status bar. The engine runs the command it put there, in the view the bar describes; the frontend names no command. |
| `ui.tab(view, action, index, to)` | notification | `activate`, `close`, or `move` (to the place `to`) the tab at `index` of the window `view`. |
| `engine.call(command, args)` | request | Everything else: `project.switch`, `git.stage`, `files.rename` and the rest are commands. |

The protocol version does not change: all of this belongs to a new
capability.

## 7. Editor tabs

- A window has `tabs`: the buffers that have been shown in it, in the order
  they were first shown. Showing a buffer adds it; the shown one is active.
- `tabs.close` (`s-w`) removes the active tab and shows the one to its
  left. Closing the last tab of a window removes the window if it is not
  the only one, and otherwise shows an empty buffer. Closing a tab does not
  kill the buffer; a buffer in no window's tabs stays open until killed.
- `tabs.next`, `tabs.previous` (`s-}`, `s-{`), `tabs.move_left`,
  `tabs.move_right`.
- Splitting a window gives the new one a single tab, the buffer it shows.
- Killing a buffer removes it from every window's tabs.
- Panels keep their own tab strips (ENGINE 8.5), unchanged.
- In Electron: a strip above each window. A tab has its name, a dot when
  modified, and a close button; a middle click closes; tabs drag to reorder
  within their strip. Dragging between windows is not in this change.

## 8. Diffs

`git.diff(path)`, also what a click in the git panel runs:

- Splits the focused window in two, side by side. Left: a read-only buffer
  holding the version in `HEAD` (for a staged row, `HEAD` against the
  index; for an unstaged row, the index against the file). Right: the
  file's own buffer, editable as ever.
- Lines that differ are marked with a face on each side: removed, added,
  changed. The marks are recomputed when the file's buffer changes.
- Scrolling either side reveals the matching line on the other.
- `git.next_change`, `git.previous_change` move between changed blocks.
- Closing either window ends the diff and removes the marks.

Not in this change: keeping the two sides level row for row. Lines wrap to
different heights and one side has lines the other lacks, so level rows
need filler the protocol has no way to ask for yet (W11).

## 9. Session restore

- `session.json` in the data directory (DISTRIBUTION 5) holds: the
  projects in order; for each, the files open with their cursor positions,
  and each frame's arrangement (window tree with ratios, tabs and which is
  active, panels); the regions' state; which project was current.
- It is written a moment after any of that changes, and when the engine
  stops, by the pattern of the recovery journal: decided on the owner
  thread, written in a worker thread, replaced whole.
- At startup it is read before any frontend is served. Projects and buffers
  are made then; an arrangement is applied to the first frame that attaches
  (and the second to the second, for as many as there were).
- A file that has gone is left out, with one message naming them all.
- It holds no text. Unsaved text is the journal's, and `recover` still
  offers it; the two do not depend on each other.
- The `session_restore` option, on by default, turns reading it off.
  `--safe` does not: a session is data, not code.
- Editor buffers and buffers with no file are not restored.

## 10. Implementation stages

Each is one branch and one pull request, and ends in something usable.

**W-1: projects in the engine.** `Workspace`, membership, arrangements,
`frame.switch`, the commands of 4.4, the narrowing of 4.3. *Runnable:* in
the terminal frontend, add two directories as projects, open files and
split windows in each, and switch back and forth.

**W-2: the frame.** The capability, `regions`, `workspaces` and `status`;
in Electron the six regions, the meta bar, the status bar, the toggles, and
the project sidebar working. The other sidebars are empty. *Runnable:*
click between projects in the Electron window.

**W-3: editor tabs.** Section 7.

**W-4: the file tree.** `files.list`, the file commands, the tree widget.

**W-5: git.** The status model, the panel, stage, unstage, discard and
commit, and the diff view.

**W-6: session restore.** Section 9.

## 11. Verification

- **Projects:** switching swaps the window tree and the buffers offered,
  and back again with every cursor where it was; an unsaved buffer in a
  project that is not showing is still journalled; save-as moves a buffer
  between projects; a nested root wins.
- **Tabs:** open, close, reorder, split; closing the last; killing a buffer
  shown in two windows.
- **Files:** `files.list` and each file command against a temporary
  directory, including names that are not valid Unicode and a directory
  that cannot be read.
- **Git:** against a repository made with real `git` in a temporary
  directory: status in each state, stage, unstage, discard, commit; the
  lines a diff marks are the lines `git diff` reports.
- **Session:** an engine writes one, a second engine restores it; a deleted
  file is skipped; a session file that is damaged is ignored with a message.
- **Electron** (`npm run smoke`, with real keys and clicks, and pictures
  saved and looked at): the regions where `CLAUDE.md` puts them; each
  sidebar toggles and resizes; a click on a project switches the main
  area; the tree expands and opens a file; staging moves a row, and a
  commit clears the panel; a diff shows two windows; tabs close and
  reorder; after the engine is restarted the projects, tabs and splits are
  back.
- **Standing:** the whole suite, strict type checking, the manifest, Gate
  D, the latency benchmark, and the renderer budgets of FRONTEND 11 with
  the workbench on screen.
- **Not verifiable without the user:** how it looks and feels in a visible
  window.

## 12. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| A second kind of interface beside buffers (W1) | Sidebars are not scriptable as buffers are, and the terminal frontend has none | Every action is a command (W2); the terminal gets them through prompts. |
| Git on a large repository | `git status` takes seconds; the panel lags | It runs as a background task, never on the lane; a refresh under way is not started twice. |
| Nothing watches the disk | The tree and the git panel are stale after work in a terminal | Refreshed on focus, which is when the user comes back from one. |
| A full sync on every switch | A visible flash on a large layout | Measured in W-2; if it shows, only what differs is sent. |
| The session file and the journal disagree | A restored buffer and a recovered one for the same file | Recovering replaces the text of the buffer that is already visiting the file, which is what `recover` does today. |

## 13. As built

### W-1: projects in the engine

Built as sections 4.1 to 4.4 describe, with these details.

- **Where things are.** `neomacs/workspace.py` has `Workspace`,
  `Workspaces` (`ed.workspaces`) and the hooks `workspace_changed` and
  `workspaces_changed`. `Frame.workspace` and `Frame.switch` are in
  `neomacs/window.py`. The commands are in `neomacs/std/projects.py`.
- **Keys.** `C-c p p` switches, `C-c p a` adds, `C-c p n` and `C-c p b` go
  to the next and the previous. `C-x p` was taken by panels.
- **`ed.show` is where a buffer is kept to its own project.** Showing a
  buffer as a document switches the frame to the buffer's project first.
  Opening a file, choosing a buffer, following a link in a traceback and
  recovering unsaved text all go through it, so all of them behave alike.
  Showing a buffer in a panel (help, messages, output) does not switch.
- **A frame's table of views holds only the project on show.** The views
  of a project that is put away are kept with its arrangement and are not
  sent to the frontend. They stay attached to their buffers, so their
  cursors follow edits made meanwhile.
- **Killing a buffer** that a put-away window shows gives that window
  another buffer of its own project, or a scratch buffer; a put-away panel
  tab that shows it is closed. Both are what happens to one on screen.
- **Saving a buffer into another project** takes it out of the windows of
  the project it has left, and a frame that was working in it follows it
  to its new project.
- **A command that switches projects keeps its pinned view and buffer**
  (ENGINE 7.7): a view put away with its project still counts as the
  frame's (`Frame.owns`).
- **Unsaved means any modified buffer of the project,** with a file or
  without, for the purpose of W13.
- **A frame that goes** takes its arrangements with it.
- **Names are kept distinct:** a second project whose directory has the
  same name is called `name (2)`.
- **`project.switch` lists the projects in their own order** until
  something is typed, and then by how well they match.
- **Not in W-1:** nothing of a project is remembered across a restart
  (that is W-6), and the frame's title does not name the project (W-2).
- **Verified:** 19 tests through the reference frontend, which lays out as
  the terminal frontend does: the list; membership, with nested roots and
  save-as; each project's windows, cursors and panels coming back; unsaved
  work in a project that is not showing; prompts; killing and removing; two
  frames with a project and a layout each. It was not run by hand in a real
  terminal.

### W-2: the frame

Built as sections 5.1 to 5.3, 5.6 and 5.7 describe, with these differences.

- **Where things are.** Engine: `neomacs/workbench.py` (what the frontend
  is told), `neomacs/std/workbench.py` (the toggles). Electron:
  `frontend/src/renderer/workbench.ts`.
- **The meta bar and the status bar can be hidden too,** not only the
  sidebars: `view.toggle_meta_bar`, `view.toggle_status_bar`. With the
  status bar hidden its strip is still there for messages, as the message
  strip was. `view.toggle_workbench` hides everything around the main
  area, or brings the default back.
- **The `workbench` option** says whether a new window starts with the
  regions shown. It is on by default.
- **Status items are objects, not runs,** and the middle of the bar is not
  in the `status` event: it is the message and the pending keys, which
  arrive as they always have (section 6 is corrected).
- **Clicks are notifications.** A click on a project or a toggle sends
  `engine.call` without waiting for an answer, since a command that asks
  the user something does not answer until they have. A click on a status
  item sends `ui.status`.
- **The right-hand end of the status bar is about the view with the
  keyboard,** a panel's tab included; while a prompt has the keyboard, about
  the window that last had it. A click on an item there gives up any open
  prompt, gives that view the keyboard, and runs the command in it.
- **A click on the frame gives the browser's focus back to the text,** so
  that an input method goes on working.
- **Keys.** `s-b` toggles the details sidebar and `s-P` opens the command
  palette.
- **Colours** are three new faces, `metabar`, `sidebar` and `statusbar`,
  in both themes.
- **The details and options sidebars are empty.** The details sidebar has
  its two halves and their headings.
- **Ahead and behind** are not in the status bar yet: they need git, which
  is W-5. The branch is there.
- **A fault this exposed, now fixed.** A geometry request that needed lines
  the page did not hold asked for them and waited. Any update that arrived
  meanwhile caused a render, which reported the real viewport again, and
  the engine then did not send the lines. Updates were rare before the
  status bar; with it they are not. A view now reports no viewport of its
  own while such a request is waiting.
- **Verified:** 8 engine tests through the reference frontend claiming the
  capability; 10 checks in `npm run smoke` with real clicks and keys in the
  Electron window (where each region is, switching by a click, the unsaved
  mark, toggles by key and by button, resizing by dragging an edge, the
  palette field and a status item running their commands, both themes,
  labels). Pictures of the page in both themes were saved and looked at.
- **Not verified:** the system dialog for adding a project, the context
  menu on a project and dragging a project to reorder it are written and
  were not exercised: the smoke test cannot drive native dialogs and menus.
  The title-bar arrangement on macOS (hidden title bar, the window's
  buttons inset) was not seen in a visible window; off screen there are no
  such buttons. Nothing was tried on Linux or Windows.

### W-3: editor tabs

Built as section 7 describes, with these details.

- **Where things are.** Engine: `Window.tabs`, `Window.move_tab` and
  `Frame.close_tab` in `neomacs/window.py`; the commands in
  `neomacs/std/tabs.py`; `ui.tab` in `neomacs/rpc/ui.py`. Electron: the
  strip is drawn by `MainArea` in `frontend/src/renderer/layout.ts`.
- **Commands and keys.** `tabs.next` (`s-}`, `C-x t n`), `tabs.previous`
  (`s-{`, `C-x t p`), `tabs.close` (`s-w`, `C-x t k`), `tabs.move_left` and
  `tabs.move_right` (`C-x t <left>`, `C-x t <right>`), `tabs.close_others`.
  There is no single `tabs.move`. They work in every frontend: the terminal
  draws no strip, but its windows have tabs all the same.
- **The untouched scratch buffer is a stand-in.** A window that has nothing
  but the empty, unmodified `*scratch*` gives that tab up to the first
  buffer shown in it, so a new window or project does not start with a
  scratch tab beside the first file. Once typed in, it is a tab like any
  other.
- **Closing the last tab of the only window** shows `*scratch*`. If that
  is the tab being closed, nothing happens and a message says so.
- **A buffer that goes loses its tab everywhere:** killed by the command or
  by a program, in windows on screen and in the layouts of projects put
  away. A window that was showing it shows the tab to its left. The same
  when a buffer is saved into another project, or a new project takes in
  files that were open.
- **The `tabs` option** (on by default) says whether the strips are drawn;
  `view.toggle_tabs` flips it. With it off `win_info` carries no `tabs`.
- **A tab also carries `path`** when its buffer has a file, shown when the
  pointer rests on it.
- **In the strip,** a tab shows a dot in place of its close button while
  its buffer is unsaved, and the button when the pointer is over it. More
  tabs than fit can be scrolled along, and the one showing is kept in
  sight. A click on a tab gives its window the keyboard.
- **The frontend names a tab by its place** in the strip it was last sent.
  A click that crosses with a change to the strip can therefore act on the
  wrong tab; nothing guards against that yet.
- **Verified:** 11 engine tests through the reference frontend (order,
  the stand-in, stepping, moving, closing, splits, killing, `ui.tab`, the
  capability and the option, projects, save-as); 6 checks in `npm run
  smoke` with real clicks and keys in the Electron window (the strip and
  where it is, a click, the unsaved mark, dragging to reorder, the close
  button, a middle click, `s-w`, a strip per window, hiding). A picture of
  the strip was saved and looked at.
- **Not verified:** the strip with more tabs than fit, and the look in a
  visible window. Nothing was tried on Linux or Windows, where `s-w` and
  the others need the Super key.

### W-4: the file tree

Built as section 5.4 describes, with these details.

- **Where things are.** Engine: `neomacs/filetree.py` (listings, and
  saying when they are stale), `neomacs/git.py` (what git says has
  changed), `neomacs/trash.py`, the commands in `neomacs/std/filetree.py`,
  `files.list` in `neomacs/rpc/ui.py`. Electron:
  `frontend/src/renderer/filetree.ts`.
- **Commands.** `files.new_file`, `files.new_folder`, `files.rename`,
  `files.delete`, `files.copy_path`, `files.refresh`, `view.focus_files`.
  Each takes the path it is about, which is how the tree runs them; run by
  name they are about the buffer's file or the directory it is in. Names
  are asked for in the prompt area, not typed into the tree.
- **Renaming** takes along the buffers visiting the file, or files under a
  directory: they keep their text, unsaved changes included, and take the
  new name. **Deleting** kills the buffers visiting what went, but keeps
  any with unsaved changes.
- **The trash** is `~/.Trash` on macOS and the freedesktop.org home trash
  elsewhere on POSIX. A file trashed on macOS this way has no "Put Back" in
  the Finder. Windows has no trash that can be reached without its shell:
  there the question is "Delete ... for good?".
- **Git state** comes from `git status --porcelain=v2`, run once for each
  time the listings are said to be stale and shared by every directory
  asked for. Staged and untracked files share one colour (a new face,
  `added`); a directory shows a dot in the colour of the most pressing
  state inside it.
- **Only a project's own directories can be listed:** a path with `..` in
  it, or an absolute one, is refused. Links are followed.
- **A directory is cut short at 5000 entries,** and nothing says so.
- **A click never gives the tree the keyboard:** it opens the file or the
  folder and the text keeps it, as with every other click on the frame.
  `view.focus_files` (`s-E`) gives the tree the keyboard; Escape, Return
  on a file, any key with a modifier, or a press of the mouse gives it
  back. While the tree has it the engine is not told of bare keys.
- **Which folders are open** is kept by the frontend for each project,
  for as long as the window lives. It is not the engine's yet, so W-6 has
  nothing of it to restore.
- **Stale** is said after a save, after a file command, when the window
  gets the keyboard back, and by `files.refresh`. Not after a shell
  command, nor a git one: there are none of those yet (W-5).
- **Double-click to keep a tab** is not built: there are no preview tabs.
- **Verified:** 12 engine tests (the order of a listing, what may be
  listed, git state against a real repository made with `git`, a project
  below the top of its repository, reading git's output, when stale is
  said, each command against a temporary directory and a trash of the
  test's own); 6 checks in `npm run smoke` (the tree and where it is,
  reading again, colours, a click on a folder and on a file, the keyboard
  in the tree with the engine hearing none of it, a tree for each
  project, the new-file button). A picture was saved and looked at.
- **Not verified:** the context menu, which is native and cannot be driven
  off screen; the real trash on any system (the tests use a directory of
  their own); the look in a visible window; anything on Linux or Windows.
