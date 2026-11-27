# Extensions and packaging design

Status: draft 5, 2026-11-27. Stages X0 to X3 are implemented; section 11.1
says how what was built differs from what is described before it. X4 is
deferred. The decisions in section 2 are confirmed.

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

[project]
description = "Git status, staging and commit"

[project.entry-points."neomacs.extensions"]
git = "neomacs_git"
```

```toml
# neomacs_git/neomacs_extension.toml, shipped inside the package
requires_restart = false

[autoload]
commands = [
    { name = "git.status", doc = "Show what has changed." },
    "git.commit",
    "git.blame",
]
modes = [{ name = "git-commit", patterns = ["COMMIT_EDITMSG"] }]
```

- The entry point names the module to import. Importing it is activating it:
  its `@command`, `Option`, `Hook.add` and mode definitions run and register.
- The module may define `activate()` and `deactivate()` for anything the
  decorators do not cover.
- What an extension declares about itself is in `neomacs_extension.toml`,
  in its top-level package. It is optional. Its `[autoload]` table drives
  lazy loading (section 6), and `requires_restart` is described in section 5.
  It is not a `[tool.neomacs]` table in `pyproject.toml`, as earlier drafts
  had it, because nothing in `pyproject.toml` but the standard metadata
  reaches an installed distribution: a wheel does not carry the file.
- The one line shown about an extension is its distribution's summary
  (`description` in `pyproject.toml`).
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
- An extension may set `requires_restart = true` in its
  `neomacs_extension.toml` if it knows it does not survive reloading.
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
Deferred with step 14 of IMPLEMENTATION.md.

### 11.1 As built: X1 to X3

**The environment (section 4).**

- It is a virtual environment at `ext-env/venv` in the data directory, made
  by `uv venv` for the engine's own interpreter. Its `site-packages` is
  added to `sys.path` with `site.addsitedir`, which appends, so the engine's
  packages and the standard library come first, and which reads `.pth`
  files, so an editable install finds its source.
- **The engine as a constraint.** The resolver is given a distribution
  called `neomacs`, at the engine's version and with no files, pinned and
  pointed at a wheel written for the purpose. An extension that requires
  `neomacs>=99` then fails to resolve, naming that requirement, and nothing
  is fetched to stand for the engine. The engine has no dependencies of its
  own; when it has, each gets the same treatment.
- **Resolving** is `uv lock` on a generated project whose dependencies are
  the extensions, restricted to this machine's platform and interpreter. The
  project and uv's own lock stay in `ext-env/resolve`; uv's lock is what
  keeps versions where they are from one resolution to the next.
- **`extensions.lock`** is written from uv's answer in a format of its own:
  for each distribution its name, version, where it came from, the hashes of
  its files, and whether there is a wheel this interpreter can use as it
  is. `sync` installs from it alone, with `uv pip sync`, which checks the
  hashes. Building the environment on another machine needs the two files
  and nothing else. A later `add` there has none of uv's memory to go on and
  may move other versions; that is shown before anything is installed.
- **Where distributions come from** can be said in `extensions.toml`:

  ```toml
  [resolver]
  index = "https://pypi.org/simple"
  extra-index = []
  find-links = ["~/wheels"]
  no-index = false
  offline = false
  ```

  With none of it, uv's own configuration and PyPI apply. The tests install
  from a directory of wheels with `no-index` and `offline`.
- **`uv`** is the one named by `NEOMACS_UV`, otherwise the one on the path;
  the desktop application will ship one (DISTRIBUTION 3.2). Version 0.5.3
  or newer is required; 0.8.19 is what this was built with. Its cache is
  `uv/` in the cache directory.
- **Adding** takes `NAME`, `NAME>=1.0`, `NAME @ git+URL[@REV]`,
  `NAME @ PATH`, or a directory with a `pyproject.toml` that names a project.
  `extensions.toml` is edited in place: the user's comments and other entries
  are left as they were.

**Before installing (4.3, 8).** Every operation that would change the
environment first shows what would be added, changed and removed, and which
distributions have no wheel a free-threaded interpreter can use as it is: a
wheel counts if its ABI tag is `none` or ends in `t`. Those are built from
source. In the editor the list is shown in `*extensions*` and a yes is asked
for; `neomacs ext` asks on a terminal and needs `--yes` otherwise. A refusal
by the resolver is given in the resolver's own words, which name both
requirements and what asks for each.

**Commands (4.2)**, beyond the table there: `extensions.disable`,
`extensions.enable`, `extensions.reload`. Installing runs as a task, listed
in `ed.tasks` with "resolving" and then "installing" as its progress. When
it finishes the running editor takes up the result: new extensions are made
available, upgraded ones that are loaded are reloaded, removed ones are
disabled before their files go. `neomacs ext` changes the files and the
environment only; an editor that is running sees it when it next starts.

**Lazy loading (6.2).**

- The index is `autoload.json` in the cache directory. It lists every
  extension in the order to load them, with what each declares. It is
  written whenever the environment is installed into, and at startup if it
  is missing or if a directory that distributions are installed into has
  changed since it was written, which costs one `stat` each to find out.
  Otherwise startup reads that one file.
- An extension with an `[autoload]` table gets a stand-in for each command,
  carrying the line about it from the index, and its modes' file patterns
  are noted. Running a stand-in, visiting a matching file, or asking for the
  mode by name imports the extension; its real commands replace the
  stand-ins, and one it declared and does not define is an error that says
  so. An extension with no table is imported at startup.
- Extensions installed beside the engine itself, as when one is being
  developed, are found the same way, by their entry point.
- Measured: fifty extensions, each taking 50 ms to import, add less than
  the variation between runs to startup, against 2.5 s if they were imported.

**Disabling and reloading (5).**

- Disabling calls `deactivate()`, cancels the extension's tasks and kills
  its processes, turns its modes off in every buffer, releases everything
  its modules registered, and takes the modules out of `sys.modules`.
  Enabling imports it afresh. It applies to single-file extensions too. It
  lasts until the editor is restarted: which extensions are disabled is not
  recorded on disk.
- Reloading evaluates each of the extension's modules again, in the order
  they were first imported, through the live evaluation pipeline.
- **After each import** the engine checks whether the GIL is on and was not
  before; the extension whose import did it is recorded, the user is told,
  and `extensions.list` shows it.
- **Global keys** bound while an extension loads are listed in one message
  in `*messages*` (X9).

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
