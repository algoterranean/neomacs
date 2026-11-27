# Extensions and packaging design

Status: draft 4, 2026-11-27. Nothing is implemented. The decisions in
section 2 are confirmed; section 13 lists what is still open.

This describes how `neomacs` is extended: what an extension is, how it is
installed, how its dependencies coexist with everyone else's, and how it is
loaded, reloaded and removed in a running editor. It assumes ENGINE.md.

## 1. Goals and non-goals

Goals:

- An extension is ordinary Python using the same API as the core. There is
  no second, restricted plugin API.
- Installing, upgrading and removing extensions works in the running editor
  wherever Python allows, in keeping with live evaluation.
- Dependency conflicts between extensions are found when installing, not as
  an import error some time later.
- A user's set of extensions is reproducible from two text files.
- Many installed extensions do not slow startup.

Non-goals:

- Sandboxing. An extension can do anything the user can (ENGINE section 1).
- Isolating extensions from each other at run time. They share one
  interpreter.
- Extensions in other languages. A separate program can drive the editor
  over the RPC socket, but that is a client, not an extension.
- Running our own package index, at least at first.

## 2. Decisions taken

| # | Decision | Reason |
|---|---|---|
| X1 | An extension is a standard Python distribution that declares an entry point in the group `neomacs.extensions`. A single `.py` file in the user's extensions directory is also an extension. | Uses the packaging ecosystem as it is. The single-file form keeps small things small. |
| X2 | Extensions come from PyPI, a git URL or a local path. There is no `neomacs` index. | No infrastructure to run; private and unpublished extensions work the same way. |
| X3 | All extensions and their dependencies live in one environment, separate from the engine's own installation, resolved together in a single pass and pinned in a lock file. | One interpreter can import only one version of a package. Resolving the whole set at once turns a conflict into an install-time error. |
| X4 | The user's extension set is declared in `extensions.toml`; the resolved result is `extensions.lock`. | Reproducible, reviewable, and can be kept in version control with the init file. |
| X5 | Every registration the engine accepts (command, option, hook function, mode, key binding, timer, task, face, placement entry) records which module made it. | This ownership record is what makes disabling, reloading and upgrading an extension in place possible. |
| X6 | Extensions load lazily. An extension can declare its commands, modes and file patterns statically, and the engine registers stubs that import it on first use. | Startup cost should depend on what is used, not on what is installed. |
| X7 | Loading order is core, extensions in dependency order, then the user's `init.py`. | The user's file has the last word. |
| X8 | Resolution and installation are done by `uv`. | A fast resolver with lock files and support for free-threaded builds, shipped as a single binary. |
| X9 | An extension that binds keys in the global map when it loads is allowed to, and the engine warns. | Global keys are the user's to assign, but refusing outright would break extensions whose purpose is a set of global bindings. |
| X10 | Nothing upgrades, or checks for upgrades, on its own. | The user decides when code on their machine changes. |
| X11 | The distribution and import name are both `neomacs`. | The name was unclaimed on PyPI and npm on 2026-11-27. `neo` and `rig`, considered earlier, are both taken on PyPI by unrelated packages. |
| X12 | Extensions published on PyPI are named `neomacs-<name>`. | So people can find them. The editor itself recognises an extension by its entry point, so the prefix is a convention and not a mechanism. |
| X13 | A project may carry a `.neomacs/` directory of settings. It is data only: it can set options that have opted in, and can never run code. It is loaded without a trust prompt. | Per-project indentation and similar settings are expected. Limiting it to vetted options is what makes loading it automatically safe. |

## 3. What an extension is

### 3.1 A package

```toml
# pyproject.toml
[project]
name = "neomacs-git"
version = "0.3.0"
dependencies = ["neomacs>=1.2,<2", "pygit2>=1.15"]

[project.entry-points."neomacs.extensions"]
git = "neomacs_git"

[tool.neomacs]
summary = "Git status, staging and commit"

[tool.neomacs.autoload]
commands = ["git.status", "git.commit", "git.blame"]
modes = [{ name = "git-commit", patterns = ["COMMIT_EDITMSG"] }]
```

- The entry point names the module to import. Importing it is activating it:
  its `@command`, `Option`, `Hook.add` and mode definitions run and register.
- The module may define `activate()` and `deactivate()` for anything the
  decorators do not cover.
- `[tool.neomacs.autoload]` is optional and drives lazy loading (section 6).
- The dependency on the engine is an ordinary requirement, with the version
  range STABILITY.md describes.

### 3.2 A single file

A file `~/.config/neomacs/extensions/foo.py` is loaded as the module
`neomacs_user.foo`. It has no metadata, no dependencies of its own and no lazy
loading. It is the right size for a few commands.

- The file's name must be a Python identifier. A file whose name starts with
  `_` is not loaded on its own account; it is there to be imported.
- `neomacs_user` is a package with no file of its own, whose path is the
  extensions directory, so one single-file extension can import another:
  `from neomacs_user import helpers`. A module imported that way before its
  turn is not loaded a second time.
- It is loaded through the live evaluation pipeline (ENGINE 9), like a
  buffer: top-level `await` is allowed, and evaluating the file's buffer
  again replaces what it registered.

### 3.3 The init file

`~/.config/neomacs/init.py` is not an extension but is handled by the same
machinery: it is a module with an ownership record, and re-evaluating it
replaces what it registered before. Its module name is `neomacs_init`.

It is ordinary Python and imports what it uses:

```python
from neomacs import ed
from neomacs.commands import command
from neomacs.keymap import global_map

ed.options.fill_column = 72
global_map.bind("C-c t", "toggle_comment")
```

The command `init.open` visits it. Evaluating its buffer (`eval_buffer`)
applies it at once.

## 4. The extensions environment

```
engine installation (read-only)        ~/.local/share/neomacs/ext-env   (managed)
  python, neomacs, neomacs's own dependencies    site-packages of all extensions
                └──────────── sys.path: engine first, then ext-env ───────────┘
```

- The engine's own packages are first on `sys.path` and cannot be shadowed.
  Their versions are passed to the resolver as constraints, so an extension
  that needs an incompatible version of something the engine ships fails to
  resolve with a message saying so.
- Everything else is resolved together. Adding extension B when extension A
  is installed re-resolves the whole set. If A and B cannot agree on a
  shared dependency, the install stops and names both.
- The environment is built for the engine's exact interpreter, including the
  free-threaded ABI.

### 4.1 The two files

```toml
# ~/.config/neomacs/extensions.toml
[extensions]
neomacs-git = ">=0.3"
neomacs-rust = { git = "https://example.org/neomacs-rust.git", rev = "v0.2.1" }
my-tools = { path = "~/src/my-tools", editable = true }
```

`extensions.lock` records the exact version and hash of every distribution
in the environment. `sync` makes the environment match the lock; `add`,
`remove` and `upgrade` edit the manifest, re-resolve, rewrite the lock and
sync.

### 4.2 Commands

| In the editor | Command line | Effect |
|---|---|---|
| `extensions.add` | `neomacs ext add NAME` | Add to the manifest, resolve, install, activate. |
| `extensions.remove` | `neomacs ext remove NAME` | Deactivate, remove, re-resolve. |
| `extensions.upgrade` | `neomacs ext upgrade [NAME]` | Re-resolve allowing newer versions; reload what changed. |
| `extensions.sync` | `neomacs ext sync` | Make the environment match the lock file. |
| `extensions.list` | `neomacs ext list` | Name, version, state, and whether it re-enabled the GIL. |

Installation runs as a background task (ENGINE 7.1) with progress in the
task list; the editor stays usable.

### 4.3 Free-threaded builds

A dependency with a C extension needs a wheel for the free-threaded
interpreter, or must build from source, and may re-enable the GIL when
imported (ENGINE D13; this is what `tree-sitter` does today). The policy:

- Installing is allowed. Before installing, the user is shown which
  distributions have no free-threaded wheel.
- After activation the engine checks whether the GIL has come back on, and
  records which extension's import did it. `extensions.list` shows this.
- The engine works either way; only parallelism in worker threads is lost.

## 5. Ownership, disabling and reloading

The engine keeps, for every registration, the module that made it (X5). From
that:

- **Disable.** Call the extension's `deactivate()` if it has one; cancel its
  timers and tasks; remove its commands, options, hook functions, modes, key
  bindings, faces and placement entries; switch buffers in one of its modes
  to the fundamental mode. Values the user set for its options are kept, in
  case it comes back.
- **Reload.** Evaluate the extension's modules again through the live
  evaluation pipeline (ENGINE 9), so functions and classes are patched in
  place. Any registration the module made last time but did not make this
  time is removed. This is the same thing that happens when a user
  re-evaluates the file from a buffer.
- **Upgrade.** Install the new version, then reload.

What cannot be done live:

- A compiled module cannot be reloaded. If an upgrade changes one, the
  extension is marked "restart to finish upgrading" and keeps running the
  old code.
- An extension may set `requires_restart = true` in `[tool.neomacs]` if it knows
  it does not survive reloading.
- Objects an extension handed to other code (a completer stored somewhere, a
  callback passed to a third party) are beyond the ownership record. In-place
  patching usually keeps them working; it is not guaranteed.

## 6. Loading

### 6.1 Startup order

1. The engine core and standard commands.
2. For each installed extension, in dependency order: register its autoload
   stubs if it has an autoload table, otherwise import it.
3. Single-file extensions, in name order.
4. `init.py`.

An extension that fails to import is reported and skipped; the rest load.
`neomacs --safe` starts with no extensions and no init file.

The user's code is loaded before the engine accepts a connection, so nobody
is looking when it fails. Each failure goes to `*messages*` with its
traceback, the last one can be seen with `show_traceback`, and the first
frame to attach is shown the first failure and how many more there were.

### 6.2 Lazy loading

At install time the autoload tables of all extensions are compiled into one
index file. At startup the engine reads that file, and nothing else, to
create:

- **Command stubs.** A command of the declared name whose function imports
  the extension and then calls the real command. The stub carries the
  one-line documentation from the index, so the command appears in
  completion and help before the extension has loaded.
- **Mode stubs.** The declared file patterns are registered; visiting a
  matching file imports the extension and then enables the mode.

Two consequences of decisions already made in ENGINE.md make this cheap:

- Key bindings refer to commands by name, so `init.py` can bind a key to an
  extension's command without loading the extension.
- A value assigned to an option that is not yet declared is held as pending
  and validated when the extension declares the option.

Python 3.15's `lazy import` lets an extension's own heavy imports be deferred
further without restructuring its code.

## 7. Configuration

- An extension declares typed `Option`s (ENGINE 6). Users set them in
  `init.py` or interactively.
- Key bindings go in the extension's mode keymaps. An extension should not
  bind keys in the global map on import; it offers commands, and binding
  them globally is the user's decision. The engine does not prevent it. It
  writes one warning per extension to `*messages*`, naming the keys, and
  `neomacs ext check` reports the same thing to the author (X9).
- State that must survive re-evaluation uses `keep(...)` (ENGINE 9.4).
- Where a buffer the extension shows appears is a role in the placement
  table (ENGINE 8.5); an extension adds its roles with a default place and
  the user may change them.

## 8. Trust

Extensions are fully trusted code. What the design does about that:

- Installing is always an explicit act. Before it proceeds, the editor shows
  every distribution that will be added or changed, including transitive
  dependencies, and where each comes from.
- The lock file pins hashes, so `sync` installs exactly what was reviewed.
- Nothing upgrades on its own, and the editor does not contact any index
  unless the user runs an extensions command (X10).
- Code found inside a project directory is never loaded automatically.
  Project settings (section 8.1) are data, restricted to options that have
  opted in.

### 8.1 Project settings

A directory containing `.neomacs/settings.toml` is a project root. A buffer
visiting a file takes its project from the nearest ancestor directory that
has one; nested projects do not merge.

```toml
# .neomacs/settings.toml
[options]
tab_width = 4
indent_with_tabs = false
fill_column = 100

[mode.python.options]
fill_column = 88
```

- **Only options, only opted-in ones.** A setting is applied only if the
  option was declared with `project=True`. The core opts in options that are
  plain formatting choices: indentation, tab width, fill column, line
  endings, trailing-whitespace handling. An option whose value names a
  program to run, a path to load, or anything else that could cause code to
  execute must not opt in. Extensions make the same judgement for their own
  options, and `neomacs ext check` flags a `project=True` option whose type
  is a path or a command.
- **Everything else is ignored**, with one message per file naming the keys
  that were skipped. The file cannot bind keys, name commands, add hooks or
  load extensions.
- **Precedence.** A value set on the buffer wins, then the project file
  (its section for the buffer's mode, then its general section), then the
  mode's default, then the global value (ENGINE section 6).
- **Reloading.** The file is read when the first buffer of the project is
  opened, when the file is saved from the editor, and by
  `project.reload_settings`.
- `describe_option` shows when a value came from a project file and which.
- **Mode sections follow the mode's ancestry.** A buffer takes the general
  section, then the section of each named mode its own mode inherits from,
  then its own mode's, each over the one before. For a buffer in Python mode
  that is `[options]`, `[mode.fundamental.options]`, `[mode.python.options]`.
- **An option that is not declared when the file is read is skipped** and
  named in the message, like any other key that is not applied. When lazy
  loading arrives (X3) an extension's options may be declared later than
  that, and this will need another look.
- **The file is read with care**, since opening any file in the project
  reads it on the main loop: only a regular file is read, and only its first
  megabyte. A file that does not parse sets nothing and is reported.
- A settings file made after the project's files were opened is found by
  `project.reload_settings`. Saving a buffer under a new name moves it to
  the project its new place is in.

Running code from a project directory, behind an explicit trust step, is a
possible later design and is not part of this one.

## 9. Developing an extension

- **In place.** A `path` entry with `editable = true` puts a source
  directory in the environment. Edit a file, evaluate the buffer, and the
  running editor has the change; this is the ordinary live evaluation path.
- **Testing.** `neomacs.testing` provides a headless editor fixture and
  assertions on buffers, selections and displayed lines:

  ```python
  def test_commit_opens_message_buffer(editor):
      editor.call("git.commit")
      assert editor.frame.view.buffer.mode.name == "git-commit"
  ```

  It is the same harness the core's own tests use (ENGINE 14), exposed as a
  pytest plugin.
- **Checking.** `neomacs ext check` reports use of names that are not part of
  the stable API (STABILITY.md), a missing or too-loose requirement on the
  engine, autoload entries that do not match what the module registers, and
  global key bindings made on import.
- **Scaffolding.** `neomacs ext new NAME` writes a minimal package.

## 10. Requirements this places on the engine

- The ownership record (X5) on every registry, and an API to query and
  remove by owner.
- Pending values for undeclared options.
- Command and mode stubs.
- `sys.path` set up with the extensions environment after the engine's own
  packages, before any extension is imported.
- A check, after each import, of whether the GIL is enabled.

## 11. Implementation stages

**X0: init file and local code** (with engine stage 7). `init.py`,
single-file extensions, `--safe`, project settings (section 8.1). *Runnable:* configuration that survives a
restart.

**X1: ownership.** Disable, and reload with removal of stale registrations.
The ownership record itself is built with the registries in engine stage 3,
because live evaluation in stage 4 already needs it. *Runnable:* disable and re-enable a single-file
extension with no trace left in between.

**X2: packages.** Entry points, `extensions.toml`, the lock file, the
extensions environment, `uv` integration, the editor commands and `neomacs ext`.
*Runnable:* add two extensions with a shared dependency; add a third that
conflicts and get a clear refusal.

**X3: lazy loading.** The autoload index and stubs, pending options.
*Runnable:* fifty installed extensions add no measurable startup time.

**X4: developer tools.** `neomacs.testing`, `neomacs ext check`, `neomacs ext new`.

## 12. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| Two extensions the user wants cannot share an environment | One of them cannot be installed | Reported at install time with both requirements named. No design removes this short of separate processes. |
| An extension's dependency shadows or breaks the standard library or the engine | Editor fails to start | Engine packages are first on the path and pinned as constraints; `--safe` always starts. |
| Reload leaves stale state | Behaviour differs from a fresh start | Ownership diffing removes stale registrations; `requires_restart` for extensions that know better; `extensions.list` offers a restart. |
| The ecosystem lacks free-threaded wheels for some time | GIL re-enabled for many users | Tolerated and reported (4.3). |
| `uv` changes behaviour or its lock format | Installs break | The lock file `neomacs` writes is its own; `uv` is pinned in the distribution. |

## 13. Open questions

None at present.

The name `neomacs` should be registered on PyPI before anything else is
published, since it is only known to have been free on 2026-11-27. Other
projects already use the name Neomacs for Emacs-related software; whether
that matters for this one has not been looked into.
