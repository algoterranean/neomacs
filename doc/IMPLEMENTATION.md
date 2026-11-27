# Implementation plan

Status: plan written 2026-11-27. Steps 1 to 3 done; see the progress log (section 9).

This is the working checklist for building Neomacs. It puts the stages of the
five design documents into one order and says, for each step, what to read,
what to build, how to tell it is done, and what must be checked before going
on. The design documents remain the specification:

| Document | Covers | Stage prefix |
|---|---|---|
| ENGINE.md | Engine core and the RPC protocol | Stage 0 to 7 |
| FRONTEND.md | Electron frontend | F0 to F5 |
| EXTENSIONS.md | Extensions and packaging | X0 to X4 |
| DISTRIBUTION.md | Packaging, signing, updates | P0 to P5 |
| STABILITY.md | API and protocol stability | T0 to T5 |

## 1. How to use this document

- Work one step at a time, in order. Do not start a step until the previous
  step's "Done when" holds and any gate after it has been passed.
- Before starting a step, read the sections listed under "Read first". They
  are the specification; the checklist here is an index into them, not a
  replacement.
- Tick a box when the item is built and tested, not when it is started.
- When what was built differs from the design, change the design document in
  the same commit and add a line to the progress log (section 9). A design
  document that no longer describes the code is a bug.
- Make one commit at the end of each step, containing the code, the ticked
  boxes and the progress log row.
- If something in a design document turns out to be wrong or missing, stop
  and fix the document first. Section 8 records the cases found so far.

### 1.1 Done, for every step

- [ ] Tests for the step pass on the free-threaded 3.15 build.
- [ ] The whole existing suite still passes.
- [ ] The GIL is still disabled after engine startup with no optional
      packages (ENGINE 12).
- [ ] Type checking is clean in strict mode.
- [ ] From step 2 on: the API manifest is regenerated and committed
      (STABILITY 3.4).
- [ ] The step's "Runnable" scenario has actually been run by hand once.
- [ ] Progress log updated.

### 1.2 Standing rules

- Python is free-threaded CPython 3.15 or newer (ENGINE D13). The engine has
  no required dependency with compiled code.
- MIT licence. Nothing is copied from Emacs, code or documentation
  (ENGINE D15).
- Editor state is touched only by the owner thread (ENGINE D1). Every buffer
  mutator asserts this in tests.
- Text mutation never awaits (ENGINE D2).
- Standard commands in `std/` use only the public API (ENGINE 12).
- Every new public name gets a stability marker, provisional until 1.0
  (STABILITY S4).
- Anything on the deferred lists (section 7) is not built, however close at
  hand it seems.

### 1.3 Repository layout

```
doc/                 the design documents and this plan
neomacs/             the Python package (ENGINE 12)
  frontends/         monolayout.py and tui.py
tests/
frontend/            the Electron application (FRONTEND.md), from step 4
packaging/           build and signing scripts (DISTRIBUTION.md), from step 13
pyproject.toml
```

Tooling (ENGINE 12): `uv` for
environments and locking, `pytest` and `hypothesis` for tests, `pyright` in
strict mode for types, `ruff` for linting and formatting.

## 2. Order at a glance

| Step | Stages | What exists afterwards |
|---|---|---|
| 1 | Stage 0, P0 | An engine process you can connect to and evaluate Python in; an installable wheel; CI |
| 2 | Stage 1, T0 | Buffers, markers, undo, files; the API manifest |
| 3 | Stage 2, T1 | A terminal editor you can view and type in; the protocol version check |
| 4 | F0, F1 | An Electron window showing and scrolling a file in proportional fonts |
| — | Gate A | The protocol's layout model is confirmed or corrected |
| 5 | Stage 3, T2 | A usable minimal editor: commands, keys, multiple cursors, prompts |
| — | Gate B | Typing latency is within budget |
| 6 | F2 | Typing and cursor motion in Electron |
| — | Gate C | Geometry requests work in wrapped proportional text |
| 7 | Stage 4 | Live evaluation |
| — | Gate D | The editor survives re-evaluating its own modules |
| 8 | Stage 5 | The debugger |
| 9 | Stage 6 | Modes, syntax, highlighting, splits, panels |
| 10 | F3, F4 | The full Electron interface |
| 11 | Stage 7, X0 | Processes, timers, the init file, project settings |
| 12 | X1 to X3, F5 | Installable extensions; frontend performance and themes |
| 13 | P1 to P3 | A signed desktop app |
| 14 | X4, T4, T3, P4 | Extension developer tools, release checks, updates |
| 15 | T5, P5 | 1.0, then Windows |

## 3. Steps

### Step 1: engine skeleton and the wheel (Stage 0, P0)

**Read first:** ENGINE 3, 4.1, 4.2 (the `engine.*` rows), 4.8 ("Writing never
blocks"), 12, 13 (Stage 0). DISTRIBUTION 3.1, 5, 13 (P0).

**Build:**

- [x] `pyproject.toml`: name `neomacs`, `requires-python >= 3.15`, MIT, the
      `neomacs` console entry point; `LICENSE`; a short `README`.
- [x] Package skeleton following ENGINE 12, empty modules where needed.
- [x] `Editor`, and `ed` as a proxy to the current editor through a context
      variable, so tests can make their own.
- [x] Transport: listen on a Unix socket (directory 0700, socket 0600) and on
      stdio; newline-delimited JSON; a reader thread that posts to the loop;
      a writer queue so the main loop never blocks on the socket.
- [x] Socket path length check with the `/tmp/neomacs-<uid>/` fallback
      (DISTRIBUTION 5).
- [x] JSON-RPC layer: requests, responses and notifications in both
      directions, each side numbering its own ids; errors carry the Python
      exception type and traceback.
- [x] `engine.api_info`, generated from signatures and docstrings.
- [x] `engine.eval` into a scratch module. A plain `compile` and `exec` is
      enough here; the real pipeline is step 7.
- [x] `engine.call`, returning "no such command" until step 5.
- [x] The error wrapper (ENGINE 3): nothing evaluated can kill the engine.
- [x] `NEOMACS_RPC_LOG`.
- [x] A Python client for tests: connect, call, collect notifications. This
      is the seed of `neomacs.testing`.
- [ ] CI: Linux and macOS on free-threaded 3.15, plus Windows as a
      non-blocking job; tests, type check, lint, the GIL check; build the
      wheel and install it into a clean environment. *Workflow written
      (`.github/workflows/ci.yml`) but never run: the repository has no
      remote. The same checks were run locally.*

**Done when:** start the engine on a socket, connect from a script, evaluate
`1 + 1`, and see an exception come back as an error with the engine still
running. A wheel built by CI installs into a clean environment and starts.

**Needs the user:** registering `neomacs` on PyPI, and a remote for the
repository if CI is to run.

### Step 2: text core and the API manifest (Stage 1, T0)

**Read first:** ENGINE 5.1, 5.2, 5.3, 5.4 (spans only), 5.5, 5.6, 5.8, 13
(Stage 1), 14 (unit, property and grapheme tests). STABILITY 3.1, 3.4, 10
(T0).

**Build:**

- [x] `TextStore` protocol and the chunked implementation, including the
      per-chunk UTF-8 byte lengths and `byte_of` / `pos_of_byte` (needed in
      step 9, cheap to do now).
- [x] `Buffer`, with every mutation going through `replace` in the order
      ENGINE 5.2 gives.
- [x] `Marker` with insertion gravity, weakly held; `Cursor`.
- [x] Grapheme boundary functions on `Buffer`, built on
      `unicodedata.iter_graphemes`, with the ASCII fast path and the
      one-line cache.
- [x] `buf.search`, on the cached snapshot.
- [x] Undo log with groups tagged by an invocation id (a counter for now;
      the dispatcher supplies real ids in step 5), linear undo and redo.
- [x] A minimal `Hook`, enough for `before_change` and `after_change`.
- [x] File read and write: decoding, line endings, write to temporary and
      rename, `before_save` and `after_save`.
- [x] Span layers: storage, tracking through edits, the property dict.
      Decoration providers wait for step 9.
- [x] The owner-thread assertion in every mutator, on in tests.
- [x] T0: `@stable` and `@provisional` markers; the manifest generator; the
      committed `api/manifest.json`; the CI check that regenerating it
      changes nothing.

**Tests:** property tests of the store, markers and undo against a plain
`str` model; `GraphemeBreakTest.txt` for the library's Unicode version; the
grapheme corpus of ENGINE 14.

**Done when:** over `engine.eval`, open a file, edit it, undo, save.
Changing a marked signature fails CI until the manifest is regenerated.

### Step 3: display and the terminal frontend (Stage 2, T1)

**Read first:** ENGINE 4.2, 4.3, 4.4, 4.6, 4.8, 5.4 (decoration providers),
8.2, 8.3, 8.4, 13 (Stage 2), 14 (view, pacing, reattach, frame tests).
STABILITY 5, 10 (T1).

**Build:**

- [x] `View` and `Window`; one window per frame for now.
- [x] Face registry and `face_define`.
- [x] The view builder: text, providers and spans merged into lines of runs.
- [x] Per-view tracking of which lines the frontend holds and their
      revisions; `lines` splices, including count-only splices outside the
      held range.
- [x] `win_info` with line count, wrap, gutter columns, margins, tab width;
      gutter marks in line attributes.
- [x] `selections` (one selection for now, sent as a list), `modeline`,
      `reveal`.
- [x] `view.viewport` handling, with the margin of extra lines.
- [x] `flush` with a sequence number, `view.ack`, one batch in flight per
      frame, the stalled-frontend rule.
- [x] Engine-to-client requests, with replies routed back to the awaiting
      coroutine; the `layout.*` calls as awaitable methods.
- [x] Frames: `ui.attach`, `ui.detach`, detach on connection loss, the
      reattach token, expiry, `FrontendGone`; `reset` and the full state
      sync; `ui.resync`.
- [x] `monolayout`: wrapping, wide characters, tab stops, scrolling, and
      answers to every `layout.*` request, in character cells.
- [x] The terminal frontend on top of `monolayout`.
- [x] The headless test client, with a screen model built on `monolayout`.
- [x] Hard-wired self-insert and cursor movement, to be replaced in step 5.
- [x] T1: the protocol integer and capability list in `api_info` and
      `ui.attach`; refusal on mismatch.

**Tests:** view tests; pacing tests; reattach tests; two clients on one
buffer. Start recording sessions for protocol conformance (ENGINE 14).

**Done when:** view and type into a file in a terminal; kill the client,
reattach, and find the same state.

### Step 4: Electron shell and one view (F0, F1)

**Read first:** FRONTEND 2, 3, 4, 5.1 to 5.4, 9, 12, 13 (F0, F1).
DISTRIBUTION 7 (the app starts its own engine).

**Build:**

- [ ] `frontend/`: TypeScript project, pinned Electron, build and test
      scripts, lock file.
- [ ] Main process: choose a socket path, start the engine as a child,
      connect, relay lines to the renderer over a `MessagePort`.
- [ ] Preload with the minimal exposed surface; sandbox, context isolation
      and content security policy as in FRONTEND 9.
- [ ] Renderer protocol client and the per-frame, per-view model.
- [ ] Line rendering from runs; faces as generated CSS rules; `tab-size`;
      wrap modes.
- [ ] Height index, virtual scrolling, scroll anchoring, placeholders for
      lines not held.
- [ ] `view.viewport` reports; `reveal`.
- [ ] Render on `flush`, acknowledge after paint, immediately when hidden.
- [ ] `reset` and `ui.resync` on reload.
- [ ] The security corpus test: hostile buffer text renders inert.

**Done when:** the app starts with no terminal involved, opens a large file,
scrolls it smoothly in a proportional font with a second face at a larger
size, and shows the same thing after the page is reloaded.

### Gate A: the layout model

The protocol's central bet is that the frontend owns layout (ENGINE D5). The
terminal frontend cannot test it. Before building the command system on top:

- [ ] Scrolling a 100,000-line file never shows a placeholder for more than
      a frame or two at ordinary scrolling speed.
- [ ] Editing near the top of a long wrapped file does not make the viewport
      jump.
- [ ] The line-based `lines` splice was sufficient; note any event that had
      to be added or changed.
- [ ] Any protocol change is made in ENGINE 4 and in both frontends now.

Record the outcome in the progress log, including "no changes needed".

### Step 5: the command system (Stage 3, T2)

This is the largest step. The sub-headings are a suggested order within it.

**Read first:** ENGINE 4.5, 5.7, 5.9 (table and scanner), 6, 7.1 to 7.7,
8.1, 8.5 (roles), 8.6, 11, 13 (Stage 3), 14 (selection, concurrency,
minibuffer, prefix, clipboard, context tests). STABILITY 3.5, 4, 10 (T2).

**Registries:**

- [ ] `@command` with parameter annotations; the flat registry.
- [ ] Every registry records the registering module (ENGINE D30), from the
      start.
- [ ] `Option`, the lookup chain, `on_change`, pending values for
      undeclared options.
- [ ] `Hook`, global and per-buffer, idempotent add, coroutine functions
      awaited; the standard hooks.
- [ ] Key notation parser; `Keymap`; the lookup order of ENGINE 7.2.

**Dispatch:**

- [ ] Command tasks; the lane; release and re-take of the lane around
      waiting for input.
- [ ] Argument resolution, including `KeyText`.
- [ ] Context variables: pinned in commands, looked up elsewhere, `NoFrame`,
      `ed.using` (ENGINE 7.7).
- [ ] Cancellation: task cancel, and the 200 ms fallback interrupt from the
      reader thread.
- [ ] Background commands, `ed.start_task`, `ed.tasks`.
- [ ] Prefix arguments (ENGINE 7.6) and `last_command`.
- [ ] Undo groups keyed by real invocation ids.

**Selections:**

- [ ] `Selection`, `SelectionSet`, extending, normalisation with snapping to
      cluster boundaries, last selection.
- [ ] Once-per-selection dispatch.
- [ ] Jump list.
- [ ] Kill ring; `clipboard.set` and `clipboard.get`; yank's rules.

**Minibuffers and messages:**

- [ ] `Minibuffer` as a view; the prompt area and inline hosts; single-line
      and multi-line; `MinibufferMode`.
- [ ] `ed.minibuffer.read`; the `Ask.*` annotations.
- [ ] `Completer`, cancellation of a stale call, `popup_show`.
- [ ] Named history lists.
- [ ] `ed.message`, the `message` event, `*messages*`.

**Syntax, first layer:**

- [ ] `Syntax` table and defaults; the scanner with its line-state cache;
      `BufferSyntax` answering from the scanner.
- [ ] Word motion over clusters.

**Remaining protocol and commands:**

- [ ] `input.text`, `input.paste`, `input.mouse` with click counts and
      areas, `input.drop`.
- [ ] `ed.show` with roles, every role placed in the main area.
- [ ] Menu registry and the `menu` event.
- [ ] `new_frame` and `ui.open_frame`.
- [ ] Standard commands: movement, selecting, adding and removing cursors,
      editing, kill and yank, undo and redo, files, buffers, run a command by
      name, `keyboard_quit`.
- [ ] T2: `@deprecated`, one warning per calling module, command and option
      aliases with value migration.

**Done when:** a usable minimal editor in the terminal. A deliberately slow
command leaves the display live, keeps typed-ahead keys in order, and is
cancelled by `C-g`. Typing with three cursors edits three places and undoes
in one step.

### Gate B: latency

- [ ] The benchmark of ENGINE 14 exists and runs in CI: `input.key` received
      to `flush` sent, for self-insert in a 10,000-line file.
- [ ] 99th percentile under 5 ms on the reference machine.

If it fails: profile first. If the cost is in the text store or the view
builder, this is the point to consider the native replacement that ENGINE 16
defers, before more is built on the slow path. Re-run with highlighting on
at the end of step 9.

### Step 6: input and geometry in Electron (F2)

**Read first:** FRONTEND 5.5, 5.6 (prompt area, popup, messages), 6, 7, 8.1,
8.2, 13 (F2).

**Build:**

- [ ] Key translation to the engine's notation; Option as Meta by default;
      `input.interrupt` with `C-g`.
- [ ] `EditContext`: composition drawn locally, `input.text` on commit,
      cursor rectangle reported back.
- [ ] Code point and UTF-16 conversion per line.
- [ ] Position to pixels and pixels to position.
- [ ] Selection and cursor layers for many selections; cursor animation
      with its cut-offs (FRONTEND F12).
- [ ] Every `layout.*` request, including waiting for lines that are not
      held and the 250 ms error fallback.
- [ ] The prompt palette, popup, and message strip.

**Done when:** edit with several cursors, move by visual line in wrapped
proportional text, and answer a prompt with completion, all in the Electron
window.

### Gate C: geometry

- [ ] With a monospaced face and ASCII text, every `layout.*` answer equals
      `monolayout`'s (FRONTEND 12).
- [ ] Vertical motion keeps its horizontal goal across lines of different
      heights and fonts.
- [ ] One `layout.move` round trip, engine to engine, fits inside the
      keystroke budget.

### Step 7: live evaluation (Stage 4)

**Read first:** ENGINE 9 (all of it), 13 (Stage 4), 14 (live-patch tests).

**Build:**

- [ ] The pipeline of ENGINE 9.1: parse, shift line numbers, split a trailing
      expression, compile with top-level `await`, execute, await if needed.
- [ ] The namespace rule: a file's module, or the scratch module.
- [ ] In-place patching of functions, decorated functions and classes, with
      a visible warning on every fallback to rebinding.
- [ ] After evaluating a whole module, remove registrations it made before
      and did not make this time, using the ownership record.
- [ ] `linecache` registration, so tracebacks and `inspect` see buffer text.
- [ ] `keep` and `forget`.
- [ ] Loading a module from disk goes through the same pipeline.
- [ ] Commands: `eval_last_expression`, `eval_definition`, `eval_selection`,
      `eval_buffer`, `eval_expression`, `repl`.
- [ ] The scratch buffer; the `*traceback*` buffer with a span per frame.
- [ ] `describe_command`, `describe_key`, `describe_option`, `find_source`,
      `apropos`.

**Done when:** from the scratch buffer, redefine a built-in command and
rebind a key, and see both take effect on the next keystroke; jump to the
source of any command.

From here on, develop inside the editor where practical.

### Gate D: patching on real code

- [ ] A script evaluates every module under `std/` and `modes/` with
      `eval_buffer` in a running engine, then runs the test suite inside
      that engine. It passes, with no fallback warnings that are not
      understood and noted.

### Step 8: the debugger (Stage 5)

**Read first:** ENGINE D1, D18, 7.7 (`C-g` while paused), 10 (all of it), 13
(Stage 5), 14 (debugger tests), 15 (the `sys.monitoring` and hand-over
rows).

**First:** confirm on the chosen Python build that `sys.monitoring` delivers
line events to a callback that blocks, that raising from a callback
propagates into the monitored frame, and that assignment through
`frame.f_locals` takes effect. Record the result.

**Build:**

- [ ] The debug loop on its own thread; the single hand-over point for
      ownership of editor state; reply routing to the loop that asked.
- [ ] Input routed to the debug loop while paused, including `C-g`.
- [ ] Breakpoints as spans in a `debug` layer, with gutter marks; resolution
      to code objects; re-application after re-evaluation.
- [ ] `sys.breakpointhook`.
- [ ] Step, next, out, continue, including across `await`; abort.
- [ ] The `*debug*` buffer; stack navigation; evaluation in the selected
      frame.
- [ ] Post-mortem inspection from `*traceback*`; `debug_on_raise`.
- [ ] The paused state shown in every frame's modeline.

**Done when:** set a breakpoint in a built-in command, press its key, step
through it while scrolling other buffers, change a local, continue.

### Step 9: modes, syntax, panels (Stage 6)

**Read first:** ENGINE 4.3 (panels, paragraph attributes, resources), 5.4,
5.9, 7.3, 8.2, 8.5, 13 (Stage 6), 14 (syntax and placement tests), 15 (the
tree-sitter row).

**First:** recheck tree-sitter on the free-threaded build (last checked
2026-11-24: no wheel, re-enables the GIL, the Python grammar failed to
import). Update the risk row in ENGINE 15 with the result.

**Build:**

- [ ] `Mode`: major and minor, per-buffer instances, inheritance of keymaps,
      defaults and syntax tables, selection by pattern and interpreter line,
      generated toggle commands.
- [ ] Fundamental, text and Python modes.
- [ ] Decoration providers with per-line caching; the scanner as a provider.
- [ ] The tree-sitter layer, optional: adapter with byte offsets,
      incremental reparse, first parse in a worker thread, error-node
      fallback to the scanner.
- [ ] Python query files with the fixed capture vocabulary.
- [ ] Commands: expression motion, expand and shrink selection, matching
      bracket, toggle comment, definition start and end, indent line; the
      auto-pair minor mode; the matching-bracket highlight.
- [ ] Incremental search with a transient keymap; `add_next_match`,
      `select_all_matches`.
- [ ] Window splits.
- [ ] Panels: docked and floating, tabs, the default placement table,
      `ui.panel`; the terminal frontend draws them.
- [ ] Paragraph attributes on lines; resources and inline images.

**Done when:** highlighted Python with structural selection and search, a
cursor on every match of a word, and the same session with tree-sitter
uninstalled. Then re-run Gate B with highlighting on.

### Step 10: the full Electron interface (F3, F4)

**Read first:** FRONTEND 5.3, 5.6, 8.3, 8.4, 9 (resource protocol), 10, 13
(F3, F4).

**Build:**

- [ ] Window tree with draggable dividers; modelines.
- [ ] Gutter columns, margins, maximum width.
- [ ] Panels docked and floating, tab strips, drag between sides; nothing
      changes until the engine's `layout` arrives.
- [ ] Inline minibuffers as nested views.
- [ ] Images through the private resource protocol.
- [ ] Clipboard through the main process; `input.paste` for menu paste and
      middle click.
- [ ] Mouse with click counts and areas; file drop.
- [ ] The application menu built from the `menu` event.
- [ ] Several windows; reconnect with reattach; the "engine gone" dialog.

**Done when:** the debugger's layout works in Electron, with source, a
bottom panel and breakpoints in the gutter; an image shows in a buffer; a
second window shows the same buffer; killing the socket is recovered from.

### Step 11: processes, startup and local configuration (Stage 7, X0)

**Read first:** ENGINE 6 (project layer), 11, 13 (Stage 7). EXTENSIONS 3.2,
3.3, 6.1, 8.1, 11 (X0). DISTRIBUTION 5.

**Build:**

- [ ] Timers; `ed.run` and `ed.spawn`; `ed.in_thread`.
- [ ] One module that knows the configuration, data, cache and runtime
      directories for each platform.
- [ ] `init.py`, loaded last, with an ownership record.
- [ ] Single-file extensions as `neomacs_user.<name>`.
- [ ] `--safe`.
- [ ] Project settings: `.neomacs/settings.toml`, `project=True` options
      only, precedence, reload, the skipped-keys message.
- [ ] The crash-recovery journal for unsaved buffers.

**Done when:** a shell command's output streams into a buffer;
configuration survives a restart; killing the engine and starting it again
offers the unsaved text back.

### Step 12: extensions, and finishing the frontend (X1 to X3, F5)

**Read first:** EXTENSIONS 3.1, 4, 5, 6.2, 7, 8, 10, 11, 12. FRONTEND 11, 13
(F5).

**Build, X1:**

- [ ] Disable an extension by ownership; reload with removal of stale
      registrations; `requires_restart`.

**Build, X2:**

- [ ] Entry point discovery in the group `neomacs.extensions`.
- [ ] `extensions.toml` and `extensions.lock`.
- [ ] The extensions environment, on `sys.path` after the engine's own
      packages, with the engine's versions passed to the resolver as
      constraints.
- [ ] `uv`, pinned; add, remove, upgrade, sync, list, in the editor and as
      `neomacs ext`, running as background tasks.
- [ ] Before installing: show every distribution to be added or changed, and
      which have no free-threaded wheel.
- [ ] After each import: record which extension re-enabled the GIL.
- [ ] The warning for global key bindings made on load.

**Build, X3:**

- [ ] The autoload index, compiled at install time; command and mode stubs.

**Build, F5:**

- [ ] The performance budgets of FRONTEND 11, enforced in CI.
- [ ] Themes.
- [ ] Labels on views and regions; messages announced.

**Done when:** two extensions with a shared dependency install; a third
that conflicts is refused with both requirements named; fifty installed
extensions add no measurable startup time.

### Step 13: the desktop app (P1 to P3)

**Read first:** DISTRIBUTION 3.2, 4, 7, 8.1, 8.2, 9, 10, 13, 14.

**First:** settle the facts in section 5 marked for this step.

**Build:**

- [ ] The build pipeline of DISTRIBUTION 10, steps 1 to 6, 8 and 9.
- [ ] Bundled Python, precompiled bytecode, pinned `uv`, fonts, licence
      bundle.
- [ ] The packaged smoke test.
- [ ] macOS signing, entitlements and notarisation; Linux signatures.
- [ ] A real compiled extension loads in the signed macOS build.
- [ ] Single-instance behaviour and `--new-instance`.
- [ ] The `neomacs` command installer; `neomacs FILE`; `--tui` and
      `--tui --attach`; file associations.

**Done when:** a downloaded build starts on a clean machine with no Python
and no security warnings.

**Needs the user:** an Apple Developer ID, signing certificates, CI secrets.

### Step 14: developer tools, release checks, updates (X4, T4, T3, P4)

**Read first:** EXTENSIONS 9. STABILITY 3.4, 4, 7, 9, 10. DISTRIBUTION 8.3.

**Build:**

- [ ] `neomacs.testing` as a pytest plugin.
- [ ] `neomacs ext check` and `neomacs ext new`.
- [ ] The manifest difference classifier and the version bump check.
- [ ] The registry dump for the user contract.
- [ ] Changelog generation.
- [ ] Updates from GitHub Releases: background download, signature check,
      apply on next start; the nightly channel; package-manager recipes.
- [ ] The first-start check of extensions against a new engine version.

**Done when:** a nightly build updates itself to the next nightly; a pull
request that removes a provisional name without a version bump fails CI.

### Step 15: 1.0, then Windows (T5, P5)

**Read first:** STABILITY 3.2, 6, 11. DISTRIBUTION 6, 13 (P5).

- [ ] Agree the stable list with the user; change the markers; enable the
      major-version and deprecation rules.
- [ ] Release 1.0.
- [ ] Windows: transport (named pipe, unless Unix sockets prove usable),
      the fallback interrupt, subprocesses, file replacement; installer and
      signing.

## 4. Gates

| Gate | After step | Question | If it fails |
|---|---|---|---|
| A | 4 | Does frontend-owned layout work in a real proportional-font frontend? | Change ENGINE 4 and both frontends before step 5. |
| B | 5, again after 9 | Is typing within the 5 ms engine budget? | Profile; consider the native text store or view builder. |
| C | 6 | Are geometry requests correct and fast enough? | Revisit which motions the engine can do without asking. |
| D | 7 | Does in-place patching survive the editor's own code? | Fix the patcher, or record the limitation and its warning. |

## 5. Facts to verify, and when

| Fact | Where it is assumed | Verify at |
|---|---|---|
| `sys.monitoring` callbacks may block, may raise into the monitored frame, and work under free threading | ENGINE 10.3 | Start of step 8 |
| Assignment through `frame.f_locals` changes the paused frame's locals | ENGINE 10.4 | Start of step 8 |
| `tree-sitter` loads on the free-threaded build without re-enabling the GIL | ENGINE 15 | Start of step 9 |
| `EditContext` behaves as needed in the pinned Electron's Chromium | FRONTEND F3 | Step 6 |
| Standalone free-threaded 3.15 builds exist for every target | DISTRIBUTION 3.2 | Start of step 13 |
| macOS accepts disabling library validation on the Python executable only | DISTRIBUTION 8.1 | Step 13 |
| JetBrains Mono and Inter are under the SIL Open Font License at the bundled versions | DISTRIBUTION 9 | Step 13 |
| Minimum OS versions match what Electron and the Python build support | DISTRIBUTION 4 | Step 13 |
| Whether Python's Unix sockets on Windows work under asyncio | DISTRIBUTION 6 | Step 15 |
| The name `neomacs` is still free on PyPI | EXTENSIONS 13 | Step 1 |

## 6. What needs the user

- Registering `neomacs` on PyPI (step 1).
- A remote repository, for CI (step 1).
- Signing identities and CI secrets (step 13).
- The 1.0 stable list (step 15).
- Any change of direction a gate calls for.

## 7. Not to be built

Deferred by decision. Each is described where it is listed.

- ENGINE 16: embedded frontend content; undo tree; native text store, unless
  Gate B demands it; what happens to unsaved buffers at exit; external file
  changes; embedded languages; word segmentation for scripts without
  spaces; normalisation in search; repeat and keyboard macros; persistent
  history; very long lines; length-header framing.
- FRONTEND 1: full screen reader support.
- EXTENSIONS 8.1: running code from a project directory.
- DISTRIBUTION 2: Windows before step 15.
- STABILITY S9: maintenance of older major versions.

## 8. Corrections made to the design documents

Found while writing this plan, and fixed in the design documents on
2026-11-27. Listed so the reasoning is not lost.

1. **Spans moved to Stage 1.** They were under Stage 6, but key lookup
   (Stage 3), the traceback buffer (Stage 4) and breakpoints (Stage 5) all
   use them. Only decoration providers remain in Stage 6.
2. **The terminal frontend moved inside the package**, to
   `neomacs/frontends/`. It was under `tools/`, but the wheel ships it.
3. **The ownership record moved to Stage 3.** It was introduced in X1, but
   live evaluation in Stage 4 uses it to drop stale registrations. X1 now
   covers only disabling and reloading.
4. **Project settings were given a stage:** engine Stage 7 and X0.
5. **Development tooling and the top-level directories were named** in
   ENGINE 12, FRONTEND 13 and DISTRIBUTION 10.

## 9. Progress log

Add a row when a step or gate is finished, or when the design changed.

| Date | Step or gate | Commit | Notes: what differed from the design, what was verified |
|---|---|---|---|
| 2026-11-27 | Planning | | Plan written. Five corrections made to the design documents (section 8). |
| 2026-11-27 | Step 1 | this commit | Done except CI, which is written but has never run (no remote). PyPI name not registered (needs the user). Differences from the design: `wcwidth` dropped in favour of `unicodedata`, since its current release ships compiled wheels (ENGINE 12, DISTRIBUTION 3.1 updated); `engine.exit` notification, the `NEOMACS-LISTENING` readiness line and `--exit-with-parent` added (ENGINE 4.1, 4.2). Verified by hand: `1 + 1` over a socket and over stdio; an exception returns an error and the engine keeps running; a built wheel installs into a clean free-threaded 3.15 environment and starts with the GIL disabled. |
| 2026-11-27 | Step 2 | this commit | Done. No differences from the design. `GraphemeBreakTest.txt` for Unicode 17.0 is vendored under `tests/data/` (Unicode licence) so the test needs no network; it is skipped if the library's Unicode version has no matching file. The type checker's stubs do not know `unicodedata.iter_graphemes` yet, so that one call is typed as `Any`. Verified by hand over `engine.eval`: open a file, edit, undo, save. |
| 2026-11-27 | Step 3 | this commit | Done. Protocol additions, all now in ENGINE 4: the `held` event and the three rules for what a frontend holds (the design said only "the held range"); `ui_faces`, so a frontend knows which face ids to draw selections, cursors and the gutter with; `engine.idle` for tests; `<space>` as the name of the space key. Other differences: the engine keeps an estimate of the viewport when a view shows another buffer or lines change above it, because a frontend reports its viewport only when it changes; frames are checked for updates after every RPC call, so a cursor moved by evaluated code is shown. A window's cursor advances when text is inserted exactly at it. The first conformance recording is `tests/data/sessions/basic.json`, made by `python -m neomacs.tools.record`. Verified by hand: none beyond the tests, which drive the terminal frontend in a pseudo-terminal (open a file, move, type, undo, quit) and reattach after a dropped connection. |
| | | | |
