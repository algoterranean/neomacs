# API stability design

Status: draft 3, 2026-11-27. Nothing is implemented. The decisions in
section 2 are confirmed; section 11 lists what is still open.

This describes what `neomacs` promises not to break, how that is recorded and
checked, and how things change when they must. It assumes ENGINE.md,
FRONTEND.md and EXTENSIONS.md, and it settles the "protocol compatibility
rules" item deferred in ENGINE.md section 16.

## 1. The problem

ENGINE.md says there is no private core: the engine is written in the same
Python, against the same objects, that users and extensions use, and any of
it can be read and redefined in the running editor. That is a property of
access. It cannot also be a promise of stability, or nothing inside the
engine could ever change.

So the design separates the two. Everything is reachable. A marked subset is
stable. Tools tell an extension author which side of the line they are on.

## 2. Decisions taken

| # | Decision | Reason |
|---|---|---|
| S1 | Three surfaces are covered: the Python API, the RPC protocol, and the user contract (names a user types into configuration). | They have different consumers and break in different ways. |
| S2 | Every Python name is in one of three tiers: stable, provisional or internal. The tier is marked in the code and collected into a generated manifest that is checked in. | A promise that is not written down in a checkable form is not kept. |
| S3 | Semantic versioning. A breaking change to anything stable needs a new major version. | Extensions declare a version range; the range has to mean something. |
| S4 | Before 1.0 nothing is stable and minor versions may break. 1.0 is cut when the engine's stages are complete and a first stable manifest is agreed. | The API will move while the engine is being built; pretending otherwise helps nobody. |
| S5 | A stable name is deprecated for at least two minor releases and six months before it is removed, and removed only in a major release. | Gives extension authors a real window. |
| S6 | CI compares the manifest against the last release and fails if the change is larger than the version bump allows. | Makes S3 mechanical. |
| S7 | The protocol has its own integer version. Engine and frontend must agree on it exactly; everything optional is negotiated as named capabilities at attach time. | Redraw events mutate the frontend's state, so an unknown event cannot safely be ignored. |
| S8 | Releases are made when ready, not on a calendar. | |
| S9 | No long-term support. Only the latest major version receives fixes. | One line of development to maintain. |
| S10 | Moving the bundled Python to a new minor version is a breaking change and happens only in a major release. | It stops extensions' compiled modules from loading. |

## 3. The Python API

### 3.1 Tiers

| Tier | Meaning | How it is marked |
|---|---|---|
| Stable | Covered by S3 and S5. | `@stable(since="1.0")` on functions and classes; listed in the module's `__all__`. |
| Provisional | Intended to become stable; may change in a minor release, with a changelog entry and, where possible, a deprecation. | `@provisional(since="1.3")` |
| Internal | No promise. May change in any release, including a patch. | Unmarked, or underscore-prefixed. |

The markers are no-ops at run time apart from recording metadata. They apply
to functions, classes, methods, and module-level constants (through a
registry call for the last).

A stable class does not make all its attributes stable. Methods and
attributes are marked individually, and unmarked ones on a stable class are
internal.

### 3.2 What is expected to be stable at 1.0

A starting list, to be settled before 1.0:

- The text model: `Buffer`, `Marker`, `Cursor`, `Selection`, `SelectionSet`,
  spans, the grapheme boundary functions.
- Registration: `@command` and the parameter annotations, `Option`, `Hook`,
  `Keymap`, `Mode`, `Syntax`, `keep`.
- The editor object: `ed.frame`, `ed.view`, `ed.window`, `ed.buffer`,
  `ed.show`, `ed.message`, `ed.minibuffer.read`, timers and tasks,
  `ed.run` and `ed.spawn`.
- `neomacs.testing`.

Expected to stay provisional for a while: panels and placement, the
`BufferSyntax` interface, decoration providers, the debugger's programmatic
interface, completion.

Expected to stay internal: the text store implementation, the view builder,
the dispatcher, the RPC layer, the live evaluation and patching internals.
An extension that needs one of these should ask for a stable entry point
rather than reach in.

### 3.3 What counts as a breaking change

Breaking, for a stable name:

- Removing or renaming it.
- Removing a parameter, adding a required one, reordering positional ones,
  or changing a default in a way that changes behaviour.
- Returning a different type, or raising a different exception for the same
  condition.
- Changing the arguments a stable hook passes to its functions.
- Changing what a documented behaviour does.

Not breaking:

- Adding a function, class, method, optional keyword parameter, hook or
  option.
- Fixing behaviour that contradicted the documentation.
- Changing anything provisional or internal.
- Performance changes.

**Interfaces that extensions implement** need extra care, because adding a
method breaks every implementer. `Completer`, decoration providers, `Mode`
and the like are therefore base classes with default implementations, not
bare protocols, and a new method always comes with a default.

### 3.4 The manifest

A build step walks the package and writes `api/manifest.json`: for every
stable and provisional name, its qualified name, kind, signature, tier and
`since` version. The file is committed.

- CI regenerates it and fails if it differs from the committed copy, so a
  change to the API is always visible in review.
- A second check compares it with the manifest of the last release and
  classifies the difference as patch, minor or major. The build fails if the
  version in the source tree is not bumped at least that far (S6).
- The reference documentation's API pages are generated from the same file.

### 3.5 Deprecation

```python
@stable(since="1.0")
@deprecated("Use Buffer.replace", since="1.4", removal="2.0")
def replace_region(...): ...
```

- The first use from each calling module produces one warning, written to
  `*messages*` and naming the extension responsible, which the ownership
  record (EXTENSIONS.md X5) identifies.
- `extensions.list` shows which installed extensions use deprecated names.
- The deprecation appears in the manifest, so tools can report it without
  running anything.
- Removal waits for the later of two minor releases, six months, and the
  next major version (S5).

## 4. The user contract

Things a user writes into `init.py`, or types, and expects to keep working:

| Thing | Rule |
|---|---|
| Command names | Stable once the command is. A rename leaves an alias under the old name for a full major version; key bindings and `engine.call` through the alias work and warn once. |
| Option names and value types | As commands. A renamed option migrates values set under the old name. |
| Hook names and their arguments | As the Python API. |
| Key notation | Stable. |
| Role names, face names, gutter column ids, the tree-sitter capture vocabulary | Stable once listed in the manifest; additions are minor changes. |
| Default key bindings, default option values, default placement | Not covered. Changes are listed in release notes under their own heading. |
| Command-line flags and `neomacs ext` subcommands | Covered by S3. |
| File formats: `extensions.toml`, `extensions.lock`, the autoload index | Each carries a format version. Old versions are read for at least one major version and rewritten on change. |

Commands, options and hooks are registered at run time, not found by walking
the package, so the manifest includes them by starting a headless engine and
dumping its registries.

## 5. The protocol

### 5.1 Version and capabilities

- `engine.api_info` returns `protocol`, an integer, and the list of
  capabilities the engine supports.
- `ui.attach` sends the frontend's `protocol` and the capabilities it
  supports. If the two integers differ, the engine refuses with an error
  that states both, and the frontend shows it. There is no attempt to
  interoperate across protocol versions.
- The engine sends only events and requests belonging to the base protocol
  or to a capability the frontend declared. A frontend never has to guess
  what an unknown event means, because it never receives one.

### 5.2 What may change without a new protocol version

- A new capability, with its events, requests and options.
- A new optional field in an existing event's map argument, such as a new
  key in `win_info` or in a face's attributes. Frontends must ignore map
  keys they do not know.
- A new notification from frontend to engine, if the engine tolerates its
  absence.

Everything else is a new protocol version: changing the arguments of an
event, changing what a splice means, changing units or indexing, removing
anything from the base protocol.

### 5.3 Unknown messages

- An unknown notification is ignored, and logged once.
- An unknown request gets a JSON-RPC "method not found" error, and the
  sender must have a fallback.
- An unknown redraw event is a protocol error: the frontend calls
  `ui.resync`. With 5.1 followed this cannot happen.

### 5.4 Relation to releases

The protocol version changes only in a major release of the engine. The
desktop app always contains a matching engine and frontend (DISTRIBUTION.md
P3), so the check matters for the terminal frontend, third-party frontends,
and scripts that connect to the socket.

`engine.eval` and `engine.call` are part of the base protocol and stable;
what the evaluated code can reach is governed by section 3.

## 6. Versions

- `MAJOR.MINOR.PATCH`, one number for the engine, the frontend and the app.
- **Patch:** fixes only. The manifest does not change.
- **Minor:** additions; changes to provisional names; new deprecations.
- **Major:** removals of deprecated names; breaking changes; protocol
  version changes; a new minor version of the bundled Python (S10).
- **Support:** fixes go to the latest major version only (S9). Releases
  happen when ready (S8), so the deprecation window in S5 is whichever of
  its conditions takes longest.
- **Before 1.0** (S4): `0.MINOR.PATCH`, where a minor release may break
  anything. The manifest and the checks exist from the first release anyway,
  with everything marked provisional, so the habit and the tooling are in
  place before they are binding.

## 7. How an extension declares compatibility

- A normal requirement on the engine's distribution, `>=1.4,<2`: the lowest
  minor version whose API it uses, up to the next major.
- The resolver enforces it at install time (EXTENSIONS.md section 4), and
  the first start after an engine update re-checks every installed extension
  (DISTRIBUTION.md 8.3).
- `neomacs ext check` reads the manifest and reports every name the extension
  uses that is provisional, internal or deprecated, and the minimum engine
  version its stable usage implies. Using internal names is allowed; it is
  reported so the author knows the requirement range is not protecting them.
- Feature detection at run time, `neomacs.has("capability")`, exists for
  optional parts such as tree-sitter, not as a substitute for a version
  range.

## 8. Live evaluation and stability

A user can redefine any function in the running editor, stable or not. That
is the point of the design and is not something to prevent. Three rules keep
it from undermining the rest:

- Redefining a name does not change its tier or the manifest. The promise is
  about what `neomacs` ships.
- An extension that redefines or wraps another package's function, rather
  than using a hook, is outside any guarantee, and `neomacs ext check` reports
  it.
- Bug reports are expected against `--safe`, which starts without extensions
  or the init file.

## 9. Process

- **Changelog.** Every release has sections for breaking changes,
  deprecations, additions, default changes, and fixes. The first three are
  generated from the manifest difference and edited by hand.
- **Promotion.** A provisional name becomes stable by a deliberate change of
  marker, after at least one minor release in use.
- **Review.** Any change to the manifest needs a reviewer who is checking
  the API, not only the code.
- **Extension canary.** Once there are published extensions, the release
  process runs the test suites of a chosen set against the release candidate
  and reports failures before publishing.

## 10. Implementation stages

**T0: markers and manifest** (with engine stage 1). The decorators, the
generator, the committed file, the "regenerate and compare" check. Everything
provisional. *Runnable:* changing a signature fails CI until the manifest is
updated.

**T1: protocol version** (with engine stage 2). `protocol` and capabilities
in `api_info` and `ui.attach`; refusal on mismatch; the terminal frontend and
test client declare capabilities.

**T2: deprecation machinery** (with engine stage 3). `@deprecated`, once-per-
module warnings, command and option aliases with value migration.

**T3: release checks.** The manifest difference classifier, the version bump
check, changelog generation, the registry dump for the user contract.

**T4: extension-facing tools** (with EXTENSIONS X4). `neomacs ext check`
reporting tiers and the implied minimum version.

**T5: 1.0.** Agree the stable list, flip the markers, enable S3 and S5.

## 11. Open questions

1. **The 1.0 stable list** in 3.2. To be settled as the last step before
   1.0.

The exact-match protocol check (S7) is confirmed for now. If third-party
frontends appear and find it painful, allowing a frontend to speak two
versions can be revisited.
