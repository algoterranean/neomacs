# Engine core design

Status: draft 19, 2026-11-27. Nothing is implemented. The decisions in
section 2 are confirmed; section 16 lists what was deferred.

This describes the backend ("engine") of Neomacs, a new, Python-native
editor. Despite the name it is not a modification of Emacs and shares no
code with it. Emacs is a source of ideas and reference material only.
The project and its Python package are named `neomacs`. Companion documents:
FRONTEND.md, EXTENSIONS.md, DISTRIBUTION.md and STABILITY.md. It is MIT licensed, so
no code or documentation text may be copied from Emacs, which is GPL; Emacs
is a source of ideas only.

## 1. Goals and non-goals

Goals:

- The editor is a live Python image. Any definition (command, mode, key
  binding, core function) can be re-evaluated from a buffer and takes effect
  immediately, without restart, including for code Emacs-style "held" by
  keymaps, hooks and timers.
- No private core. The engine is written in the same Python, against the same
  API, that users extend. Every command is inspectable and its source is one
  jump away.
- The engine owns all editor state. The UI is a separate process that lays
  out and draws the content it is given, forwards input, and answers
  questions about geometry, over a Neovim-style RPC protocol.
- Rich display: proportional fonts, mixed font sizes, inline images.
- A slow command never freezes the editor.
- The Emacs data model is kept where it is proven: buffers, markers, layered
  keymaps with prefix keys, major and minor modes, buffer-local options,
  hooks, a minibuffer with pluggable completion, text annotations.

Non-goals (for this document):

- Running Elisp, or API compatibility with Emacs.
- Macros or any change to Python syntax.
- Sandboxing extensions. Extension code is fully trusted, as in Emacs.
- Collaborative editing, remote/multi-user operation.
- The Electron frontend's internals. Only the protocol it must speak is
  specified here (section 4); the frontend gets its own document.

## 2. Decisions taken

| # | Decision | Reason |
|---|---|---|
| D1 | One engine process. All editor state is read and written by one *owner* thread at a time. Normally that is the main thread, running an asyncio loop; the only hand-over is to the debug loop while the main thread is paused at a breakpoint (section 10). | Same model as Emacs: no locking, and evaluated code sees a consistent world. |
| D2 | Commands are coroutines (`async def`) by default and run as asyncio tasks. Foreground commands run one at a time, in input order; a command waiting for user input gives up its turn; a command may be declared background. Text mutation is synchronous and never awaits. | A slow command must not freeze the editor, but keys typed ahead must still apply in order to a consistent state. |
| D3 | No nested event loop. A prompt is an `await`. | Follows from D2; replaces Emacs's recursive edit. |
| D4 | Transport is JSON-RPC 2.0, one message per line, over stdio or a Unix socket. | Readable on the wire while the protocol is still changing, and the same machinery serves the LSP client. The codec sits behind the transport and can be replaced. |
| D5 | The frontend owns layout and measurement: fonts, wrapping, line heights, images, scrolling. The engine sends logical lines of styled runs and inline objects, never sees pixels, and asks the frontend when a command needs geometry. | Required for proportional fonts, mixed sizes and inline images. Only the frontend has a text shaping and rendering stack. |
| D6 | Buffer positions are 0-based code point offsets; ranges are half-open, like Python slices. | Python convention. Avoids a permanent off-by-one against `str`. |
| D7 | No implicit "current buffer and point" in the text API. Edits take explicit positions, or go through a `Cursor` object. The selected window, its buffer and its selections are available as context for commands. | Explicit is the Python norm, and removes the need for `save-excursion`. |
| D8 | Keymaps, hooks and timers refer to commands by name where possible, and re-evaluation patches function and class objects in place. | Two independent mechanisms, so stale references are rare (section 9). |
| D9 | A reference terminal frontend, written in Python, is built before the Electron one. It does its own monospace layout and degrades rich content. | Every stage is runnable early, and the protocol is exercised by two clients. |
| D10 | Text storage sits behind a small interface; the first implementation is pure Python. | Lets it be replaced by a native rope if profiling requires. |
| D11 | Several frontends may be attached to one engine at once. Each is a *frame* with its own window tree, minibuffers, focused view and selections; buffers and everything else are shared. | Same model as Emacs frames. Needed anyway for the terminal and Electron frontends to coexist. |
| D12 | Line numbers are 1-based everywhere: the Python API, the protocol and the display. Offsets within a line, like buffer positions, are 0-based. | Matches what the user sees, tracebacks and `ast`. One convention on both sides of the protocol. |
| D13 | Runs on the most recent stable CPython, free-threaded build, as of the start of stage 0, and no older than 3.15. | Worker threads run Python in parallel. Does not change D1: editor state still belongs to one owner thread. 3.15 is the floor because it provides grapheme segmentation (D20). |
| D14 | Command names are flat, with a dotted prefix by convention. Undo is linear with a redo stack. State that survives re-evaluation is declared with an explicit `keep(...)`. | Simplest option in each case; none blocks a later change. |
| D15 | MIT licence. | |
| D16 | A window holds a list of selections, each an anchor and a head; the head is the cursor. Multiple cursors are part of the model from the start. There is no Emacs-style mark: the selection and the jump list are separate things, and there is no inactive region. | Retrofitting multiple cursors means revisiting every command. Splitting the mark's two jobs removes its confusing inactive state. |
| D17 | The system clipboard belongs to the frontend and the kill ring to the engine; the protocol carries clipboard text both ways. | Only the frontend can reach the OS clipboard, and with several frontends each has its own. |
| D18 | The debugger pauses the main thread inside the paused frame and services the editor from a second event loop on another thread. | The paused code's stack lives on the main thread, and asyncio cannot run a nested loop there. Uses only public APIs. |
| D19 | A frame survives its connection. A frontend can resynchronise on a live connection or reattach to its frame on a new one, and receives the full state. | An Electron reload or a dropped socket must not lose window layout, cursors or a pending prompt. |
| D20 | Positions stay in code points, but everything the user perceives as a character is a grapheme cluster: character motion, deletion, selection ends, displayed columns. Segmentation is `unicodedata.iter_graphemes` from the standard library. | An emoji, a flag or an accented letter can be several code points. The standard library does the segmentation, so there is no dependency. |
| D21 | Syntax information has two layers behind one interface, `buf.syntax`: a small declarative table that every mode has, and tree-sitter where a grammar loads. Tree-sitter is optional; every command works without it. | The table covers what a parse tree cannot (words, what to insert, buffers with no grammar) and what it gets wrong (the line being typed). Tree-sitter does not currently load cleanly on the free-threaded build. |
| D22 | The gutter is a list of columns per window. Marks in it travel with each line; line numbers are drawn by the frontend. Margins and widths are given in ems of the default face. Tabs are sent as tab characters, and tab stops are multiples of the default face's space width. | The engine has no pixels (D5), so everything it specifies about horizontal space must be in font-relative units the frontend can resolve. |
| D23 | Display updates are paced per frame: at most one unacknowledged `redraw` batch is in flight, and while one is outstanding the engine only marks things dirty. Intermediate states are never generated. Input is never dropped. | A slow frontend must make the display skip ahead, not fall behind. Skipping generation is safe where discarding messages is not, because `lines` events are splices. |
| D24 | A minibuffer is any text input that is not a document buffer. It behaves exactly as a buffer does (editing commands, selections, keymaps, modes, undo) and differs only in where it is shown: a frame's prompt area, inline within a buffer, or another part of the UI. It may be single-line or multi-line. Windows and minibuffers are both *views*. | One editing model everywhere text is typed. A prompt, a search field and a commit message box are the same thing in different places. |
| D25 | Prefix arguments are supported: a numeric or universal argument typed before a command, delivered through the command's declared parameters. | An Emacs feature worth keeping; cheap once commands declare their parameters. |
| D26 | Where a buffer appears is decided by role. A command says what kind of thing it is showing; a table maps roles to places. Places are the main window tree and *panels*: containers that are docked or floating and hold several views as tabs. | Replaces Emacs's display rules. Each case has a default, no command hardcodes a location, and a user changes placement in one table. |
| D27 | Code always has a frame if any is attached. A command's view, window and buffer are pinned when it starts; everywhere else they are looked up from the frame at the moment of use. | Timers, tasks and callbacks outlive the window they started in. A pinned window can go stale; a frame rarely does. |
| D28 | The application menu is defined in the engine and sent to the frontend. | Extensions add items through the same registry as everything else, and labels show the keys actually bound. |
| D29 | One engine per app instance, started by and exiting with it. | See DISTRIBUTION.md P8. Settles who starts the engine; what happens to unsaved buffers on exit is still deferred. |
| D30 | Every registration records the module that made it. | Needed to disable, reload and upgrade extensions in a running editor (EXTENSIONS.md X5). |
| D31 | Options have a project layer between buffer-local and mode default, filled from a project's `.neomacs/settings.toml`. Only options declared with `project=True` can be set there. | Per-project formatting settings without running project code (EXTENSIONS.md X13). |

## 3. Process model

```
 Electron (or TUI)                         Engine (python -m neomacs)
┌──────────────────┐    JSON-RPC     ┌───────────────────────────────────┐
│ lay out and draw │◄── redraw ──────│ reader thread ─► input queue      │
│ translate keys   │─── input ──────►│                    │              │
│ own scrolling    │─── interrupt ──►│ main thread: asyncio loop         │
│ answer geometry  │◄── layout.* ───►│   dispatcher ─► command tasks     │
└──────────────────┘                 │   view updates, timers, processes │
                                     └───────────────────────────────────┘
```

- **Main thread.** Runs the asyncio loop. Command tasks, timers, subprocess
  callbacks and RPC handlers all run here and can interleave only at an
  `await`.
- **Reader thread.** Reads and decodes frames from the transport and posts
  them to the loop with `call_soon_threadsafe`. It exists so that an
  interrupt can be seen while the main thread is computing.
- **Worker threads.** `await ed.in_thread(fn, *args)` runs CPU-heavy work in a
  pool. Worker code must not touch editor state; in debug builds every buffer
  mutator asserts it is on the owner thread (D1).

**Cancelling (the `C-g` path).** The UI sends `C-g` as a normal key and, in
addition, an out-of-band `input.interrupt` notification. There are two
levels:

1. The reader thread posts a cancel request to the loop. If a foreground
   command is running, its task is cancelled, which raises `CancelledError`
   at the `await` it is suspended on; queued input is discarded and "Quit" is
   reported. If no foreground command is running, the request is dropped and
   the `C-g` key is dispatched normally to `keyboard_quit`.
2. If the loop has not picked up the request within 200 ms, a command is
   computing without awaiting. The reader thread then calls
   `_thread.interrupt_main()`, which raises `KeyboardInterrupt` in whatever
   is running. The task runner treats it as a cancel.

`C-g` does not cancel background tasks; they are listed and cancelled
individually (section 7.1).

**Error isolation.** Every command, hook function, timer callback and RPC
handler is called through one wrapper that catches `Exception`, logs the
traceback to the `*messages*` buffer and shows a one-line summary as a message
(7.5). Nothing a user evaluates may take the engine down.

## 4. RPC protocol

### 4.1 Framing and transport

JSON-RPC 2.0, three message shapes:

```
request       {"jsonrpc": "2.0", "id": 7, "method": "engine.eval", "params": ["1 + 1"]}
response      {"jsonrpc": "2.0", "id": 7, "result": "2"}
              {"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "...", "data": {...}}}
notification  {"jsonrpc": "2.0", "method": "input.key", "params": ["C-x"]}
```

Framing is newline-delimited: each message is one line of UTF-8 JSON with no
raw newline inside it, terminated by `\n`. Parameters are positional arrays.
Either side may send requests (section 4.4), so each side numbers its own
ids. An error's `data` carries the Python exception type and traceback.

Integers on the wire stay below 2^53 so that a JavaScript frontend reads
them exactly; buffer positions and ticks are far inside that range.

Transports: stdio (the frontend spawns `python -m neomacs --stdio`), or a Unix
socket created with mode 0600 (`python -m neomacs --listen PATH`), which lets a
frontend, a test client or a script attach to a running engine. There is no
TCP listener by default, because the protocol can evaluate arbitrary code.

When it starts with `--listen` the engine prints one line,
`NEOMACS-LISTENING <path>`, on standard output once the socket is ready; a
parent process waits for that line before connecting. With
`--exit-with-parent` the engine exits when the process that started it goes
away.

Setting `NEOMACS_RPC_LOG=path` copies every message, with a direction and a
timestamp, to a file for debugging.

### 4.2 Client to engine

| Method | Kind | Purpose |
|---|---|---|
| `engine.api_info()` | request | Protocol version and the list of methods, generated from the Python signatures and docstrings. |
| `engine.eval(source, mode="auto")` | request | Evaluate Python in the scratch module; returns `repr` or an error. Used by scripts and tests. |
| `engine.call(command, args)` | request | Run a named command. |
| `engine.exit()` | notification | Ask the engine to exit. Sent by the app when it quits (DISTRIBUTION.md section 7). |
| `engine.idle(timeout)` | request | Returns once all input has been handled and every responsive frame is up to date. For tests and scripts. |
| `ui.attach(options)` | request | Carries the frontend's protocol version and capabilities, which must be compatible (STABILITY.md section 5). Create a frame for this connection and start receiving `redraw` for it. Options name the UI's capabilities, e.g. `{"rich_text": true, "images": true}`. Returns the frame id and a reattach token. With `{"frame": id, "token": ...}` it reattaches to an existing frame instead (4.6). |
| `ui.resync()` | request | Ask for the full state again on this connection (4.6). |
| `ui.detach()` | notification | Stop redraw and delete the frame. |
| `view.viewport(win, first_line, last_line, page_lines)` | notification | The lines now visible in a window, after the UI scrolled, resized or re-wrapped. The engine makes sure the UI holds those lines plus a margin. |
| `view.split_resized(split, ratios)` | notification | The user dragged a divider. |
| `ui.panel(panel, action, arg)` | notification | The user acted on a panel in the frontend (4.3). |
| `view.ack(seq)` | notification | The batch whose `flush` carried `seq` has been applied (4.8). |
| `input.key(key)` | notification | One key in canonical notation (4.7). |
| `input.text(text)` | notification | Committed text from an input method. |
| `input.paste(text, which)` | notification | A paste the frontend started itself: a menu item, a drop, a middle click. Handled by the `paste` command as one undo step. |
| `input.mouse(button, action, clicks, mods, win, area, line, offset)` | notification | Mouse event. `action` is `press`, `release` or `drag`. `clicks` is 1, 2 or 3, counted by the UI, which knows the system's double-click interval; it becomes `<double-mouse-1>` and `<triple-mouse-1>`. The UI does the hit test and reports where it landed, not pixels: `area` is `"text"`, `"modeline"`, or `"gutter:<column>"`, with the buffer line and, in text, the offset. The engine turns it into a key such as `<mouse-1>`, `M-<drag-mouse-1>` or `<gutter-mouse-1>` and looks it up in the keymaps like any other, so click, drag, add-cursor-on-click and toggling a breakpoint in the gutter are ordinary commands. |
| `input.drop(paths, win, line, offset)` | notification | Files were dropped on a view. Handled by the `drop` command. |
| `input.focus(gained)` | notification | Focus change. |
| `input.interrupt()` | notification | Out-of-band cancel, handled on the reader thread. |

### 4.3 Engine to client: redraw

Wherever an event or request below takes `win`, the id is that of a *view*:
a window or a minibuffer (7.5). Both are drawn and addressed the same way.

One notification, `redraw`, whose parameter is a list of events applied in
order. The UI repaints only on `flush`, and then acknowledges the batch (4.8).

The unit of display is the **logical line**: one buffer line, however many
visual rows it wraps to. The engine sends what each line contains; the UI
decides how it looks on screen.

| Event | Meaning |
|---|---|
| `["face_define", id, attrs]` | `attrs` may have `family`, `size` (a multiple of the base size, or points), `weight`, `slant`, `fg`, `bg`, `underline`, `strike`, `box`, `line_height`. Ids are small integers; 0 is the default face. |
| `["resource_define", id, kind, source]` | An image or other binary resource. `source` is `{"path": ...}` when the UI can read the file itself, otherwise `{"mime": ..., "data": base64}`. Local frontends should always get a path. |
| `["layout", {"main": tree, "panels": panels}]` | `tree` is the window tree: `["split", id, "h" or "v", ratios, children]` and `["win", id]`. The UI turns ratios into pixels. `panels` is described below. |
| `["win_info", win, info]` | Per-window facts the UI needs: `line_count` (for the scroll bar), `wrap` mode, buffer name, and the presentation settings described below: `gutter`, `margins`, `max_width`, `tab_width`. |
| `["lines", win, first, removed, lines]` | Splice: replace `removed` lines starting at line `first` with `lines`, which is either a list of lines or, when the frontend is not to hold them, just a count. See "What a frontend holds" below. |
| `["held", win, first, last]` | Keep only lines `first` to `last` of what is held and forget the rest; `first` greater than `last` means forget everything. Sent when the view shows another buffer, jumps far away, or is holding far more than it needs. |
| `["selections", win, primary, ranges, shape]` | Every selection in the window as `[anchor_line, anchor_offset, head_line, head_offset]`, in buffer order. The UI draws a cursor of the given shape (`block`, `bar`, `hollow`, `hidden`) at each head and highlights from anchor to head. `primary` is the index of the primary selection. Sent separately from `lines`, so moving or selecting does not re-send text. |
| `["reveal", win, line, offset, align]` | Scroll so the position, normally the primary cursor, is visible. `align` is `nearest`, `center`, `top` or `bottom`. |
| `["modeline", win, runs]` | The window's status line. |
| `["popup_show", items, selected, win, line, offset]`, `["popup_hide"]` | A completion or menu popup anchored to a buffer position. The UI draws and places it. |
| `["minibuffer_show", view, host, options]`, `["minibuffer_hide", view]` | Display or remove a minibuffer that is not inline. `host` is `{"kind": "prompt"}` for the frame's prompt area, or `{"kind": "place", "name": ...}` for a named part of the UI. `options` has `label` (runs), `multiline`, `min_lines`, `max_lines`, `placeholder`, `masked`. Its text arrives through `lines` like any view's. |
| `["menu", items]` | The application menu (8.6): a tree of `{"label", "command", "key", "enabled", "children"}`. Choosing an item sends `engine.call` with its command. Sent on attach and whenever it changes. |
| `["focus", view]` | Which view has keyboard focus. |
| `["message", runs, kind]` | A transient message for the user. `kind` is `info`, `warning`, `error`, or `keys` for a pending key sequence or prefix argument. The UI chooses where to show it. |
| `["title", text]`, `["bell"]`, `["busy", bool]` | Window title, alert, busy indicator. |
| `["reset"]` | Discard everything held for this frame: faces, resources, layout, lines. First event of a full state sync (4.6). |
| `["ui_faces", names]` | The face ids for what the frontend draws on its own account: `selection`, `cursor`, `gutter`, `placeholder`, `modeline`. Sent on attach and when the theme changes. |
| `["flush", seq]` | End of batch. `seq` increases by one per batch, per frame. |

A line is `[attrs, runs]`. `attrs` holds paragraph-level properties
(`align`, `indent`, `spacing_before`, `spacing_after`) and the line's gutter
marks (`gutter`, below). Each run is one of:

- `[offset, text, face_id]`: text. `offset` is where the run's text starts
  within the buffer line, or `null` for virtual text that is not in the
  buffer. The UI needs it to map a click back to a buffer position, because
  invisible text is simply left out of the runs.
- `[offset, object]`: an inline object standing for the buffer text at
  `offset`, e.g. `{"image": resource_id, "width": ..., "height": ...,
  "alt": ..., "covers": n}`, or `{"minibuffer": view_id, "covers": n}` for a
  minibuffer embedded in the text (7.5).

The engine keeps every selection end on a grapheme cluster boundary (5.8),
so a cursor is never placed inside a cluster.

All offsets are in code points. A JavaScript frontend must convert to and
from UTF-16 indices.

**What a frontend holds.** For each view the frontend holds one contiguous
range of lines, and the engine always knows which. Three rules keep the two
in step, and they are all a frontend needs to implement:

- A `lines` event carrying a list touches the held range or lies inside it,
  never leaving a gap. Afterwards the frontend holds the new lines together
  with whatever it held on either side of the replaced range. The engine
  uses this both for edits and to supply lines the frontend has not seen: a
  fill replaces existing, unheld lines next to the held range with their
  content, so `removed` equals the number of lines sent. When nothing is
  held, the list becomes the held range.
- A `lines` event carrying a count never overlaps the held range. If it
  lies above, the held lines keep their content and are renumbered by the
  difference; if below, nothing held changes.
- `held` shrinks the range.

An edit that would insert more than a few hundred lines into the held range
is not sent as a list. The engine first shrinks the held range to end just
above the edit, sends a count, and then fills in whatever the viewport
needs.

The frontend also keeps its scroll position on the same text: when lines are
added or removed above the first visible line, that line's number changes by
the difference and what is on screen does not move.

**Panels.** Each entry of `layout.panels` is:

```
{"id": 3, "name": "bottom", "visible": true,
 "mode": "docked", "side": "bottom", "size": 0.3,
 "tabs": [{"view": 41, "title": "*traceback*"}, {"view": 44, "title": "*messages*"}],
 "active": 41}
```

For a floating panel, `mode` is `"floating"` and in place of `side` there is
an `anchor`: `"center"`, an edge or corner name, or `{"view": id, "line": n,
"offset": k}` to float beside a position in the text. `transient` marks one
that closes when focus leaves it. `size` is a fraction of the frame and only
a starting point; the user resizes panels in the frontend. Each tab's view
receives `lines`, `selections` and the rest like any other view.

When the user acts on a panel directly, the frontend reports it and the
engine, which stays the authority, answers with a new `layout`:
`ui.panel(panel, action, arg)`, where `action` is `activate_tab`,
`close_tab`, `hide`, `dock` (with a side) or `float`.

**Gutter.** The gutter is the strip beside the text that holds per-line
information. `win_info.gutter` is an ordered list of columns:

```
"gutter": [
  {"id": "signs",   "kind": "marks",   "side": "left", "width": 1},
  {"id": "numbers", "kind": "numbers", "side": "left", "mode": "absolute"},
  {"id": "fold",    "kind": "marks",   "side": "left", "width": 1}
]
```

- A `numbers` column is drawn entirely by the frontend, which already knows
  each line's number and, from `selections`, the primary cursor's line.
  `mode` is `absolute` or `relative`. Scrolling and cursor motion therefore
  re-send nothing.
- A `marks` column shows what the engine puts in it. A line's
  `attrs.gutter` maps a column id to `[text, face_id]`, for example
  `{"signs": ["●", 12]}`. `width` is in ems. A mark can also name an image
  resource: `[{"image": resource_id}, face_id]`.
- `side` may be `right`.
- A wrapped line shows its gutter content on its first visual row only.

On the engine side, marks come from spans that carry a `gutter` property
(column, text, face, priority) and from decoration providers. If several
compete for one column on one line, the highest priority wins. Which columns
a window has is the buffer-local option `gutter_columns`; `line_numbers` is
`off`, `absolute` or `relative`.

**Margins.** `win_info.margins` is `{"left": n, "right": n}`: blank space
between the window edge and the gutter or text. `win_info.max_width`, if
set, caps the width of the text column and centres it. All three are in ems
of the default face, because the engine does not know pixel sizes.

**Tab stops.** The engine sends a tab as a tab character inside a text run
and never expands it. `win_info.tab_width` is the buffer's `tab_width`
option. The frontend places tab stops at multiples of `tab_width` times the
width of a space in the window's default face, measured from the left edge
of the text column, and a tab advances to the next stop. With a monospaced
default face this is the usual behaviour. With a proportional one, leading
tabs still align with each other, though not with runs of spaces.

Where the engine itself needs an indentation width, for the indentation
commands, `buf.indentation(line)` counts the line's leading whitespace with a
tab advancing to the next multiple of `tab_width` columns. That is a property
of the text, defined without reference to layout, and is only ever applied to
leading whitespace.

There is no echo area. Messages are `message` events, and prompts are
minibuffers. Scrolling belongs to the UI: it scrolls on its own for wheel
and scroll bar input and tells the engine with `view.viewport`; the engine
scrolls by sending `reveal`.

### 4.4 Engine to client: geometry requests

The protocol is bidirectional. When a command needs to know something only
layout can answer, the engine sends a request and awaits the reply. The
engine sends any pending `redraw` first, and messages are ordered, so the UI
always answers against the same text the engine has.

| Method | Returns |
|---|---|
| `layout.move(win, positions, unit, count)` | For each `[line, offset, goal_x]` in `positions`, the position `count` units away, where `unit` is `visual_line` or `page`, and the horizontal goal to pass next time. Takes a list so that vertical motion with many cursors is one round trip. |
| `layout.line_edge(win, line, offset, which)` | The start or end of the visual row containing the position. |
| `layout.viewport(win)` | First and last visible line and lines per page, when the engine cannot wait for the next `view.viewport`. |
| `layout.rect(win, line, offset)` | Pixel rectangle of a position, for the rare extension that needs it. |

Because commands are coroutines (D2), a geometry request is just an `await`
and costs one local round trip.

### 4.5 Clipboard

`which` is `"clipboard"`, or `"primary"` for the X11 selection.

| Method | Direction | Kind | Purpose |
|---|---|---|---|
| `clipboard.set(which, content)` | engine to client | notification | Put this on the system clipboard. |
| `clipboard.get(which, formats)` | engine to client | request | Return the current system clipboard in the first available of `formats`. |
| `input.paste(text, which)` | client to engine | notification | See 4.2. |

`content` and the reply are `{"text": ...}`. The shape leaves room for
`html` and `image` entries later; `formats` is `["text"]` for now.

The engine never caches the system clipboard. It asks at the moment of a
yank, which is one awaited round trip, because a frontend cannot reliably
learn that another application has copied something. Section 5.7 says how
this meets the kill ring.

### 4.6 Resync and reattach

A frame outlives the connection that created it (D19).

- **Resync.** `ui.resync()` on a live connection makes the engine send a full
  state sync. This is for a frontend that has lost its own state, such as an
  Electron renderer reloading while the main process keeps the pipe open, or
  one that received a `lines` splice it could not apply.
- **Detach.** When a connection closes without `ui.detach`, its frame becomes
  detached: it keeps its window tree, selections, jump lists and minibuffers,
  and its buffers keep receiving edits. Requests the engine was awaiting from
  that frontend fail with `FrontendGone`, raised in the awaiting command. A
  command waiting at a prompt in that frame stays waiting.
- **Reattach.** A new connection calls `ui.attach` with the frame id and
  token. The engine binds the frame to it and sends a full state sync.
  Reattaching needs the socket transport; a stdio connection cannot be
  reopened.
- **Expiry.** A frame detached for longer than the `frame_detach_timeout`
  option (default 10 minutes) is deleted and its pending prompts are
  cancelled.

A **full state sync** is one `redraw` batch: `reset`; every `face_define` and
`resource_define` in use; `layout`; for each window `win_info`, `selections`,
`modeline`, and a `reveal` of the first line it was last showing; `title`;
`flush`. Lines are not included. The frontend lays out, reports its viewports
with `view.viewport`, and lines arrive as they would after any scroll.

### 4.7 Key notation

Emacs notation, because it is compact and already familiar: `a`, `C-x`,
`<space>`,
`M-f`, `C-M-s`, `<return>`, `<f5>`, `S-<left>`. Modifier order is fixed
(`C-M-S-s-`). The frontend is responsible for turning platform key events
into this form; `neomacs.keys.parse("C-x C-f")` turns a string into a key
sequence on the engine side.

### 4.8 Pacing and backpressure

A frontend that paints slower than the engine changes things must see the
display skip ahead, not lag (D23).

**Why messages cannot be discarded.** Some events replace state, and for
those only the latest matters. A `lines` event is a splice relative to what
the frontend already holds; lose one and every later line number is wrong.
So nothing already sent may be dropped. What can be skipped is generating an
update in the first place: an update is computed as the difference between
what the frame was last sent and the current state (8.3), so the next one is
correct however many were skipped.

**The rule.**

1. Every batch ends with `["flush", seq]`.
2. The frontend sends `view.ack(seq)` when it has applied the batch and, if
   its window is visible, painted it. A frontend that is not painting, such
   as a hidden or minimised window, acknowledges as soon as it has applied.
3. The engine keeps at most one unacknowledged batch in flight per frame.
   While one is outstanding, changes only mark state dirty.
4. When the acknowledgement arrives and anything is dirty, the engine sends
   one batch for the current state.

The update rate is therefore whatever the frontend can paint, and pacing is
independent for each frame.

**Sent regardless of pacing:**

- `clipboard.set`, `bell`, and any request awaiting a reply.
- The pending update before a geometry request (4.4). The frontend needs the
  current text to answer, so the engine sends a batch even if one is
  outstanding. It then has two in flight and waits for both acknowledgements
  before sending another.

**Input.** `input.key`, `input.text` and `input.paste` are never dropped or
reordered. `view.viewport`, `view.split_resized` and mouse-drag positions are
"latest wins": when several are queued for the same window, the engine acts
only on the newest.

**Writing never blocks the engine.** Outgoing messages go to a queue drained
by a writer; the main loop does not wait on the socket. With pacing the queue
normally holds at most a batch and a few one-off messages.

**A stalled frontend.** If a batch is unacknowledged for 2 seconds the frame
is marked stalled, which is shown in other frames' modelines. The engine goes
on accumulating dirty state and sending nothing but one-off messages. If the
queue for that connection passes a size limit, the engine closes the
connection; the frame becomes detached and a reattach brings it up to date
with a full state sync (4.6). A full state sync is itself a batch and is
acknowledged like any other.

## 5. Text model

### 5.1 Storage

```python
class TextStore(Protocol):
    def __len__(self) -> int: ...  # code points
    def slice(self, start: int, end: int) -> str: ...
    def replace(self, start: int, end: int, text: str) -> None: ...
    def line_count(self) -> int: ...
    def line_start(self, line: int) -> int: ...  # 1-based line -> position
    def line_of(self, pos: int) -> int: ...
    def snapshot(self) -> str: ...  # whole text, cached until next edit
    def byte_of(self, pos: int) -> int: ...  # UTF-8 byte offset, for tree-sitter
    def pos_of_byte(self, byte: int) -> int: ...
```

The first implementation is a list of string chunks (target 2 to 4 thousand
characters each) with parallel arrays of chunk lengths, newline counts and
UTF-8 byte lengths,
and prefix sums rebuilt lazily after an edit. An edit touches one chunk and
splits or merges as needed. `snapshot()` exists because Python's `re` needs a
`str`; joining a few megabytes is cheap, and the result is cached until the
next edit.

This is adequate for source files. Files of hundreds of megabytes are out of
scope until the store is replaced.

### 5.2 Buffer

```python
class Buffer:
    name: str
    file: Path | None
    store: TextStore
    tick: int  # incremented on every change
    saved_tick: int  # tick at last save; modified = tick != saved_tick
    read_only: bool
    mode: Mode  # major mode instance
    minor_modes: list[Mode]
    options: OptionView  # buffer-local option values (section 6)
    locals: dict[str, Any]  # free-form per-buffer state for extensions
    undo: UndoLog

    def __len__(self) -> int: ...
    def __getitem__(self, s: slice) -> str: ...  # buf[a:b]
    def insert(self, pos: int, text: str) -> None: ...
    def delete(self, start: int, end: int) -> str: ...
    def replace(self, start: int, end: int, text: str) -> None: ...
    def marker(self, pos: int, *, advance: bool = False) -> Marker: ...
    def cursor(self, pos: int = 0) -> Cursor: ...
    def search(
        self,
        pattern: str | re.Pattern,
        start: int = 0,
        end: int | None = None,
        *,
        backward: bool = False,
    ) -> re.Match | None: ...
```

All mutation funnels through `replace`, which, in order: checks `read_only`,
runs `before_change`, records the undo entry, edits the store, adjusts
markers and spans, bumps `tick`, records the changed line range for the
renderer and for incremental parsers, and runs `after_change(start, old_end,
new_end)`.

### 5.3 Markers and cursors

A `Marker` is a position that moves with the text. `advance` says which way
it goes when text is inserted exactly at it. Markers live in a per-buffer
list and are adjusted on every edit; the buffer holds them weakly so
abandoned markers disappear.

A `Cursor` is a marker with movement and editing methods. It replaces the
Emacs idea of point:

```python
cur = buf.cursor(0)
cur.forward_word()
cur.forward_char()  # one grapheme cluster (5.8), not one code point
cur.insert("hello ")  # inserts at the cursor and moves past it
cur.line, cur.column  # derived; column counts clusters from the line start
cur.offset  # code points from the line start, as in the protocol
```

A `Cursor` is the low-level tool for code that works on a buffer directly.
What the user sees and commands act on are a window's selections (5.7).

### 5.4 Annotations

Two mechanisms, deliberately separate:

- **Spans** are stored ranges that track edits, grouped into named layers
  (the equivalent of Emacs overlays and Neovim extmarks). A span has start
  and end markers, a priority and a property dict. Recognised properties
  include `face`, `invisible`, `read_only`, `keymap`, `help`, and
  `before_text`/`after_text` for virtual text, `display` to show an inline
  object such as an image in place of the text, `gutter` to put a mark in a
  gutter column on the span's first line (4.3), and `line` for
  paragraph-level attributes. Used for
  search matches, diagnostics, clickable text. Stored in a sorted list per
  layer at first; an interval tree if profiling asks for it.
- **Decoration providers** compute faces on demand for a range of lines and
  are not stored as spans. Syntax highlighting is a provider. The renderer
  calls `provider.decorate(buf, first_line, last_line)` for visible lines
  only and caches results per line until the line, or parser state before
  it, changes.

There are no Emacs-style text properties that travel with copied text in the
first version.

### 5.5 Undo

Each change appends `(pos, removed_text, inserted_text)` to
the buffer's log, tagged with the command invocation that made it. A group
is one invocation's changes to the buffer (section 7.1); consecutive single-character inserts are merged into one group up to a size
limit. A group also records the window's selections before and after, so
undo and redo put every cursor back. The model is linear undo with a redo
stack that is discarded by a new edit.

### 5.6 Files

Reading: bytes are decoded as UTF-8 (BOM honoured), falling back to Latin-1
with a warning flag on the buffer; the line ending style is recorded and
normalised to `\n`. Writing reverses this, writes to a temporary file in the
same directory and renames it over the target. `before_save` and
`after_save` hooks run around the write.

### 5.7 Selections

A window's view of its buffer is a set of selections (D16). This replaces
point, mark and region.

```python
class Selection:
    anchor: Marker  # the fixed end
    head: Marker  # the moving end; the cursor is drawn here
    goal_x: float | None  # horizontal goal for vertical motion, from layout

    start, end, empty, text  # derived; start <= end whichever way it faces

    def move_to(self, pos: int) -> None: ...  # move the head (see "Extending")
    def forward_word(self, n: int = 1) -> None: ...  # and the other Cursor motions
    def select(self, anchor: int, head: int) -> None: ...
    def collapse(self) -> None: ...  # anchor := head
    def replace(self, text: str) -> None: ...  # replace selected text, collapse after it
    def insert(self, text: str) -> None: ...  # same as replace
    def delete(self) -> str: ...


class SelectionSet:  # win.selections
    primary: Selection
    extending: bool

    def __iter__(self) -> Iterator[Selection]: ...  # in buffer order
    def __len__(self) -> int: ...
    def add(self, head: int, anchor: int | None = None) -> Selection: ...
    def remove(self, sel: Selection) -> None: ...
    def set(self, ranges: list[tuple[int, int]], primary: int = 0) -> None: ...
    def keep_primary(self) -> None: ...
    def restore_last(self) -> None: ...
```

- **One or many.** A window always has at least one selection. With one, the
  editor behaves like any single-cursor editor. `ed.window.cursor` is
  shorthand for the primary selection's head.
- **Primary.** The primary selection is the one the viewport follows, the one
  `keymap` spans are looked up at, and the one that remains after
  `keep_primary()`.
- **Extending.** Motion methods move the head. The anchor follows it, giving
  an empty selection, unless the set is extending. `set_anchor` (the
  equivalent of setting the mark) turns extending on; any edit, or
  `keyboard_quit`, turns it off. A shifted motion key extends for that one
  command. There is no selection that exists but is not shown.
- **Normalisation.** After every command each end is snapped to a grapheme
  cluster boundary (5.8), the set is sorted, and selections
  that overlap, and empty ones at the same position, are merged. If the
  primary is merged away, the merged selection becomes primary.
- **Editing at several cursors.** Anchors and heads are markers, so an edit
  at one selection moves the others correctly and commands need no
  bookkeeping of their own.
- **Last selection.** When a non-empty set collapses, its ranges are kept so
  `restore_last()` can bring them back.
- **Layout-dependent motion.** Moving by visual row or by page is an
  awaitable method on the window, `await ed.window.move_visual(1)`, which
  sends every selection to the frontend in one `layout.move` request (4.4).

**Kill ring and clipboard.** The kill ring is global to the engine and shared
by all frames. Killing or copying with several selections pushes one entry
holding one piece per selection, and sends the pieces joined by newlines to
the invoking frame's frontend with `clipboard.set`.

`yank` first awaits `clipboard.get` from the invoking frame. If the text
differs from what the engine last sent to that frontend, another application
has copied something, and it is pushed as a new single-piece entry. Then the
top entry is inserted: into the same number of selections as it has pieces,
piece *i* goes to selection *i*; otherwise every selection gets the pieces
joined by newlines. If the frontend does not answer within 500 ms, or the
command has no frame (a timer, a script), the kill ring is used as it is.

**Jump list.** Separately from selections, each window keeps a bounded
history of positions as markers. Commands that move a long way (search,
go to line, start or end of buffer, go to definition) call
`ed.window.jumps.push()` first; `jump_back` and `jump_forward` walk the list.
This is the navigation half of what the Emacs mark ring did.

### 5.8 Characters and grapheme clusters

A Python string is a sequence of code points, and one visible character is
often several: `é` written as a letter plus a combining accent is 2, a flag
is 2, a family emoji is 5. The design keeps code points for everything
internal and uses grapheme clusters wherever the user is involved (D20).

**Unchanged, in code points:** buffer positions, markers, slicing, the text
store, regular expression search, offsets in the protocol. Code that edits a
buffer programmatically may address any code point.

**In clusters:**

| Operation | Rule |
|---|---|
| `forward_char`, `backward_char` | Move to the next or previous cluster boundary. |
| `delete_char`, `delete_backward_char` | Delete one whole cluster. |
| `delete_backward_codepoint` | Delete a single code point, for removing just an accent. A separate command, unbound by default. |
| `transpose_chars` | Swaps two clusters. |
| Selection ends | Snapped to a boundary after every command (5.7). An end inside a cluster moves to the cluster's start, except the far end of a non-empty selection, which moves to the cluster's end, so a partly selected cluster becomes wholly selected. |
| `cur.column`, the modeline column | Count clusters from the start of the line. |
| Mouse positions from the frontend | Snapped the same way; the frontend's idea of a cluster is not trusted. |

**The boundary functions.**

```python
class Buffer:
    def next_boundary(self, pos: int) -> int: ...
    def prev_boundary(self, pos: int) -> int: ...
    def is_boundary(self, pos: int) -> bool: ...
    def clusters(self, start: int, end: int) -> Iterator[tuple[int, int]]: ...
```

All four are built on `unicodedata.iter_graphemes`, applied to the logical
line that contains the position. A cluster never spans a line: files are
normalised to `\n` on reading (5.6), so there is no `\r\n` pair in a buffer.
Segmenting from the start of the line is what makes the answer correct for
sequences that depend on what came before, such as pairs of flag letters.

Two things keep this cheap:

- If the line is ASCII (`str.isascii()`), every position is a boundary and
  nothing is segmented.
- The boundaries of the most recently segmented line are cached per buffer
  and discarded when the buffer's `tick` changes.

A very long line that is not ASCII is segmented in full on each cache miss.
That is accepted for now.

**Words.** Word motion steps over whole clusters and classifies each by its
first code point, using the mode's syntax table (5.9).

### 5.9 Syntax

Commands need to know what a word is, which brackets pair, where strings and
comments are, and what an expression or a function is. That knowledge comes
from two layers behind one interface (D21).

**Layer 1: the syntax table.** A small, declarative description that every
mode has and that needs no parser.

```python
@dataclass(frozen=True)
class Syntax:
    word_chars: str = "_"  # word characters besides Unicode letters and digits
    symbol_chars: str = ""  # further characters allowed in an identifier, e.g. "-"
    brackets: tuple[tuple[str, str], ...] = (("(", ")"), ("[", "]"), ("{", "}"))
    quotes: tuple[str, ...] = ('"', "'")  # string delimiters, longest first
    escape: str | None = "\\"
    line_comment: str | None = None  # e.g. "#"
    block_comment: tuple[str, str] | None = None  # e.g. ("/*", "*/")
    comment_padding: str = " "
    auto_pairs: tuple[tuple[str, str], ...] | None = None  # default: brackets and quotes


class PythonMode(ProgMode):
    syntax = replace(ProgMode.syntax, line_comment="#", quotes=('"""', "'''", '"', "'"))
```

A code point is a word character if `str.isalnum()` says so or it is in
`word_chars`. The table is inherited along the mode hierarchy, and
`FundamentalMode` supplies the defaults above, so the minibuffer, `*messages*`
and unknown file types all have one.

From the table alone a **scanner** answers structural questions. It walks the
text tracking string, comment and bracket state. The state at the start of
each line is cached and invalidated from the first changed line onward, so a
query costs a scan of one line in the usual case. Outward searches for an
enclosing bracket are bounded.

**Layer 2: tree-sitter.** A mode may name a grammar (`grammar = "python"`).
If the tree-sitter binding and that grammar can be imported, the buffer keeps
a parse tree:

- **Incremental.** `after_change` records each edit; the tree is edited and
  reparsed before the next view update or the next syntax query, whichever
  comes first. The first parse of a large file runs in a worker thread on a
  snapshot, and the scanner answers until it finishes.
- **Offsets.** Tree-sitter addresses text in UTF-8 bytes. The text store
  keeps byte lengths per chunk (5.1), and one adapter module converts in both
  directions; nothing else in the engine sees bytes.
- **Queries.** Each language has query files written for this editor,
  `highlights.scm`, `textobjects.scm` and `indents.scm`, using one fixed
  vocabulary of capture names (`@function`, `@class`, `@parameter`,
  `@statement`, `@comment`, `@string`, ...). That vocabulary is what makes
  "function" mean the same thing in every language. Other editors' query
  files are a starting point only: they follow their own conventions, and
  their licences must be checked before anything is copied (D15).
- **Optional.** If the import fails, the mode works from its table and a
  message says so once. Nothing in the standard commands requires a tree.

**The interface.**

```python
class BufferSyntax:  # buf.syntax
    table: Syntax
    tree: Tree | None

    # words: table only
    def word_at(self, pos: int) -> tuple[int, int] | None: ...
    def next_word_end(self, pos: int) -> int: ...
    def prev_word_start(self, pos: int) -> int: ...

    # structure: tree if there is one, otherwise the scanner
    def kind_at(self, pos: int) -> Literal["code", "string", "comment"]: ...
    def matching_bracket(self, pos: int) -> int | None: ...
    def enclosing(self, start: int, end: int) -> tuple[int, int] | None: ...
    def expression_at(self, pos: int, direction: int) -> tuple[int, int] | None: ...
    def indent_for(self, line: int) -> int | None: ...

    # named units: tree only; None without one
    def unit_at(self, pos: int, name: str) -> tuple[int, int] | None: ...
    def units(self, name: str, start: int, end: int) -> Iterator[tuple[int, int]]: ...
```

| Question | With a tree | Without |
|---|---|---|
| Word boundaries | Table | Table |
| In a string or comment? | Node type | Scanner state |
| Matching bracket | Sibling token of the bracket's node | Scanner, skipping strings and comments |
| Enclosing range, for expand-selection | Parent node | Enclosing bracket pair, then the line, then the paragraph |
| Expression forward or backward | Next or previous sibling node | A word, a string, or a bracketed group |
| Function, class, parameter | `textobjects.scm` | None; commands that need it say the mode has no grammar |
| Indentation for a line | `indents.scm` | Previous line's indentation, adjusted by bracket depth; a mode may override |
| Comment delimiters, pairs to auto-close | Table | Table |

**Broken code.** When the tree has an error node on or around the line being
edited, `matching_bracket`, `enclosing` and `indent_for` use the scanner for
that query. This is the common case while typing, and the reason the scanner
is not just a fallback for missing grammars.

**Highlighting.** The scanner is also a decoration provider (5.4): any mode
with a table gets string and comment faces, and keyword faces if it lists
keywords. A mode with a tree uses `highlights.scm` instead.

**Commands built on this:** word motion and deletion, `forward_expression`,
`backward_expression`, `up_expression`, `expand_selection`,
`shrink_selection`, `goto_matching_bracket`, `toggle_comment`,
`start_of_definition`, `end_of_definition`, `indent_line`, the auto-pair
minor mode, and the highlight on the bracket matching the one at the cursor.

**Limits.** Word motion treats a run of Chinese or Japanese text as one word;
there is no dictionary segmentation. A language embedded in another (code in
Markdown, SQL in a string) is not handled.

## 6. Options

Typed, documented, optionally buffer-local settings, replacing Emacs's
`defcustom` plus buffer-local variables.

```python
fill_column = Option("fill_column", int, 80, "Column beyond which lines wrap.")
tab_width = Option(
    "tab_width", int, 8, "Width of a tab stop.", validate=lambda n: n > 0, project=True
)  # may be set by a project file

buf.options.fill_column  # lookup: buffer -> project -> major mode default -> global
buf.options.fill_column = 100  # sets the buffer-local value
ed.options.fill_column = 72  # sets the global value
```

The project layer holds values from the nearest `.neomacs/settings.toml`
above the buffer's file, and only for options declared with `project=True`
(D31). EXTENSIONS.md section 8.1 has the file format and the rule for which
options may opt in.

Options are registered by name, so re-evaluating the definition updates the
doc and default without discarding values already set. Each option has an
`on_change` hook. A `describe_option` command lists value, origin and doc.

A value assigned to an option that has not been declared yet is held as
pending and validated when the declaration arrives. This lets an init file
configure an extension that loads lazily (EXTENSIONS.md 6.2).

## 7. Commands, keymaps, modes, hooks

### 7.1 Commands

```python
@command
async def find_file(path: Annotated[Path, Ask.file("Find file: ")]) -> None:
    """Visit PATH in a buffer."""
    buf = await ed.buffers.visit(path)
    ed.window.show(buf)


@command
def forward_word(sel: Selection, n: Annotated[int, Prefix(default=1)]) -> None:
    """Move forward N words."""
    sel.forward_word(n)


@command
def self_insert(sel: Selection, text: Annotated[str, KeyText]) -> None:
    """Insert the character that was typed."""
    sel.replace(text)


@command(background=True)
async def grep(pattern: Annotated[str, Ask.string("Grep for: ")]) -> None:
    """Search the project for PATTERN; results arrive in a buffer."""
    ...
```

- `@command` registers a `Command` record (name, function, doc, parameter
  specs) in `ed.commands`, a flat dictionary. The default name is the
  function name; packages are expected to use a dotted prefix. Registering an
  existing name replaces the function in the record.
- **Every invocation is an asyncio task.** `async def` is the normal form. A
  plain `def` is accepted for commands that never wait, which is most motion
  and editing; it runs inside the task without yielding.
- **Arguments.** Called from Python, arguments are passed normally. Called
  from a key, each parameter that was not supplied is resolved from its
  annotation: `Prefix` and `RawPrefix` from the prefix argument (7.6), `Ask.*` by
  prompting in a minibuffer (7.5), `KeyText` from the key that invoked the command.
- **Once per selection.** A command that declares a `Selection` parameter is
  called once for each selection in the window, in buffer order, and the
  whole pass is one invocation: one undo group, one lane turn. Other
  arguments are resolved once and shared. This is how a command written for
  one cursor works with many. A command without a `Selection` parameter
  runs once and may use `ed.window.selections` as a whole, which is what
  commands such as `add_cursor_below` and `select_all_matches` do.
- **The command lane.** Foreground commands hold the lane one at a time, in
  the order their keys arrived. Keys typed while a command is running are
  queued and resolved against the keymaps when their turn comes. Redisplay,
  timers, process output and background tasks keep running throughout, so
  the editor stays live while a foreground command waits on I/O.
- **Waiting for the user releases the lane.** `await ed.minibuffer.read(...)`
  and `await ed.read_key()` give up the lane, so the keys that answer the
  prompt can run their own commands. When the answer arrives the waiting
  command queues to take the lane back. Nested prompts stack naturally.
- **Background commands.** With `background=True` a command never holds the
  lane. It appears in `ed.tasks` with a name and optional progress, is shown
  in the modeline, and is cancelled individually. Any command can also detach
  part of its work with `ed.start_task(coro, name=...)`.
- **Context.** `ed.view`, `ed.window` and `ed.buffer` are pinned when the
  command starts, so a command keeps its own even if the user focuses
  something else while it waits. Section 7.7 gives the rules for code that
  is not a command. Tests can construct a separate `Editor` and make it
  current with a context manager.
- **Undo.** Every change is tagged with the invocation that made it. An undo
  group is one invocation's changes to one buffer, whatever else interleaved.

Rules for code that awaits:

- Text mutation never awaits (D2), so the edits between two `await`s are
  atomic.
- Across an `await` anything may have changed. Hold a `Marker`, not an
  integer position, and use `buf.changed_since(tick)` to detect edits.

### 7.2 Keymaps

A `Keymap` is a trie from key to either a command name or another keymap.

```python
global_map.bind("C-x C-f", "files.find")
python_mode.keymap.bind("C-c C-c", "python.eval_buffer")
```

Bindings store names, not functions, so a redefined command needs no
rebinding and `describe_key` can report the name. Lookup order for a key
sequence, first match wins:

1. a transient map installed by the running command (e.g. incremental search);
2. the `keymap` property of the highest-priority span at the primary cursor;
3. minor mode maps, most recently enabled first;
4. the buffer's local map;
5. the major mode's map, then its parent modes' maps;
6. the global map.

While a prefix is pending the engine keeps the partial sequence, shows it as a
`keys` message after a short delay, and cancels on `C-g`. The global map binds
printable keys to `self_insert` through a default binding.

### 7.3 Modes

```python
class PythonMode(ProgMode):
    """Major mode for Python source."""

    name = "python"
    patterns = ["*.py", "*.pyi"]
    keymap = Keymap()
    defaults = {"tab_width": 4, "indent_with_tabs": False}
    syntax = replace(ProgMode.syntax, line_comment="#")  # see 5.9
    grammar = "python"  # optional tree-sitter grammar

    def enable(self, buf: Buffer) -> None: ...
    def disable(self, buf: Buffer) -> None: ...
```

A mode is a class; each buffer gets its own instance, which is where
per-buffer mode state lives. Derived modes are subclasses, and keymaps and
defaults are inherited along the class hierarchy. Minor modes are the same
thing with `minor = True`, may be global or per buffer, and each gets a
generated toggle command. Modes are registered by `name`; the major mode for
a file is chosen from `patterns`, then from the interpreter line.

### 7.4 Hooks

```python
after_save = Hook("after_save", "Run after a buffer is written to its file.")


@after_save.add
def _refresh_git_status(buf: Buffer) -> None: ...


after_save.add(fn, buffer=buf)  # buffer-local
after_save.run(buf)
```

A hook is a named, documented list with a global part and a per-buffer part.
Adding is idempotent, keyed on the function's module and qualified name, so
re-evaluating a file does not add duplicates and the new definition replaces
the old. Functions that raise are reported through the error wrapper and the
remaining functions still run. `before_change` and `after_change` functions
must be synchronous, because they run inside the mutation. Functions on
other hooks may be coroutines and are awaited in order.

Standard hooks in the core: `before_change`, `after_change`, `pre_command`,
`post_command`, `before_save`, `after_save`, `buffer_created`,
`buffer_killed`, `mode_enabled`, `window_changed`, `ui_attached`.

**Ownership.** Every registry in this section (commands, options, hooks,
keymaps, modes) and the others in the engine (timers, tasks, faces,
placement entries, menu items) records the module that made each
registration (D30), and can list and remove registrations by module. Live
re-evaluation uses it to drop what a module no longer defines; the extension
system uses it to disable and reload (EXTENSIONS.md section 5).

### 7.5 Minibuffers

A minibuffer is any text input that is not a document buffer (D24): the
prompt for a file name, a search field, a commit message box, a rename field
at the cursor, the input line of a REPL. It functions exactly as a buffer:
the same editing commands, selections and multiple cursors, keymaps, modes,
undo, syntax and completion. It differs only in where it is displayed, and in
having no file and no entry in the buffer list.

**Views.** A *view* is a buffer together with a selection set, a jump list
and a viewport. A window is a view placed in the window tree. A minibuffer is
a view placed somewhere else. Everything that works on a view (commands, the
protocol's `lines` and `selections` events, the `layout.*` requests) works on
both without knowing which it has.

```python
class Minibuffer:
    buffer: Buffer  # private; not in ed.buffers
    view: View
    label: str
    multiline: bool
    min_lines: int
    max_lines: int
    placeholder: str
    masked: bool  # for passwords
    completer: Completer | None
    history: str | None  # name of a history list
    text: str  # the current contents

    def submit(self) -> None: ...
    def cancel(self) -> None: ...
    async def result(self) -> str: ...  # the next submitted text; raises Cancelled

    on_submit: Hook
    on_change: Hook
```

**Where it is shown.**

| Host | How | Use |
|---|---|---|
| The frame's prompt area | `ed.frame.prompt(mb)` | Questions asked by commands. The frontend decides what a prompt area looks like: a strip at the bottom, a centred palette. Prompts stack; the newest has focus. |
| Inline in a buffer | `view.embed(mb, pos)` | A field that is part of a buffer's content and moves with its text: a search box at the top of a results buffer, an input cell. Implemented as a span whose `display` is the minibuffer. |
| A named place | `ed.frame.place(mb, name)` | Any other part of the UI the frontend offers. A frontend lists its place names in the `ui.attach` options; an unknown name falls back to the prompt area. |

**Single-line and multi-line.** In a single-line minibuffer a newline can
never be inserted (pasted newlines become spaces) and `<return>` submits. In
a multi-line one `<return>` inserts a newline and `C-<return>` submits; it
grows from `min_lines` to `max_lines` and then scrolls. These are bindings in
the minibuffer's mode, so they can be changed.

**One-shot and persistent.** A prompt is removed when it is submitted or
cancelled. A minibuffer placed inline or in a named place normally stays, and
`on_submit` runs each time.

**The common case.**

```python
path = await ed.minibuffer.read("Find file: ", completer=files, history="files")
```

`ed.minibuffer.read` creates a minibuffer, shows it in the prompt area,
focuses it, awaits `result()`, removes it and restores focus. The `Ask.*`
parameter annotations (7.1) call it. It takes `initial`, `multiline` and
`masked` as well.

**Focus.** A frame has one focused view, and keys go to it. `ed.view` is the
focused view: a window, a panel tab or a minibuffer. `ed.window` is always a
window of the main area: the focused one, or, when a panel or minibuffer has
focus, the one that had it most recently. A command run from a prompt or from
a help panel can therefore act on the window it was invoked from. Key lookup (7.2) is unchanged: a
minibuffer's buffer has a major mode, `MinibufferMode`, which binds submit,
cancel, history and completion.

**Completion.** A completer supplies candidates asynchronously:

```python
class Completer(Protocol):
    async def candidates(self, text: str, pos: int) -> list[Candidate]: ...
```

It is called after each change, and a call still running when the next change
arrives is cancelled. Candidates are shown with `popup_show` anchored to the
minibuffer, and commands move through and accept them. How candidates are
matched (prefix, substring, fuzzy) is the completer's business; a helper
provides the usual filters. The same mechanism serves completion in ordinary
buffers.

**History.** `history="files"` names a list kept by the engine. Submitted
text is appended, and `history_previous` and `history_next` walk it.

**Messages.** There is no echo area. `ed.message(text, kind="info")` appends
to `*messages*` and sends a `message` event to the frame concerned.

### 7.6 Prefix arguments

A prefix argument is typed before a command and modifies it (D25): `C-u 8`
then a motion key moves eight times; a bare `C-u` often selects a variant of
the command.

```python
@dataclass(frozen=True)
class PrefixArg:
    value: int  # 4 per bare universal argument (4, 16, ...), -1 for a bare minus
    explicit: bool  # digits were typed
    universal: int  # how many bare universal arguments were typed
```

- **Declaring.** `Annotated[int, Prefix(default=1)]` receives `value`, or the
  default when no prefix was typed. `Annotated[PrefixArg | None, RawPrefix]`
  receives the whole record, for commands that treat `C-u` differently from
  `4`. A command that declares neither ignores the prefix.
- **Entering.** `universal_argument` (`C-u`), `digit_argument` (`M-0` to
  `M-9`) and `negative_argument` (`M--`) are ordinary commands registered
  with `@command(prefix=True)`. Each updates the frame's pending prefix and
  installs a transient keymap for the next key, in which digits, `-` and
  `C-u` continue the argument.
- **Consuming.** The dispatcher captures the pending prefix when a command
  starts and clears it after any command not marked `prefix=True`, after
  `C-g`, and after an unbound key. The pending prefix is shown as a `keys`
  message, e.g. `C-u 4-`.
- **Scope.** The pending prefix belongs to the frame. With several
  selections it is resolved once and shared by every per-selection call.
- **From code.** Calling a command from Python or through `engine.call`
  passes the argument explicitly; no prefix state is involved.

The dispatcher also records the last command that was not a prefix command,
with its resolved arguments, as `ed.frame.last_command`. Repeating a command
and keyboard macros would be built on that record; neither is designed yet.

### 7.7 Context outside a command

`ed.frame`, `ed.view`, `ed.window` and `ed.buffer` must mean something in
code that no key started (D27).

| Code running as | `ed.frame` | `ed.view`, `ed.window`, `ed.buffer` |
|---|---|---|
| A command | The frame its key came from | Pinned when the command starts |
| A hook function called during a command | The command's | The command's |
| A timer or task started by a command | The command's frame, inherited | Looked up from the frame each time they are read |
| Code with no originating frame: the init file, timers and processes it starts, `engine.eval` from a script | The most recently focused attached frame, or `None` | Looked up likewise; `NoFrame` is raised if there is no frame |

- **Looked up** means the frame's focused view at that moment, and that
  view's buffer. A background task that reads `ed.buffer` twice may get two
  different buffers; one that cares takes the buffer as a value before it
  starts.
- **A deleted frame.** If the inherited frame no longer exists, the last row
  applies.
- **With no frame**, `ed.message` writes only to `*messages*`, and anything
  that needs the user, such as `ed.minibuffer.read`, raises `NoFrame`. A
  frame that is merely detached still counts, and its prompts wait (4.6).
- **Hooks about a buffer receive it.** `after_change`, `after_save` and the
  like are passed the buffer and should not consult `ed.buffer`, since the
  change may come from a task in another frame.
- **Acting on a particular view.** `with ed.using(view):` pins the context
  for a block, for code that was handed a view and wants commands it calls
  to act there.

Mechanically these are context variables. Starting a task or timer copies
the frame and drops the pins.

## 8. Dispatch and display

### 8.1 Dispatch

For each input event, in arrival order:

1. If the lane is held by a command that is not waiting for input, the event
   stays queued.
2. Feed the event to the key resolver. If the sequence is still a prefix,
   wait for more. If it is unbound, report and reset.
3. Start the command task: take the lane, run `pre_command`, resolve missing
   arguments, call the command, run `post_command`, close the undo group,
   release the lane.
4. Request a view update.

The dispatcher step is a function looked up by name each time, so dispatch
itself can be redefined live.

### 8.2 Windows

A tree of splits, each with ratios, whose leaves are windows. A window is a
view (7.5) placed in that tree: it has a
buffer, a selection set (5.7), a jump list, a wrap setting, a modeline
format function, and the
viewport the frontend last reported (first and last visible line, lines per
page). The viewport is advisory and may be a moment out of date; commands
that need an exact answer ask (4.4).

### 8.3 View updates

The engine does not render. It keeps, for each window, the range of lines the
frontend currently holds and the revision of each line it sent.

A view update is scheduled whenever anything displayed becomes dirty. It
runs at most once per loop turn, and for a given frame only when that frame
has no unacknowledged batch (4.8). It is not tied to the end of a command, so
output from a background task appears as it arrives. An update:

1. For each changed line inside the held range, builds the line: take the
   text, merge decoration providers and spans by priority, leave out
   invisible text, attach inline objects, and emit the result as runs in a
   `lines` splice.
2. Sends insertions and deletions outside the held range as counts.
   Between updates the engine keeps one bounding range of changed lines per
   view, so however many edits were skipped, an update is a single splice.
3. Sends `selections` and `modeline` if they changed, and `reveal` if a
   command moved the primary cursor.
4. Sends `flush` with the next sequence number.

When the frontend reports a new viewport, the engine sends any lines it is
missing for the visible range plus a margin of about two pages either side,
so ordinary scrolling does not wait on the engine.

Faces are named (`default`, `keyword`, `heading-1`, `selection`, ...) and
defined by the active theme with the attributes listed in 4.3. The view
builder resolves a merged face to an id and sends `face_define` the first
time an id is used or when the theme changes.

### 8.4 Frames

Each attached frontend has a frame (D11). A frame owns a window tree, its
minibuffers, a focused view and a pending prefix argument; each view has its own selections, so
two frames showing the same buffer scroll and move independently while edits
appear in both.

- The engine tracks held lines and viewports per window, hence per frame, and
  a `redraw` batch goes only to the frame whose windows changed.
- A key event belongs to the frame it arrived from. `ed.frame`, `ed.window`
  and `ed.buffer` in the resulting command refer to that frame's selection,
  and its prompts open in that frame's prompt area.
- Geometry requests go to the frame that owns the window.
- A frame exists only because a frontend attached, so the engine cannot
  create one. The `new_frame` command sends the notification
  `ui.open_frame(options)` to the current frontend, which opens a window
  that attaches as a new frame.
- Display updates are paced separately for each frame (4.8), so a slow
  frontend does not hold back the others.
- There is one command lane for the whole engine, not one per frame, because
  buffers are shared. A slow foreground command started in one frame holds
  back keys in the others; `input.interrupt` from any frame cancels it.
- When a connection closes, its frame is detached and can be reattached
  (4.6). The engine keeps running with no attached frames until told to exit.

### 8.5 Panels and placement

Emacs decides where a buffer appears with a rule engine that few people
fully understand. Here a command states what it is showing, and a table says
where that kind of thing goes (D26).

**Places.** A frame has two kinds:

- The **main area**: the window tree (8.2), for documents.
- **Panels**: named containers beside or over the main area. A panel holds
  one or more views as tabs, exactly one of them active. Its content is real
  buffers in real views, so a help page or a traceback can be searched,
  selected in and copied from like any text.

A panel is either **docked** to a side of the frame, taking space from the
main area, or **floating** over it, anchored to the centre, an edge, or a
position in the text. It can be switched between the two at any time, and
hidden without losing its tabs. A floating panel may be transient, closing
when focus leaves it.

```python
class Panel:
    name: str
    mode: Literal["docked", "floating"]
    side: Literal["left", "right", "top", "bottom"]  # when docked
    anchor: Anchor  # when floating
    transient: bool
    visible: bool
    tabs: list[View]
    active: View | None

    def show(
        self, buf: Buffer, *, focus: bool = False
    ) -> View: ...  # reuses a tab already showing buf
    def close(self, view: View) -> None: ...  # closes the tab; the buffer survives
    def dock(self, side: str) -> None: ...
    def float(self, anchor: Anchor = "center") -> None: ...
    def hide(self) -> None: ...
```

**Roles.** Commands do not choose a place:

```python
view = ed.show(buf, role="help")
```

The `placement` option maps each role to a place:

```python
placement = {
    "document": Main(),
    "help": InPanel("side", dock="right"),
    "traceback": InPanel("bottom", dock="bottom"),
    "debug": InPanel("bottom", dock="bottom"),
    "messages": InPanel("bottom", dock="bottom"),
    "search": InPanel("bottom", dock="bottom"),
    "repl": InPanel("bottom", dock="bottom"),
    "peek": InPanel("peek", floating="cursor", transient=True),
}
```

- `InPanel(name, ...)` shows the buffer as a tab of that panel, creating the
  panel with the given docking or floating settings if it does not exist,
  and making it visible. Later changes the user makes to the panel (moving
  it to another side, floating it) are kept; the settings in the table are
  only for creating it.
- `Main()` shows the buffer in the main area: if a window of this frame
  already shows it, that window is used; otherwise it replaces the buffer in
  `ed.window`. `Main(split="h")` or `"v"` splits `ed.window` first.
- An unknown role is treated as `document`.
- `ed.show` gives focus to the new view only if asked (`focus=True`), so
  output appearing in a panel does not steal the keyboard.

Changing where something goes is one entry: `placement["help"] = Main(split="h")`
puts help in an ordinary split, and `InPanel("help", floating="center")`
makes it a floating window.

**Commands.** `panel.toggle`, `panel.focus`, `panel.next_tab`,
`panel.close_tab`, `panel.dock`, `panel.float`, and `other_view`, which
cycles focus through windows and visible panels.

**In the terminal frontend** a docked panel is drawn as a split with a tab
line, and a floating one as a box over the text.

### 8.6 Menus

The application menu is data in the engine (D28):

```python
ed.menu.add("File/Open…", "files.find")
ed.menu.add("Git/Status", "git.status", after="Git/Commit…")
```

An item is a path, a command name, and optional placement. The engine fills
in each item's key from the current keymaps and its enabled state from the
command, and sends the whole tree as a `menu` event when a frame attaches
and whenever an item or a relevant binding changes. Choosing an item runs
its command through `engine.call`, so a menu item can do nothing a key
binding could not. The terminal frontend may ignore the event.

## 9. Live evaluation

This is the feature the rest of the design exists to support.

### 9.1 Pipeline

`neomacs.live.evaluate(source, module, filename, first_line, mode)`:

1. `ast.parse` the source; shift line numbers by `first_line` so they match
   the buffer.
2. If `mode` is `auto` and the last top-level statement is an expression,
   split it off so its value can be returned.
3. Note which top-level names the source defines with `def` or `class`, and
   save their current values from the module.
4. `compile` with top-level `await` allowed, and `exec` in the module's
   `__dict__`; `eval` the split-off expression if any. If the compiled
   code is a coroutine, await it, so `await ed.minibuffer.read("? ")`
   works directly in the scratch buffer.
5. Patch redefinitions in place (9.3).
6. Register the source text in `linecache` under `filename`, with no
   modification time so it is never invalidated against the disk.
7. Return the value, or the exception with its traceback.

Loading a module from disk at startup and evaluating it from a buffer are
the same code path with different sources of text.

### 9.2 Namespace rule

- A buffer visiting a file that belongs to an importable package evaluates
  into that module's `__dict__`. If the module is not loaded yet, an empty
  one is created and registered; the user is expected to evaluate the whole
  buffer first, as in Emacs.
- Any other buffer evaluates into a scratch module that has `ed` and the
  public API pre-imported.

### 9.3 Redefinition

After `exec`, for each name that held a function or class before and holds a
new one now:

- **Function to function:** copy `__code__`, `__defaults__`,
  `__kwdefaults__`, `__annotations__`, `__doc__` and `__dict__` from the new
  object onto the old one, and rebind the name to the old object. Everything
  that held a reference now runs the new code. If the number of closure
  variables differs, `__code__` cannot be assigned; the name is rebound to
  the new object and a warning is shown.
- **Decorated functions:** if both old and new expose `__wrapped__`, patch
  down the chain. Otherwise rebind and warn.
- **Class to class:** update the old class in place: patch or set each
  attribute from the new class, delete attributes that are gone, and keep the
  old class object bound to the name. Existing instances, subclasses and
  `isinstance` checks keep working. Changes to bases, `__slots__` or the
  metaclass cannot be applied in place; the name is rebound and the warning
  says existing instances are stale.

This is the approach IPython's autoreload takes, applied per evaluation
rather than per file. `from m import f` is safe because `f` is still the same
object. References to a replaced module-level value that is neither a
function nor a class are not fixed up. Coroutine functions are patched the
same way; a task that is already running keeps executing the old code
until it finishes.

### 9.4 State across re-evaluation

Evaluating a buffer re-runs its module-level assignments. State that must
survive is declared explicitly:

```python
history = keep("search.history", list)  # created once, returned thereafter
```

`keep` is a keyed registry on the editor; `forget("search.history")` resets
it. Registrations are idempotent by construction: commands and options by
name, key bindings by key, hooks by qualified function name, modes by name.

### 9.5 Commands

| Command | Unit evaluated |
|---|---|
| `eval_last_expression` | The expression ending at the cursor. |
| `eval_definition` | The top-level statement containing the cursor (found from `ast` line ranges). Inside a class, the whole class. |
| `eval_selection`, `eval_buffer` | As named. With several selections, each is evaluated in buffer order. |
| `eval_expression` | Prompted in the minibuffer. |
| `repl` | A buffer whose input is evaluated in the scratch module. |

The value's `repr` is shown as a message, truncated, with the full text
in `*messages*`. An exception shows a `*traceback*` buffer, with the role
`traceback` (8.5), in which each frame is a span that jumps to the line,
including for unsaved code.

### 9.6 Introspection

Built on what Python already records: `describe_command` and
`describe_key` show the signature and docstring; `find_source` uses
`inspect` plus the `linecache` registration to open the definition;
`apropos` searches the command and option registries.

## 10. Debugging

A live-coding editor has to be able to stop inside its own commands and
inspect them, using itself as the debugger's interface. A stock `pdb` cannot
do this: it blocks on standard input, and the thread it blocks is the one
that runs the editor.

### 10.1 The problem and the approach

A breakpoint stops execution inside a callback that must not return until the
user says continue. The paused code's stack is on the main thread, so the
main thread cannot go back to its event loop, and asyncio refuses to run a
second loop on a thread that is already running one.

So the editor is serviced from somewhere else while the main thread waits
(D18):

1. The breakpoint callback, on the main thread, marks the engine paused,
   records the frame, hands ownership of editor state to the **debug loop**,
   and blocks on a `threading.Event`.
2. The debug loop is a second asyncio loop on its own thread. While the
   engine is paused, the reader thread routes all input to it. It runs the
   same dispatcher, so every command works: the debugger's own, and ordinary
   ones for looking around.
3. On continue or step, the debug loop finishes the command it is running and
   parks, ownership returns to the main thread, and the callback returns.

At any moment exactly one thread touches editor state, which is all D1
requires. The debug-build assertion in mutators checks "owner thread", not
"main thread".

### 10.2 What is stopped while paused

Everything on the main loop: the paused command, other foreground and
background tasks, timers, and process output handlers. Subprocesses
themselves keep running and their output waits in pipes. This is deliberate.
The world the paused command sees does not change under it.

`C-g` and `input.interrupt` go to the debug loop while paused. They cancel
whatever debugger command is running there and never abort the paused
command; that takes an explicit `debug.abort`.

The paused command still holds the main lane. The debug loop has a lane of
its own. Requests to the frontend made from the debug loop (geometry,
clipboard, prompts) work normally, because the reader thread delivers each
reply to the loop that sent the request.

### 10.3 Breakpoints and stepping

- **Mechanism.** `sys.monitoring`, which enables line events only on the code
  objects that have breakpoints, so undebugged code runs at full speed.
  Stepping turns on line, call and return events for the frames involved.
- **Breakpoints are spans.** A breakpoint is a span in a `debug` layer of the
  source buffer, so it moves with edits, and it carries a `gutter` mark so it is visible
  beside the line and can be toggled by clicking there. It
  resolves to a file and line, and from there to code objects.
- **Re-evaluation.** Patching a function replaces its code object (9.3). The
  evaluation pipeline tells the debugger which code objects it produced for
  which file, and breakpoints are re-applied to them.
- **`breakpoint()`.** `sys.breakpointhook` is set to the same pause, so the
  builtin works in any code.
- **Stepping across `await`.** If a step leaves the coroutine at an `await`,
  the world resumes until the coroutine does, and the pause happens then.
- **Scope.** Callbacks ignore the debug loop's own thread, so a breakpoint in
  code the debugger's interface uses cannot pause the debugger. Breakpoints
  in `in_thread` workers are not supported at first.

### 10.4 Interface

While paused, the frame that issued the command shows the source buffer in
the main area with a span on the current line, and a `*debug*` buffer,
shown with the role `debug` (8.5), listing the stack and the selected
frame's locals.

| Command | Effect |
|---|---|
| `debug.step`, `debug.next`, `debug.out` | Step into, over, or out of the current call. |
| `debug.continue` | Run to the next breakpoint. |
| `debug.up`, `debug.down` | Select a stack frame; the source follows. |
| `debug.abort` | Raise `DebugAbort` in the paused frame, unwinding the command. |
| `debug.toggle_breakpoint` | At the cursor. A condition may be given. |
| `eval_expression` and the other eval commands | While paused, evaluate in the selected frame's globals and locals. Assignments to locals take effect. |

The debugger's keymap is a minor mode enabled on the source buffers while
paused, so its bindings are ordinary and rebindable.

### 10.5 After an error

An uncaught exception in a command already opens `*traceback*` (9.5). From
there `debug.inspect` opens the same interface on the traceback's frames:
stack navigation and evaluation work, stepping and continuing do not, because
the code has already unwound. An option `debug_on_raise` pauses at the point
of raising instead, for a named exception type.

### 10.6 Limits

- Editing a buffer while the paused command is halfway through editing it is
  allowed and can confuse that command when it resumes.
- Re-evaluating a function while paused affects future calls; the paused
  frame continues with the code it entered with.
- A breakpoint inside the text core's mutation path pauses with buffer
  bookkeeping half done. The debug loop will display such a buffer, but
  should not edit it.
- If the debugger itself is broken, the fallback is an external debugger
  attached to the engine process, which is outside this design.

## 11. Asynchronous work

- **Timers:** `ed.after(seconds, fn)`, `ed.every(seconds, fn)`,
  `ed.when_idle(seconds, fn)`; each returns a handle with `cancel()`.
- **Processes:** `await ed.run(argv, input=...)` for one-shot commands;
  `ed.spawn(argv, on_output=..., on_exit=...)` or output into a buffer for
  long-lived ones. Built on asyncio subprocesses.
- **Threads:** `await ed.in_thread(fn, *args)` for CPU-bound work on
  immutable inputs such as a text snapshot.

- **Tasks:** `ed.start_task(coro, name=...)` for detached work; `ed.tasks`
  lists what is running, with progress and a `cancel()` on each.

All callbacks arrive on the main thread through the error wrapper.

## 12. Package layout

```
neomacs/
  __main__.py            start the engine: parse args, open transport, run loop
  editor.py              Editor: registries, lifecycle, `ed` proxy
  keys.py                key notation
  text/store.py          TextStore and the chunked implementation
  text/buffer.py         Buffer, change pipeline, files
  text/marker.py         Marker, Cursor
  selection.py           Selection, SelectionSet, jump list, kill ring
  text/spans.py          span layers
  text/undo.py
  syntax/table.py        Syntax, word classification
  syntax/scan.py         the scanner and its line-state cache
  syntax/tree.py         tree-sitter adapter, byte offsets, queries (optional)
  syntax/buffer.py       BufferSyntax, choosing between the two
  options.py  hooks.py  commands.py  keymap.py  modes.py
  dispatch.py            key resolver, command tasks, the lane, cancellation
  minibuffer.py  completion.py
  window.py              views, the window tree, panels, placement by role
  display/view.py        builds lines of runs from text, spans and providers
  display/faces.py
  rpc/transport.py  rpc/protocol.py  rpc/api.py  rpc/ui.py
  live/evaluate.py  live/patch.py  live/source.py
  debug/pause.py         monitoring callbacks, ownership hand-over, debug loop
  debug/breakpoints.py  debug/ui.py
  clipboard.py           kill ring, clipboard requests
  proc.py  timers.py
  std/                   standard commands, written only against the public API
    movement.py  editing.py  cursors.py  files.py  windows.py  help.py
    evalcmds.py
  modes/fundamental.py  modes/text.py  modes/python.py
  modes/queries/python/  highlights.scm  textobjects.scm  indents.scm
  frontends/monolayout.py  monospace layout of lines of runs; answers layout.*
  frontends/tui.py         reference terminal frontend, built on monolayout
  testing/               headless client and fixtures (`neomacs.testing`)
tests/
frontend/                the Electron application (FRONTEND.md)
packaging/               build and signing scripts (DISTRIBUTION.md)
```

Dependencies: none at run time. The protocol uses the standard library's
`json`, and the terminal frontend takes character widths from `unicodedata`.
`tree-sitter`, optionally, from stage 6; `hypothesis`
and `pytest` for tests. Typed throughout and checked in strict mode.

The terminal frontend and its layout module are inside the package because
the wheel ships them (DISTRIBUTION.md 3.1). Development uses `uv` for
environments and locking, `pyright` in strict mode, and `ruff` for linting
and formatting.

Every required dependency with a C extension must ship a free-threaded build
(D13). Importing one that does not makes CPython turn the GIL back on for
the whole process, so CI fails if the GIL is found enabled after startup
with no optional packages loaded. `tree-sitter` is optional (D21). If loading
it re-enables the GIL the engine still runs correctly, loses parallelism in
worker threads, and reports the fact in `*messages*`.

## 13. Implementation stages

The engine has eight stages, 0 to 7. Each ends with something that can be
run and checked using only the terminal frontend, so the engine can be built
and used to completion without any Electron work. The Electron frontend is a
parallel track, described after the stages.

**Stage 0: skeleton and RPC.** Package, `Editor`, reader thread, JSON-RPC
framing, `engine.api_info`, `engine.eval`, the error wrapper, a Python test
client. *Runnable:* start the engine on a socket, connect from a script,
evaluate `1 + 1`, and observe that an exception comes back as an error
without killing the engine.

**Stage 1: text core.** `TextStore`, `Buffer`, markers, `Cursor`, the
grapheme boundary functions, span layers, undo, change hooks, file read and
write. *Runnable:* over `engine.eval`, open a
file, edit it, undo, save. Property tests compare the store, markers and
undo against a naive `str` model under random edits.

**Stage 2: display and the terminal frontend.** One window, the view builder,
faces, `ui.attach`, `redraw` with `lines`, `selections`, `modeline`, `reveal` and
`flush`, viewport reports, the gutter, margins and tab stops, acknowledgement pacing, `reset`, `ui.resync` and reattaching to a frame. `frontends/monolayout.py` lays out lines of runs in
character cells (wrapping, wide characters, scrolling) and answers the
`layout.*` requests; `frontends/tui.py` draws its result. Hard-wired self-insert
and cursor movement. *Runnable:* view and type into a file in a terminal.
The headless test client uses the same layout module, so tests can assert on
what would be displayed.

**Stage 3: the command system.** `@command`, command tasks and the lane,
argument resolution, keymaps and the key resolver, the dispatcher, options,
hooks, the ownership record on every registry (D30), minibuffers (in the
prompt area and inline) with completion and
history, messages, prefix arguments, cancellation and interrupt, the
background task list, `*messages*`, syntax tables and the scanner, the kill ring and clipboard messages,
selection sets with per-selection
dispatch, the jump list, and the standard commands for movement, selecting,
adding and removing cursors, killing and yanking, files, buffers and running
a command by name. `ed.show` with roles, every role placed in the main area
until panels arrive in stage 6. The context rules of 7.7. The remaining
input messages (`input.text`, `input.paste`, `input.mouse`, `input.drop`),
the menu registry and `menu` event, and `new_frame` with `ui.open_frame`.
*Runnable:* a usable minimal editor in which a deliberately slow command
leaves the display live, keeps typed-ahead keys in order, and is cancelled by
`C-g`; typing with three cursors edits three places and undoes in one step.

**Stage 4: live evaluation.** The evaluation pipeline, in-place patching,
`linecache` registration, `keep`, the eval commands, the traceback buffer,
the scratch buffer, `describe_*` and `find_source`. *Runnable:* from the
scratch buffer, redefine a built-in command and rebind a key, and see both
take effect on the next keystroke; jump to the source of any command.

**Stage 5: debugger.** The debug loop and ownership hand-over, reply routing
by loop, `sys.monitoring` breakpoints as spans, `breakpoint()`, stepping
including across `await`, the `*debug*` buffer and commands, evaluation in a
frame, post-mortem inspection. *Runnable:* set a breakpoint in a built-in
command, press its key, step through it while scrolling other buffers, change
a local, continue.

**Stage 6: modes and annotations.** Major and minor modes with their syntax
tables, decoration providers, scanner-based highlighting for
every mode, the tree-sitter layer and Python queries where the binding
loads, the expression, selection-expanding, comment and indentation
commands, incremental search with `add_next_match` and `select_all_matches`, window splits, and panels: docked
and floating, with tabs, and the default placement table. Paragraph
attributes on lines, and resources with inline images. *Runnable:* highlighted Python with
structural selection and search, a cursor on every match of a word, and the
same session with tree-sitter uninstalled.

**Stage 7: processes and startup.** Timers, processes, the user init file,
single-file extensions from the user's directory, project settings (D31),
and the crash-recovery journal for unsaved buffers. *Runnable:* a shell command's
output streamed into a buffer; configuration that survives restart.

### 13.1 Parallel tracks

The other four documents have stages of their own. Each can start once the
engine stage it needs is done, and none of them holds up the next engine
stage.

| After engine stage | Electron frontend (FRONTEND.md) | Other tracks |
|---|---|---|
| 0 | F0: shell and connection | P0: the wheel (DISTRIBUTION.md); then P1, the unsigned app, once F0 exists |
| 1 | | T0: markers and manifest (STABILITY.md) |
| 2 | F1: one view | T1: protocol version |
| 3 | F2: input and geometry | T2: deprecation machinery |
| 6 | F3: frame chrome; F4: the rest of the protocol | |
| 7 | F5: finish | X0 to X4: extensions (EXTENSIONS.md); then T4 |

The remaining distribution stages (P2 to P5) and the release checks and 1.0
freeze (T3, T5) follow once there is something to release.

There is no engine stage for "the Electron frontend". Everything the engine
does for a rich frontend is protocol work that the terminal frontend and the
headless test client also exercise, and it is assigned to stages 2, 3 and 6
above.

## 14. Verification

- **Unit and property tests** for the text core (stage 1 onward).
- **View tests:** a headless client applies `redraw` events and asserts on
  the lines of runs it holds; with the monospace layout module it can also
  assert on rows as displayed, in the manner of Neovim's functional tests.
- **Syntax tests:** every `buf.syntax` method is run against a corpus of
  well-formed files with the tree and with the scanner alone, and the two
  must agree wherever both answer; a second corpus of half-typed code checks
  that bracket matching and indentation stay sensible; byte and code point
  offsets round-trip on non-ASCII text; the whole suite passes with
  tree-sitter not installed.
- **Grapheme tests:** a corpus of combining sequences, flags, joined emoji,
  skin-tone modifiers and Indic syllables: character motion crosses each in
  one step in both directions, deletion removes it whole, and no command
  sequence leaves a selection end inside a cluster. The Unicode
  consortium's `GraphemeBreakTest.txt` for the library's Unicode version is
  run against the boundary functions.
- **Selection tests:** property tests that random command sequences leave
  the set sorted and non-overlapping; every standard editing command run
  with one cursor and with several gives the same text as doing the edits
  one at a time; undo restores every cursor; kill and yank with matching
  and mismatched counts.
- **Concurrency tests:** keys typed during a slow foreground command apply in
  order; a prompt inside a prompt; a background task editing a buffer the
  user is typing in; cancellation at each `await`; the 200 ms fallback
  interrupt for a command that never awaits.
- **Live-patch tests:** redefine a function held by a hook, a timer and a
  `from` import; redefine a class with live instances; redefine a coroutine
  command while an invocation of it is suspended; change a closure's free
  variables and check the fallback warning.
- **Latency benchmark**, run in CI from stage 3: time from `input.key`
  received to `flush` sent for self-insert in a 10,000-line highlighted
  file. Target: 99th percentile under 5 ms on a development laptop. A second
  figure for vertical motion, which includes a geometry round trip.
- **Protocol conformance:** the terminal frontend and the headless client are
  both driven by the same recorded sessions.
- **Frame tests:** two headless clients attached at once; edits in one
  appear in the other; each keeps its own cursor and viewport; closing one
  leaves the other working.
- **Placement tests:** each role lands where the table says; showing a
  buffer twice reuses its tab or window; a panel survives hide and show
  with its tabs; docking and floating round-trip; closing a tab leaves the
  buffer alive; `ed.show` does not move focus unless asked; `ed.window`
  stays a main-area window while a panel has focus.
- **Context tests:** a task started by a command reports to that command's
  frame after focus has moved; with that frame deleted it falls back to the
  most recent one; with none attached a prompt raises `NoFrame` and a
  message reaches `*messages*`; `C-g` while paused leaves the paused
  command intact.
- **Minibuffer tests:** the same editing command sequence gives the same
  text in a window and in a minibuffer; single-line never contains a
  newline; nested prompts restore focus in order; cancelling raises in the
  awaiting command; an inline minibuffer moves with edits above it; a
  completer call is cancelled by the next keystroke.
- **Prefix tests:** digits, repeated universal arguments and minus build the
  expected value; the prefix reaches the next command only and is cleared by
  `C-g`; a per-selection command sees the same value at every selection.
- **Pacing tests:** a client that withholds acknowledgements receives no
  second batch however many edits are made, then one batch that brings it
  exactly to the current state; no key is lost or reordered meanwhile;
  a geometry request forces a send; a slow client does not delay a fast
  one; a stalled client is disconnected at the queue limit and recovers by
  reattaching.
- **Reattach tests:** drop a connection mid-session and reattach: layout,
  selections and a pending prompt survive; a request awaiting the lost
  frontend fails with `FrontendGone`; an expired frame is deleted;
  `ui.resync` reproduces the same state as a fresh sync.
- **Clipboard tests:** kill reaches the client; yank prefers newer external
  text; the 500 ms fallback; piece distribution across selections.
- **Debugger tests:** pause in a synchronous and in a coroutine command;
  the display stays live and timers stay stopped while paused; step across
  an `await`; breakpoints survive re-evaluation and edits above them;
  abort unwinds and releases the lane; the owner-thread assertion never
  fires.

## 15. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| The engine cannot see layout (D5) | Vertical motion, paging and anything else geometric costs a round trip, and the engine's idea of the viewport can be stale | Geometry requests are ordered after the redraw they depend on; the round trip is local and measured by the benchmark; the common cases (`reveal`, mouse hit testing, popup placement) are delegated to the frontend outright. |
| The frontend scrolls past the lines it holds | Blank or placeholder lines during a fast scroll | The engine keeps a margin of lines either side of the viewport; the frontend estimates heights for lines it has not laid out. |
| Two frontends lay out differently | A command behaves differently in the terminal and in Electron | Commands depend on layout only through `layout.*`; the monospace layout module is the tested reference for those calls. |
| Commands interleave at every `await` | Stale positions, edits applied to text that has since changed | The lane keeps foreground commands sequential; mutation is atomic between awaits; markers and `changed_since`; a debug check that warns when a suspended command's buffer changed under it. |
| A command computes without awaiting | The loop stalls: no redisplay, no cancel request delivered | The reader-thread fallback interrupt after 200 ms; `in_thread` for heavy work; a debug warning for any loop turn over 50 ms, naming the command. |
| Pure Python is too slow for the store or the view builder | Typing lag in large or heavily decorated files | Latency benchmark from stage 3; both sit behind narrow interfaces that a native extension can replace. |
| In-place patching has edge cases | A redefinition silently does not take effect | Every fallback to rebinding produces a visible warning naming the object. |
| Interrupt cannot break a blocking C call | A runaway regex freezes the engine | Timeouts on searches started by commands; long work goes to `in_thread`. |
| One process holds all state | A crash loses unsaved work | Recovery journal in stage 7. |
| The socket evaluates arbitrary code | Local privilege issue | stdio or a 0600 Unix socket only; no TCP without an explicit flag and a token. |
| `tree-sitter` does not support the free-threaded build | No parse tree, or the GIL is re-enabled when it loads | It is optional (D21); the scanner covers every standard command. Checked on 2026-11-24 with free-threaded 3.15.2 on macOS arm64: `tree-sitter` 0.27.0 has no prebuilt wheel, builds from source, and re-enables the GIL on import; `tree-sitter-python` 0.25.0 builds from source but fails to import (missing scanner symbol). Recheck at the start of stage 6. |
| The scanner and the tree disagree | A command behaves differently depending on whether a grammar is installed | The agreement tests in section 14; the table is the single source for delimiters in both layers. |
| A required dependency lacks a free-threaded build | The GIL is silently re-enabled, or the package will not install | Startup check in CI. Today there are no required dependencies with C extensions. |
| Free threading exposes races the GIL used to hide | Corruption if a worker thread touches editor state | D1 plus the owner-thread assertion in every mutator, enabled in tests. |
| Ownership hand-over to the debug loop is wrong somewhere | Two threads touch editor state, or neither runs | One hand-over point in one module; the assertion above; debugger tests run under the free-threaded build, where such a bug shows quickly. |
| A long pause at a breakpoint | Subprocess pipes fill and language servers time out, since nothing on the main loop runs | Stated behaviour (10.2); the modeline shows the paused state in every frame. |
| `sys.monitoring` behaves differently across CPython releases or under free threading | Missed breakpoints or wrong stepping | The mechanism is confined to `debug/pause.py`; verify on the chosen release at the start of stage 5. |
| Python and the frontend carry different Unicode versions | They disagree about a very new emoji sequence: the cursor steps through something drawn as one glyph, or the reverse | The engine is the authority for where a cursor may be; affects only sequences newer than the older of the two tables. |
| A detached frame holds a prompt, and so a command, indefinitely | That command's state lingers | Frames expire after `frame_detach_timeout`, cancelling their prompts. |
| A frontend stops acknowledging | Its display freezes while the engine carries on | The frame is marked stalled after 2 seconds and disconnected at a queue limit; reattaching gives a full sync (4.8). |

## 16. Deferred

No open question blocks stage 0. These were considered and put off:

- **Embedded frontend content.** Letting an extension place arbitrary HTML in
  a line or a whole window. Powerful in Electron, impossible in the terminal
  frontend. To be revisited when a concrete feature needs it.
- **Undo tree.** The change log already records enough to build one.
- **Native text store.** Only if the latency benchmark calls for it (D10).
- **Engine lifecycle, remaining part.** Who starts the engine is settled:
  one per app instance (D29, DISTRIBUTION.md section 7). Still open: what
  happens to unsaved buffers when the instance exits.
- **External file changes.** Detecting that a file changed on disk,
  auto-save and backups. Only the crash-recovery journal is planned.
- **Embedded languages and word segmentation** for scripts written without
  spaces (5.9).
- **Normalisation in search.** A composed `é` and a decomposed one are
  different code point sequences and do not match each other. Whether
  search should treat them as equal is a separate question from cluster
  boundaries.
- **Repeat and keyboard macros.** To be built on the dispatcher's record of
  the last command (7.6).
- **Persistent history.** Minibuffer history lists do not survive a restart.
- **Very long lines.** The unit of display is a whole logical line, so a
  file with a single line of several megabytes is sent as one message and
  laid out as one paragraph. A rule for splitting or truncating such lines
  is needed before the editor is usable on minified or generated files.
- **Message framing.** Newline-delimited JSON was chosen over LSP-style
  length headers for simplicity; switching is confined to the transport.
