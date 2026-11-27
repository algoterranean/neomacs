# Electron frontend design

Status: draft 7, 2026-11-27. Stages F0 and F1 are implemented in `frontend/`. The decisions in
section 2 are confirmed.

This describes the Electron frontend of `neomacs`. It is the client side of the
protocol in ENGINE.md section 4 and assumes that document throughout:
"the engine" is the Python process, a "view" is a window, panel tab or
minibuffer, and positions are 1-based lines with 0-based code point offsets.

## 1. Goals and non-goals

Goals:

- Rich text: proportional fonts, mixed sizes, inline images, embedded
  minibuffers, correct shaping, bidirectional text and font fallback.
- The frontend holds no editor logic. It lays out, draws, scrolls, translates
  input and answers geometry questions. Every command runs in the engine.
- Typing feels immediate: the frontend's share of a keystroke fits in one
  display frame.
- A renderer reload or a dropped connection loses nothing (ENGINE 4.6).
- Content from files is never treated as markup or code.

Non-goals:

- Working without the engine, or offline editing of any kind.
- A plugin system of its own. Extensions are Python (EXTENSIONS.md).
- Mobile or browser-hosted versions.
- Full screen reader support. Views and regions are labelled and messages
  are announced (stage F5); making a virtualised editor properly usable with
  a screen reader is a separate project.

## 2. Decisions taken

| # | Decision | Reason |
|---|---|---|
| F1 | One `BrowserWindow` is one frame and has one engine connection. The main process owns the socket and relays lines of JSON between it and the renderer, unparsed, over Electron's IPC. | Matches ENGINE D11. Keeping the socket in the main process lets a renderer reload without dropping the connection. |
| F2 | Text is rendered with the DOM, one element per logical line, not on a canvas. | The browser already does shaping, wrapping, bidi, font fallback, images and hit testing. Rich text on a canvas means rebuilding all of it. |
| F3 | The document is not `contenteditable`. Text input comes through the EditContext API. | The engine is the only thing that edits text. EditContext gives input-method support without letting the browser mutate the DOM. |
| F4 | Selections and cursors are drawn by the frontend as overlay elements, not with the browser's native selection. | Multiple cursors and several selections (ENGINE D16) are not expressible as a native selection. |
| F5 | Only lines near the viewport exist in the DOM. Heights of other lines are estimated and corrected as they are measured. | Keeps layout cost proportional to what is visible. |
| F6 | TypeScript, no UI framework anywhere: not for views, and not for the chrome around them (tab strips, palette, toasts). Direct DOM manipulation, keyed by line in the views. | The update path is a stream of splices; a virtual DOM adds a diff the engine has already done. The chrome is small and driven entirely by engine events, so it has little state of its own. |
| F7 | The renderer is sandboxed with context isolation, no Node integration and a strict content security policy. Buffer text is inserted only as text nodes. | A file being viewed must not be able to run script or reach the file system. |
| F8 | Faces become generated CSS rules; sizes are relative to the view's default face. | One rule per face id; restyling on a theme change touches the stylesheet, not the lines. |
| F9 | The application menu is supplied by the engine. | Extensions can add items, and the menu shows the key actually bound to each command. |
| F10 | On macOS the Option key is Meta by default. | The editor's bindings assume a Meta key. A setting restores Option for typing characters. |
| F11 | The main process starts its own engine as a child and passes it a fresh socket path. It does not look for one already running. | One engine per app instance (DISTRIBUTION.md P8). |
| F12 | The cursor animates between positions. Scrolling caused by the keyboard does not. | Makes it easy to follow where the cursor went. Kept strictly local to the frontend and cut short under load so it never delays typing. |

## 3. Processes

```
 Engine (Python)            Electron main (Node)              Renderer (sandboxed)
┌───────────────┐  socket  ┌─────────────────────┐     IPC     ┌────────────────────┐
│ one connection│◄────────►│ one socket per       │◄──────────►│ protocol client     │
│ per frame     │  JSON    │ window; clipboard;   │  lines of  │ model, layout, DOM  │
└───────────────┘  lines   │ resources; menus     │  JSON      │ input, geometry     │
                           └─────────────────────┘            └────────────────────┘
```

- **Main process.** Starts the engine, opens one connection per
  window, and forwards each line of JSON to that window's renderer over IPC
  without parsing it. It also holds what a sandboxed renderer
  may not: the system clipboard, native menus and dialogs, and the resource
  protocol (section 9).
- **Renderer.** Parses messages, keeps the frame's model, renders, and sends
  input. It never touches the file system or the network.
- **Preload script.** Exposes exactly one object to the page, `neomacsHost`:
  send a line, receive lines, learn how to attach (fresh, resync, or
  reattach with a frame id and token), and report the frame once attached.
  Clipboard, beep and similar calls are added to it as later stages need
  them.
- **The page itself** is served from a private scheme, `neomacs-app://`,
  that maps only onto the bundled renderer files, so the content security
  policy's `'self'` means exactly those files.

**Starting the engine.** On launch the main process picks a socket path in
the runtime directory (DISTRIBUTION.md section 5), starts the bundled engine
as a child process with `--listen` on that path, and connects when it is
ready. Every window of this app instance connects to that engine. When the
app quits, the main process asks the engine to exit.

**Reload and reconnect.**

- Renderer reload (development, or a crash of the page): the socket stays
  open in the main process. The new page calls `ui.resync` and rebuilds from
  the full state sync.
- Socket lost: the main process reconnects with backoff and the renderer
  calls `ui.attach` with the frame id and token, which the main process keeps
  for each window. If the frame has expired, it attaches fresh.
- Engine process gone: the main process shows the tail of the engine's log
  and offers to start a new one. That is a new engine with none of the old
  state; unsaved work comes back only through the recovery journal.

## 4. The renderer's model

Per frame: the layout (window tree and panels), the face table, the resource
table, the focused view, and the views.

Per view:

- `lineCount`, wrap mode, gutter columns, margins, tab width (from
  `win_info`).
- **Held lines:** a contiguous range of logical lines with, for each, its
  attributes, its runs, and a cached UTF-16 conversion table (section 6).
- **Height index:** measured pixel height for each laid-out line, an
  estimate for the rest, in a structure that answers "y of line n" and "line
  at y" in logarithmic time.
- Selections, primary index, cursor shape.
- Modeline runs.

**Applying a batch.** Events mutate the model in order. Nothing is drawn
until `flush`. On `flush` the renderer schedules one render in the next
animation frame, and after that frame has painted it sends
`view.ack(seq)`. If `document.visibilityState` is `hidden`, it acknowledges
immediately after applying (ENGINE 4.8).

`lines` is a splice on the held range, and maps to inserting, removing or
replacing line elements. A splice outside the held range adjusts `lineCount`
and the height index only. `reset` clears the frame's model entirely.

## 5. Layout and drawing

### 5.1 Structure of a view

```
view (scroll container)
├── spacer above        height = estimated height of lines before the DOM range
├── line …              one per logical line in the DOM range
│   ├── gutter cells    one per gutter column
│   └── content         runs as spans, inline objects as elements
├── spacer below
├── selection layer     absolutely positioned rectangles
└── cursor layer        one element per visible selection head
```

- A text run is `<span class="f12">…</span>` with the run's code point
  offset kept on the element. Its text is set with `textContent`.
- An image object is an `<img>` whose source is a resource URL (section 9).
- An embedded minibuffer is a nested view element.
- A line's content uses `white-space: pre-wrap` when the view wraps and
  `pre` when it does not.
- Gutter content appears on the first visual row of a wrapped line. Number
  columns are drawn from the line's index and the primary selection, with no
  data from the engine.

### 5.2 Faces

`face_define(id, attrs)` generates one CSS rule, `.f<id> { … }`. Face 0 is
applied to the frame's root element, so everything else inherits from it. A
`size` that is a number is a multiple and becomes `em`; a string such as
`"12pt"` is an absolute size. Every value is checked before it is written
into the sheet, so a face cannot inject CSS; `line_height`, weight, slant, colours,
underline, strike and box map to the corresponding CSS properties. A theme
change arrives as new `face_define` events and rewrites rules in place.

### 5.3 Tabs, margins, gutter widths

- **Tabs.** The line element sets `tab-size` to the view's `tab_width`. CSS
  measures tab stops in multiples of the space advance of the block's own
  font, from the block's content edge, which is the rule ENGINE 4.3 states.
- **Margins and `max_width`** are padding and a maximum width on the content
  column, in `em` of face 0.
- **Mark columns** have their `width` in `em`.

### 5.4 Virtual scrolling

The frontend owns scroll position.

1. The scroll container's total height is the sum of the height index.
2. On scroll, the renderer finds the visible line range from the height
   index, and reports it with `view.viewport(view, first, last, page_lines)`,
   at most once per animation frame.
3. Lines in the visible range plus one page either side are in the DOM. The
   engine keeps about two pages either side held (ENGINE 8.3), so the DOM
   range is normally a subset of what is held.
4. A visible line that is not held is drawn as an empty placeholder of
   estimated height until its `lines` event arrives.
5. When a line is laid out, its measured height replaces the estimate. If it
   lies above the viewport, the scroll offset is adjusted by the difference
   so that what the user is looking at does not move.
6. The view's position is "this line, this many pixels into it", not a
   scroll offset; the scroll offset is derived from it on every render.
   A render can run after the user has scrolled but before the scroll event
   for it has been delivered, so every render first checks whether the
   scroller is still where the view last put it, and if not, takes the
   scroller's position as the user's.

`reveal(view, line, offset, align)` scrolls so the position is visible, using
the same machinery; if the target line is not held, the renderer scrolls to
its estimated position, reports the viewport, and corrects once it arrives.

### 5.5 Selections and cursors

For each selection that intersects the DOM range, the renderer computes
client rectangles with a `Range` over the affected runs and places highlight
rectangles in the selection layer. A cursor element is placed at each head,
in the shape the engine asked for. The primary cursor is distinguished by a
class. These layers are recomputed on `selections`, on any `lines` change in
the DOM range, and on resize.

**Cursor animation (F12).** When a selection head moves, its cursor element
glides from the old rectangle to the new one over about 80 ms, using a CSS
transform so no layout is involved. The animation is purely cosmetic: the
engine's position is already the new one, and geometry requests are answered
from it. To keep it from feeling like lag:

- If the next batch arrives before an animation finishes, the cursor jumps
  to its current target and animates from there.
- No animation while a key is auto-repeating, when the move is larger than
  the viewport, or when the view has more than a handful of cursors.
- The setting `cursor_animation` turns it off.

Scrolling that the engine asks for with `reveal` is not animated. Wheel and
trackpad scrolling is already continuous, because the frontend owns it.

### 5.6 Frame chrome

- **Main area.** The window tree is nested flex containers. Dragging a
  divider updates ratios locally and sends `view.split_resized`.
- **Panels.** A docked panel is a region on its side with a tab strip. A
  floating panel is positioned over the main area at its anchor; an anchor
  that names a text position is resolved with the same rectangle lookup as a
  cursor. Tab clicks, drags to another side, float and close send
  `ui.panel`, and the renderer waits for the engine's `layout` before
  changing anything.
- **Prompt area.** A centred palette over the main area that hosts whichever
  minibuffer is on top of the prompt stack.
- **Popup.** `popup_show` items in a list anchored below the given position,
  flipping above when there is no room.
- **Messages.** A status strip along the bottom shows the latest `info`
  message and, at its right end, any `keys` message. `warning` and `error`
  also appear as a dismissible toast.
- **Modeline.** One strip per window, drawn from runs like a line.
- **Title, bell, busy.** Window title; a brief visual flash and the system
  beep; a progress indicator in the status strip.

## 6. Positions

The engine speaks code points; the DOM and JavaScript strings speak UTF-16.

- Each held line records whether all its text is in the Basic Multilingual
  Plane. If so, the two indices are equal and no conversion happens.
- Otherwise the line builds, on first use, a table of the UTF-16 index of
  each code point, and converts through it.

**Position to pixels.** Find the run containing the offset, convert to a
UTF-16 index within its text node, and take `Range.getClientRects()`.

**Pixels to position.** `document.caretPositionFromPoint(x, y)` gives a text
node and index; the run's stored offset plus the converted index is the
position. A point in virtual text maps to the nearest real offset. A point in
the gutter reports `area: "gutter:<column>"` and the line only.

The engine snaps whatever is reported to a grapheme cluster boundary
(ENGINE 5.8), so the frontend does no cluster arithmetic of its own.

## 7. Geometry requests

| Request | How it is answered |
|---|---|
| `layout.move(view, positions, "visual_line", count)` | For each position: take its caret rectangle; step `count` visual rows using the rows' own rectangles, crossing into neighbouring logical lines as needed; hit-test at the goal x on the destination row. Returns position and goal x. |
| `layout.move(…, "page", count)` | Scroll by `count` viewport heights, then hit-test at the goal x on the row at the same distance from the top as before. |
| `layout.line_edge(view, line, offset, which)` | First or last position on the visual row containing the caret rectangle. |
| `layout.viewport(view)` | From the height index and scroll offset. |
| `layout.rect(view, line, offset)` | The caret rectangle in view coordinates. |

If answering needs a line that is not held, the renderer scrolls or extends
its viewport report, waits for the `lines` event, and then answers. The
engine can send lines while a command is awaiting the reply, because view
updates do not depend on the command lane. If the line has not arrived
within 250 ms the renderer replies with an error and the engine falls back to
motion by logical line.

## 8. Input

### 8.1 Keys

A `keydown` becomes one `input.key` in ENGINE's notation (4.7).

- Modifiers map to `C-` (Control), `M-` (Alt, or Option on macOS), `s-`
  (Command or Super), and `S-` (Shift).
- For a printable key, `S-` is dropped and the shifted character is sent:
  Shift with `a` is `A`, not `S-a`. For named keys it is kept: `S-<left>`.
- With Control or Meta held, the base key is taken from the physical key
  (`KeyboardEvent.code`) mapped through the current layout, so that Option on
  macOS yields `M-f` and not the character Option-f types. This is the
  default (F10). The setting `option_is_meta` turns it off for users who
  need Option to type characters.
- Named keys use Emacs names: `<return>`, `<tab>`, `<backspace>`,
  `<delete>`, `<escape>`, arrows, `<home>`, `<end>`, `<prior>`, `<next>`,
  `<f1>` and so on.
- Every `keydown` handled this way has its default action prevented. No
  browser or Electron shortcut is left active.
- `C-g` also sends `input.interrupt`.

### 8.2 Text and input methods

The focused view owns an `EditContext`.

- While a composition is in progress, the renderer draws the pre-edit text
  itself at the primary cursor, as a local decoration. The engine is told
  nothing.
- When text is committed, the renderer sends `input.text`.
- After each render the renderer reports the primary cursor's rectangle to
  the `EditContext`, so the system's candidate window appears in the right
  place.

A plain printable key with no composition is sent as `input.key`, so it goes
through keymaps like everything else.

### 8.3 Mouse, wheel, drop

- **Mouse.** Press, release and drag are hit-tested and sent as
  `input.mouse`, with a click count for double and triple clicks, since the
  operating system's double-click interval is known here. Drags are sent at
  most once per animation frame.
- **Wheel and scroll bar** scroll locally and result in a viewport report.
- **Dropping files** onto a view sends `input.drop(paths, view, line,
  offset)`.
- **Right click** is `<mouse-3>`. A context menu, if a command wants one, is
  a `popup_show`.

### 8.4 Clipboard and menus

- `clipboard.set` and `clipboard.get` are passed to the main process, which
  uses Electron's clipboard module, including the primary selection on
  Linux.
- Paste typed as a key (`s-v`, `C-y`) is just a key; the engine's `yank`
  asks for the clipboard. The native menu's Paste item and a middle click
  send `input.paste`.
- The application menu is built from the engine's `menu` event (ENGINE
  8.6). Each item has a label, a command name and the key bound to it;
  choosing one sends `engine.call` with that command. The main process
  rebuilds the native menu whenever the event arrives. On macOS the
  system-required items (About, Hide, Quit) are added by the frontend.

## 9. Resources and security

- **Resource protocol.** The main process registers a private URL scheme.
  `resource_define(id, kind, {"path": …})` registers that path under the id,
  and the renderer refers to it as `neomacs-resource://<frame>/<id>`. The main
  process serves only ids the engine has defined, so the page cannot read
  arbitrary files. `{"data": …}` resources become blob URLs.
- **Sandbox.** `sandbox: true`, `contextIsolation: true`,
  `nodeIntegration: false`, no `webview`, navigation and new windows denied.
- **Content security policy.** No inline or remote script, no remote
  connections; images only from the resource scheme and blobs; styles from
  the bundled sheet and the generated face sheet.
- **Text is text.** Runs, labels, titles, popup items and messages are set
  with `textContent`. Nothing received from the engine is parsed as HTML.

The engine evaluates arbitrary Python by design. The point of these rules is
narrower: opening a hostile file must not let its contents execute in the
renderer.

## 10. Several windows

Each window is a frame with its own connection. A command that wants another
window sends `ui.open_frame(options)`; the main process creates a
`BrowserWindow`, which attaches as a new frame. Closing a window sends
`ui.detach`. The main process keeps each window's frame id and token for
reattaching.

## 11. Performance

Budgets, measured with marks around each phase and checked in CI on a
reference machine:

| Phase | Budget |
|---|---|
| Parse and apply a typical keystroke batch | 1 ms |
| Layout and paint for that batch | 6 ms |
| Renderer total, input event to acknowledgement | under one frame at 120 Hz (8 ms) |
| Answer `layout.move` for one position | 2 ms |
| Scroll frame with all lines held | 8 ms |

Together with the engine's 5 ms (ENGINE 14), a keystroke is on screen within
one 60 Hz frame.

Means: line elements are reused across splices; face changes touch the
stylesheet, not elements; reads of layout (rectangles) are batched before
writes within a frame; selection and cursor layers are separate from text so
moving the cursor does not re-lay-out lines.

## 12. Testing

- **Protocol conformance.** The recorded sessions that drive the terminal
  frontend and headless client (ENGINE 14) are replayed against the renderer
  by a fake engine; the resulting model must match.
- **Geometry agreement.** With a monospaced face and ASCII text, every
  `layout.*` answer must equal the reference monospace layout module's.
- **Position round trips** between code points, UTF-16 and pixels on a corpus
  with astral characters, combining sequences and bidirectional text.
- **Security.** A corpus of hostile buffer contents (markup, script URLs,
  resource ids not defined) must render as inert text.
- **End to end.** `npm run smoke` starts the real app, off screen, against a
  real engine and runs a script in the main process: open a 100,000-line
  file in a proportional font, scroll, edit above and inside the viewport,
  reload the page, show hostile text. It prints one line of JSON results and
  exits 0 or 1. Scenarios are added to it as stages land (type, split,
  prompt, reconnect).
- **Performance.** The budgets in section 11.

## 13. Implementation stages

Each depends on the engine stage named and ends with something runnable.
The frontend is a track parallel to the engine's stages, not a stage after
them (ENGINE.md 13.1): a frontend stage can start as soon as its engine
stage is done.

**F0: shell and connection** (engine stage 0). The project lives in
`frontend/` at the top of the repository. Main process, engine
discovery, relay, preload, sandbox and policy; the renderer shows a log of
messages. *Runnable:* the app starts, connects and prints `api_info`.

**F1: one view** (engine stage 2). Model, `lines` splices, faces, line
rendering, virtual scrolling, viewport reports, `reveal`, `flush` and
acknowledgement, `reset` and `ui.resync`. *Runnable:* open a large file and
scroll it; reload the page and get the same display back.

**F2: input and geometry** (engine stage 3). Key translation, EditContext,
selection and cursor layers, position conversion, the `layout.*` requests,
messages, the prompt area and popup. *Runnable:* edit with several cursors,
move by visual line in wrapped proportional text, answer a prompt with
completion.

**F3: frame chrome** (engine stage 6). Window tree with dividers, modelines,
gutter, margins and tab stops, panels docked and floating with tabs, inline
minibuffers. *Runnable:* the debugger's layout, with source, a bottom panel
and breakpoints in the gutter.

**F4: the rest of the protocol** (engine stages 3 and 6). Images and the
resource protocol, clipboard, mouse with click counts, file drop, the
application menu, several windows, reconnect with reattach. *Runnable:* an image in a buffer; a second window on the same
buffer; kill the socket and recover.

**F5: finish** (engine stage 7). Performance budgets enforced, themes, basic accessibility
(section 1), packaging hooks for DISTRIBUTION.md.

## 14. Open questions

None at present. The default fonts are chosen in DISTRIBUTION.md section 9.

## 15. Protocol additions this document needed

All are now in ENGINE.md (draft 13):

- `input.mouse` carries a click count.
- `input.drop(paths, view, line, offset)`.
- `ui.open_frame(options)`, engine to client.
- The `menu` redraw event.
