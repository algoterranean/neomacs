# Distribution design

Status: draft 4, 2026-11-27. Nothing is implemented. The decisions in
section 2 are confirmed. Items marked **to verify** are facts this design relies on
that have not been checked.

This describes how `neomacs` reaches a user's machine: what is shipped, how it
is built, signed and updated, and where it puts things. It assumes ENGINE.md,
FRONTEND.md and EXTENSIONS.md.

## 1. Goals and non-goals

Goals:

- A user installs one thing and has a working editor, with no Python or Node
  of their own.
- The engine alone, with the terminal frontend, is installable with standard
  Python tools.
- Builds are reproducible from lock files and produced entirely by CI.
- An update never changes code underneath a running editor.

Non-goals:

- Mobile, browser or server-hosted editions.
- Supporting Python older than 3.15, or builds of the engine against several
  Python versions at once.
- App store distribution, at least at first.

## 2. Decisions taken

| # | Decision | Reason |
|---|---|---|
| P1 | Two artefacts: a Python package containing the engine and terminal frontend, and a desktop application containing that package, a Python runtime, `uv` and the Electron frontend. | The engine is pure Python with no required compiled dependencies, so it is an ordinary wheel. The desktop app is that wheel plus everything needed to run it. |
| P2 | The desktop app bundles its own free-threaded CPython and runs the engine as a child process. | Users should not need a Python installation, and the free-threaded build is not what most systems have. |
| P3 | The app, the engine inside it and the frontend share one version number and are updated together. | One thing to reason about. The protocol version check (STABILITY.md) covers the cases where engine and frontend come from different installs. |
| P4 | macOS and Linux are supported from the first release. Windows is built and tested in CI from the start but released later. | Windows needs a different transport (section 6) and more platform work; testing it from the start keeps the door open. |
| P5 | Updates are downloaded in the background and applied on the next start. | A live image must not have its code replaced while it runs. |
| P6 | No telemetry. Crash information stays on the user's machine. | |
| P7 | Everything shipped is built in CI from tagged source and lock files, and signed there. | Reproducibility and a single place where signing keys are used. |
| P8 | One engine per app instance. The app starts its engine as a child process and the engine exits with it. All windows of that instance are frames of the one engine. | The engine's lifetime is the app's; there is no daemon to find, version-match or leave behind. |
| P9 | Default fonts are bundled: JetBrains Mono as the monospaced family and Inter as the proportional one. More may be added later. | Text and themes look the same on every machine. Both are actively maintained, cover Latin, Greek and Cyrillic, and are published under the SIL Open Font License. |
| P10 | The app is single-instance per user by default. A second launch, or `neomacs FILE`, opens in the running instance; `--new-instance` starts a separate app and engine. | Files opened from anywhere land in one place, and two instances never edit the same file unknowingly. |
| P11 | The update feed is GitHub Releases. Nightly builds are public. | Nothing to host; the standard Electron updater supports it. |

## 3. What is shipped

### 3.1 The Python package

- A pure-Python wheel (`py3-none-any`) that requires Python 3.15 or newer.
- Contents: the engine, the standard commands and modes, the terminal
  frontend, `neomacs.testing`.
- Required dependencies: none with compiled code. `wcwidth` for the terminal
  frontend. `tree-sitter` is an optional extra.
- Entry point: a `neomacs` command.
- It runs on a build with the GIL as well as a free-threaded one; on a GIL
  build worker threads do not run in parallel, and the engine says so once at
  startup.

Installing it with `uv tool install` or `pipx` gives a working terminal
editor. This is also what extension authors' CI installs.

The distribution name and the import name are both `neomacs` (EXTENSIONS.md
X11).

### 3.2 The desktop application

```
Neomacs.app  (or the equivalent directory on Linux and Windows)
├── Electron runtime and the frontend bundle
└── resources/
    ├── python/            a relocatable free-threaded CPython
    ├── site-packages/     the neomacs wheel and its dependencies, precompiled
    ├── bin/uv             pinned
    ├── fonts/             if bundled (section 9)
    └── licenses/          third-party notices
```

- **Python runtime.** A relocatable standalone build, as produced by the
  python-build-standalone project. **To verify:** that free-threaded 3.15
  builds are published for every target in section 4.
- **The engine is a child process**, started with the bundled interpreter.
  Python is not embedded in the Electron process.
- **Bytecode** is compiled at build time with unchecked hash-based `.pyc`
  files, so nothing is written into the application directory at run time
  and startup does not stat sources.

## 4. Platforms

| Platform | Architectures | Packages | Status |
|---|---|---|---|
| macOS 13 or later | arm64, x86-64 | Signed and notarised `.dmg`; Homebrew cask | First release |
| Linux, glibc 2.31 or later | x86-64, arm64 | AppImage; `.deb`; `.rpm` | First release |
| Windows 10 or later | x86-64 | Signed installer; `winget` | Later; CI from the start |

These minimum versions are the intended floor. They are to be checked
against what the chosen Electron release and Python runtime actually
support when the first build is packaged, and raised if either needs it.

## 5. Where things live

| Purpose | Linux | macOS | Windows |
|---|---|---|---|
| Configuration: `init.py`, `extensions.toml`, `extensions.lock`, single-file extensions | `$XDG_CONFIG_HOME/neomacs` | `~/.config/neomacs` | `%APPDATA%\neomacs` |
| Data: extensions environment, recovery journal | `$XDG_DATA_HOME/neomacs` | `~/Library/Application Support/neomacs` | `%LOCALAPPDATA%\neomacs` |
| Cache: autoload index, `uv` cache | `$XDG_CACHE_HOME/neomacs` | `~/Library/Caches/neomacs` | `%LOCALAPPDATA%\neomacs\cache` |
| Runtime: one engine socket per app instance | `$XDG_RUNTIME_DIR/neomacs/<instance>.sock` | the same, in a short per-user directory under the system temporary directory | a named pipe per instance |

- Configuration on macOS is under `~/.config` rather than `Library`, because
  it is meant to be edited and kept in version control.
- **Socket path length.** Unix socket paths are limited to about 104 bytes on
  macOS and 108 on Linux. The runtime directory is chosen to be short, and if
  the resulting path is still too long the engine falls back to
  `/tmp/neomacs-<uid>/`, created with mode 0700.
- The application directory is never written to after installation.

## 6. Transport per platform

| | Transport | Access control |
|---|---|---|
| Linux, macOS | Unix domain socket | Directory mode 0700, socket mode 0600 (ENGINE 4.1) |
| Windows | Named pipe | Pipe security descriptor limited to the current user |

Node's `net` module speaks both. On the Python side this is confined to
`rpc/transport.py`. **To verify:** whether Python's Unix socket support on
Windows is complete enough under asyncio to use instead; if it is, one
transport everywhere would be simpler.

Other Windows work known in advance: the fallback interrupt (ENGINE section
3) behaves differently there and must be tested; subprocess and
pseudo-terminal handling differ; file replacement on save must cope with
files held open by other programs.

## 7. Launching

There is one engine per app instance (P8).

- **The app.** Starting the app starts an engine as its child, with a fresh
  socket in the runtime directory, and opens a window attached to it. Further
  windows are further frames of the same engine. When the app quits, it asks
  the engine to exit; if the app dies, the engine notices its parent has gone
  and exits too.
- **A second launch.** The app is single-instance per user (P10):
  launching it again, or opening a file from the system's file manager,
  opens a window or a file in the running instance. `--new-instance` starts
  a separate app with its own engine, sharing nothing in memory with the
  first.
- **The command.** The desktop app can install a `neomacs` command on the
  user's `PATH`, on request. `neomacs FILE…` finds the running instance
  through its socket in the runtime directory, the most recently focused one
  if there are several, and asks it to visit the files; if there is none it
  starts the app. `neomacs ext …` manages extensions (EXTENSIONS.md 4.2).
- **The terminal frontend.** `neomacs --tui` starts its own engine, as a
  child of the terminal program, with the same lifetime rule.
  `neomacs --tui --attach` attaches to a running instance as another frame.
- **File associations and "open with"** are registered by the installers and
  routed the same way as the command.

What happens to unsaved buffers when an instance exits remains part of the
engine lifecycle work deferred in ENGINE.md.

## 8. Signing and updates

### 8.1 macOS

- Signed with a Developer ID certificate, hardened runtime enabled, and
  notarised.
- Every Mach-O file in the bundle is signed, including the Python
  executable, its shared libraries, any compiled modules, and `uv`.
- **Entitlements.** Electron needs the JIT entitlement. The bundled Python
  executable needs library validation disabled, because extensions bring
  compiled modules signed by other parties or not at all, and the hardened
  runtime would otherwise refuse to load them. That entitlement is granted to
  the Python executable only, not to the Electron process.

### 8.2 Windows and Linux

- Windows binaries and the installer are Authenticode-signed.
- Linux packages are accompanied by checksums and a detached signature. The
  `.deb` and `.rpm` repositories are signed in the usual way if and when they
  are hosted.

### 8.3 Updates

- The app checks the project's GitHub Releases (P11), downloads in the background, verifies the
  signature, and tells the user an update is ready. It is applied when the
  app next starts (P5). Nothing is replaced under a running engine.
- A user can turn the check off. Package-manager installs (Homebrew, `apt`,
  `winget`) leave updating to the package manager and disable the built-in
  check.
- **Channels:** stable, and nightly builds from the main branch. Nightlies
  are public, published as pre-releases, and a user opts in to the channel.
- **Extensions are not touched** by an app update. If the new engine version
  falls outside an installed extension's declared range, the first start
  after updating reports it and offers to upgrade or disable that extension.
- **The bundled Python** moves to a new minor version only in a major
  release of `neomacs` (STABILITY.md S10), because that breaks extensions'
  compiled modules. On the first start after such a release the extensions
  environment is rebuilt from the lock file.

## 9. Fonts and third-party material

- **Fonts.** JetBrains Mono and Inter are bundled and used as the default
  monospaced and proportional faces (P9), so that documents and themes look
  the same on every machine. Both are distributed under the SIL Open Font
  License, which permits bundling; **to verify** against each project's
  licence file at the version bundled, and include that file in
  `resources/licenses`. The variable-font files are used, one per family and
  style, to keep the size down. Users choose other fonts through face
  settings, and more families may be bundled later.
- **Licences.** `neomacs` is MIT. The app also contains Electron and Chromium,
  CPython, `uv` and fonts, each under its own licence; their notices are
  collected at build time into `resources/licenses` and shown by a command.
- Nothing from Emacs is included (ENGINE D15).

## 10. Build pipeline

One CI workflow, triggered by a version tag, with a job per target. Its
scripts live in `packaging/` at the top of the repository.

1. Build the wheel from the tag.
2. Run the engine's test suite on the target's Python, free-threaded.
3. Fetch the pinned Python runtime and `uv` for the target and verify their
   hashes.
4. Install the wheel and its locked dependencies into `site-packages`;
   compile bytecode; remove tests and caches.
5. Build the frontend bundle from its lock file.
6. Package with the Electron packager for the target.
7. Sign, notarise where applicable, and verify the signature.
8. Start the packaged app headlessly, attach, run a smoke script, quit.
9. Generate checksums, a software bill of materials and the licence bundle.
10. Publish artefacts and update the release feed. Publish the wheel to PyPI.

Signing keys exist only as CI secrets and are used only in step 7. Nightly
builds run the same workflow from the main branch with a nightly signing
identity.

## 11. Startup and size

Targets, to be checked in CI against the packaged app on a reference machine:

| Measure | Target |
|---|---|
| Engine process start to socket listening, no extensions | 200 ms |
| App launch to first painted window | 700 ms |
| `neomacs FILE` with the engine already running, to the file being visible | 150 ms |

Means: precompiled bytecode; `lazy import` (Python 3.15) for everything not
needed to open the first buffer; lazy extension loading (EXTENSIONS.md 6.2);
`python -X importtime` tracked in CI so a slow import shows up as a
regression.

Download and installed size are to be measured from the first packaged
build. The Electron runtime dominates; the Python runtime and engine are
small beside it.

## 12. Diagnostics

- The engine writes a log to the data directory, rotated by size.
- If the engine process dies, the app shows the tail of that log and offers
  to restart it. Unsaved work is recovered from the journal (ENGINE stage 7).
- A renderer crash reloads the page and resynchronises (FRONTEND.md 3).
- Electron's crash reporter is not configured to upload anything.
- `neomacs --version` prints the versions of the engine, protocol, Python,
  Electron, and whether the GIL is enabled.

## 13. Implementation stages

**P0: the wheel** (with engine stage 0). Package metadata, the `neomacs` command,
CI that tests on free-threaded 3.15 on Linux and macOS, and on Windows
without release. *Runnable:* install from a built wheel into a clean
environment and start the engine.

**P1: unsigned app.** Bundle the Python runtime and engine with the Electron
shell (FRONTEND F0); directories per section 5; packaged smoke test.
*Runnable:* a downloadable build that starts on a machine with no Python.

**P2: signing.** macOS signing, entitlements and notarisation; Linux
signatures. *Runnable:* installs without security warnings on a clean macOS
machine.

**P3: command and associations.** The `neomacs` command installer, file
associations, extension-environment handling across updates.

**P4: updates and channels.** Release feed, background download, apply on
restart, nightly channel, package-manager recipes.

**P5: Windows release.** Named pipe transport, installer, signing.

## 14. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| No standalone free-threaded 3.15 build for some target | That target cannot be bundled | Verify before P1; fall back to building CPython in CI for the missing target. |
| Notarisation rejects the bundle because of an unsigned or oddly linked file inside the Python runtime | No macOS release | Sign every Mach-O file found by scanning, not by list; a CI step that runs Apple's checks before submission. |
| The library-validation entitlement is refused, or its scope must widen | Extensions with compiled modules fail to import on macOS | Tested in P2 with a real compiled extension, before the extension system depends on it. |
| A major release changes the Python minor version | Compiled extension modules no longer load | Only done in a major release (STABILITY.md S10); the extensions environment is rebuilt from the lock on that start (8.3); stated in release notes. |
| Electron security updates arrive often | A stale bundled Chromium | Nightly builds track Electron; stable releases follow its supported lines. |
| Socket path too long in an unusual environment | Engine cannot listen | Length check with the `/tmp` fallback (section 5). |

## 15. Open questions

None at present. The items marked **to verify** in sections 3, 6 and 9, and
the minimum OS versions in section 4, are facts to check at packaging time
and not decisions.
