"""The extensions an editor knows about: finding them, loading them when
they are first needed, and disabling and reloading them in place
(EXTENSIONS 5, 6).
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import site
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from neomacs import commands as commands_module
from neomacs import modes as modes_module
from neomacs import paths
from neomacs.errors import NeomacsError
from neomacs.ext.wheel import normalize
from neomacs.keymap import global_map
from neomacs.ownership import ownership
from neomacs.stability import provisional

if TYPE_CHECKING:
    from neomacs.editor import Editor

GROUP = "neomacs.extensions"
DECLARATIONS = "neomacs_extension.toml"
INDEX_FORMAT = 1
USER_PACKAGE = "neomacs_user"


@provisional("0.1")
@dataclass
class Extension:
    """One extension, as the editor knows it."""

    name: str  # its entry point's name, or a single file's name
    module: str  # what is imported to activate it
    kind: str = "package"  # "package", or "file" for a single-file extension
    distribution: str = ""
    version: str = ""
    summary: str = ""
    file: str = ""  # a single-file extension's path
    # Declared in the package, so that it need not be imported at startup:
    # (command name, one line about it), and modes with their file patterns.
    commands: list[list[str]] = field(default_factory=list[list[str]])
    modes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    requires_restart: bool = False
    needs: list[str] = field(default_factory=list[str])  # distributions it depends on
    # "known" (declared, not loaded), "loaded", "disabled", "failed", or
    # "restart" (changed on disk in a way a running editor cannot take up).
    state: str = "known"
    error: str = ""
    gil: bool = False  # importing it turned the GIL back on

    @property
    def lazy(self) -> bool:
        return bool(self.commands or self.modes)

    @property
    def stub_owner(self) -> str:
        return f"autoload:{self.name}"


def _gil_enabled() -> bool:
    check = getattr(sys, "_is_gil_enabled", None)
    return bool(check()) if check is not None else True


# -- the index --------------------------------------------------------------------------


def _declarations(module: str) -> dict[str, Any]:
    """What the package of MODULE declares about itself, read without importing it."""
    top = module.split(".", 1)[0]
    try:
        spec = importlib.util.find_spec(top)
    except ImportError, ValueError:
        return {}
    for location in (spec.submodule_search_locations or []) if spec is not None else []:
        file = Path(location) / DECLARATIONS
        if file.is_file():
            try:
                return tomllib.loads(file.read_text(encoding="utf-8"))
            except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
                return {"_error": f"{file} could not be read"}
    return {}


def _declared(extension: Extension, declared: dict[str, Any]) -> None:
    if "_error" in declared:
        extension.error = str(declared["_error"])
        return
    extension.requires_restart = bool(declared.get("requires_restart", False))
    autoload = declared.get("autoload", {})
    if not isinstance(autoload, dict):
        return
    table = cast(dict[str, Any], autoload)
    for entry in cast(list[Any], table.get("commands", [])):
        if isinstance(entry, str):
            extension.commands.append([entry, ""])
        elif isinstance(entry, dict) and "name" in entry:
            item = cast(dict[str, Any], entry)
            extension.commands.append([str(item["name"]), str(item.get("doc", ""))])
    for entry in cast(list[Any], table.get("modes", [])):
        if isinstance(entry, dict) and "name" in entry:
            item = cast(dict[str, Any], entry)
            patterns = [str(p) for p in cast(list[Any], item.get("patterns", []))]
            extension.modes.append({"name": str(item["name"]), "patterns": patterns})


def _requirement_name(requirement: str) -> str:
    name = ""
    for ch in requirement.strip():
        if not (ch.isalnum() or ch in "-_."):
            break
        name += ch
    return normalize(name)


def scan() -> list[Extension]:
    """Every installed extension, found by its entry point, with one that
    another depends on before it.  This reads the metadata of everything
    installed, so it is done when the environment changes and not at startup."""
    importlib.invalidate_caches()
    found: dict[str, Extension] = {}
    for entry in importlib.metadata.entry_points(group=GROUP):
        dist = entry.dist
        extension = Extension(entry.name, entry.value.split(":", 1)[0].strip())
        if dist is not None:
            extension.distribution = str(dist.metadata["Name"] or "")
            extension.version = dist.version
            extension.summary = str(dist.metadata["Summary"] or "")
            extension.needs = sorted({_requirement_name(r) for r in (dist.requires or [])})
        _declared(extension, _declarations(extension.module))
        found.setdefault(extension.name, extension)
    by_distribution = {normalize(e.distribution): e for e in found.values() if e.distribution}
    ordered: list[Extension] = []
    placed: set[str] = set()

    def place(extension: Extension, path: tuple[str, ...] = ()) -> None:
        if extension.name in placed or extension.name in path:
            return
        for need in extension.needs:
            other = by_distribution.get(need)
            if other is not None and other is not extension:
                place(other, (*path, extension.name))
        placed.add(extension.name)
        ordered.append(extension)

    for name in sorted(found):
        place(found[name])
    return ordered


def index_path() -> Path:
    return paths.cache_dir() / "autoload.json"


def _stamp(site_packages: Path | None) -> list[list[Any]]:
    """Something that changes when what is installed changes: when each
    directory that distributions are installed into was last added to or
    taken from.  The engine's own are included, for an extension installed
    beside the engine while it is being developed."""
    directories = [str(site_packages)] if site_packages is not None else []
    with contextlib.suppress(Exception):
        directories += site.getsitepackages()
    stamp: list[list[Any]] = []
    for directory in directories:
        try:
            stamp.append([directory, Path(directory).stat().st_mtime_ns])
        except OSError:
            stamp.append([directory, 0])
    return stamp


def write_index(extensions: list[Extension], site_packages: Path | None) -> None:
    record = {
        "format": INDEX_FORMAT,
        "stamp": _stamp(site_packages),
        "python": sys.version,
        "extensions": [
            asdict(e) | {"state": "known", "error": e.error, "gil": False} for e in extensions
        ],
    }
    path = index_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        tmp.replace(path)


def read_index(site_packages: Path | None) -> list[Extension] | None:
    """The index, if there is one and it describes what is installed now."""
    try:
        record: Any = json.loads(index_path().read_text(encoding="utf-8"))
        if record.get("format") != INDEX_FORMAT or record.get("python") != sys.version:
            return None
        if record.get("stamp") != _stamp(site_packages):
            return None
        return [Extension(**entry) for entry in record["extensions"]]
    except OSError, ValueError, TypeError, KeyError, AttributeError:
        return None


# -- one editor's extensions ---------------------------------------------------------------


@provisional("0.1")
class Extensions:
    """`ed.extensions`: the extensions this editor knows, by name."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self.known: dict[str, Extension] = {}
        self._site: Path | None = None
        self._warned_keys: set[str] = set()

    def __iter__(self):  # noqa: ANN204
        return iter(list(self.known.values()))

    @provisional("0.1")
    def get(self, name: str) -> Extension:
        found = self.known.get(name)
        if found is None:
            raise NeomacsError(f"There is no extension called {name}")
        return found

    # -- startup -----------------------------------------------------------------

    def use_environment(self, site_packages: Path | None) -> None:
        """Put the extensions environment on the path, after the engine's own
        packages, which it therefore cannot shadow (EXTENSIONS 4)."""
        if site_packages is None or self._site == site_packages:
            return
        self._site = site_packages
        if str(site_packages) not in sys.path:
            # `addsitedir` appends, and reads the directory's .pth files, which
            # is how an editable install says where its source is.
            site.addsitedir(str(site_packages))
        importlib.invalidate_caches()

    def start(self, site_packages: Path | None) -> None:
        """Called once when the engine starts: make every installed extension
        available, importing only those that cannot be loaded lazily."""
        self.use_environment(site_packages)
        entries = read_index(site_packages)
        if entries is None:
            entries = scan()
            write_index(entries, site_packages)
        for extension in entries:
            self.known[extension.name] = extension
            self.make_available(extension)

    def rescan(self) -> list[Extension]:
        """Look again at what is installed, and write the index."""
        entries = scan()
        write_index(entries, self._site)
        return entries

    # -- loading ------------------------------------------------------------------

    def make_available(self, extension: Extension) -> None:
        """Register stand-ins for what EXTENSION declares, or import it if it declares nothing."""
        if extension.lazy:
            self._stubs(extension)
            extension.state = "known"
        else:
            self.load(extension)

    def _stubs(self, extension: Extension) -> None:
        owner = extension.stub_owner
        for name, doc in extension.commands:
            self._command_stub(extension, name, doc, owner)
        for mode in extension.modes:
            name = str(mode["name"])
            modes_module.autoload[name] = (
                list(mode.get("patterns", [])),
                lambda e=extension: self._demand(e),
            )
            ownership.record(
                owner, "mode-stub", name, lambda n=name: modes_module.autoload.pop(n, None)
            )

    def _command_stub(self, extension: Extension, name: str, doc: str, owner: str) -> None:
        async def stand_in() -> None:
            self._demand(extension)
            real = commands_module.commands.get(name)
            if real is None or real.fn is stand_in:
                raise NeomacsError(
                    f"The extension {extension.name} says it has a command {name}, "
                    "and loading it did not define one"
                )
            await self.editor.call(name)

        stand_in.__name__ = stand_in.__qualname__ = name
        stand_in.__doc__ = (
            doc or f"From the extension {extension.name}, which is loaded on first use."
        )
        entry = commands_module.Command(name, stand_in, stand_in.__doc__, [], owner=owner)
        commands_module.commands[name] = entry

        def remove() -> None:
            if commands_module.commands.get(name) is entry:
                del commands_module.commands[name]

        ownership.record(owner, "command", name, remove)

    def _demand(self, extension: Extension) -> None:
        """Something EXTENSION declared is wanted: load it now."""
        if extension.state == "known" and not self.load(extension):
            raise NeomacsError(
                f"The extension {extension.name} could not be loaded: {extension.error}"
            )

    @provisional("0.1")
    def load(self, extension: Extension) -> bool:
        """Import EXTENSION, which activates it.  Returns whether that worked;
        a failure is reported and remembered, not raised."""
        editor = self.editor
        ownership.release(extension.stub_owner)
        gil_before = _gil_enabled()
        try:
            if extension.kind == "file":
                raise NeomacsError("a single-file extension is loaded by evaluating it")
            module = importlib.import_module(extension.module)
            activate = getattr(module, "activate", None)
            if callable(activate):
                activate()
        except Exception as exc:
            extension.state, extension.error = "failed", f"{type(exc).__name__}: {exc}"
            editor.report_exception(f"loading the extension {extension.name}", exc, keep=True)
            editor.startup_problems.append(
                f"extension {extension.name} failed to load: {extension.error}"
            )
            return False
        extension.state, extension.error = "loaded", ""
        self.after_load(extension, gil_before)
        return True

    def after_load(self, extension: Extension, gil_before: bool) -> None:
        """Note what loading EXTENSION did that the user should know about."""
        editor = self.editor
        if not gil_before and _gil_enabled():
            extension.gil = True
            editor.message(
                f"Loading the extension {extension.name} turned the GIL back on: one of its "
                "compiled modules is not built for free threading. Everything still works; "
                "worker threads no longer run in parallel.",
                kind="warning",
            )
        # Global keys are the user's to assign (EXTENSIONS X9): allowed, and said.
        keys: list[str] = []
        for module in self.modules_of(extension):
            for kind, key in ownership.owned(module):
                if kind == "key" and key[0] == id(global_map):
                    keys.append(" ".join(key[1]))
        if keys and extension.name not in self._warned_keys:
            self._warned_keys.add(extension.name)
            editor.message(
                f"The extension {extension.name} bound keys in the global map: "
                f"{', '.join(sorted(keys))}",
                kind="log",
            )

    def modules_of(self, extension: Extension) -> list[str]:
        """The loaded modules that are EXTENSION's, in the order they were imported."""
        if extension.kind == "file":
            return [extension.module] if extension.module in sys.modules else []
        top = extension.module.split(".", 1)[0]
        return [name for name in list(sys.modules) if name == top or name.startswith(top + ".")]

    # -- disabling and reloading (EXTENSIONS 5) -----------------------------------------------

    @provisional("0.1")
    def disable(self, name: str) -> Extension:
        """Take everything the extension registered out of the editor.  Values
        the user set for its options are kept, in case it comes back."""
        extension = self.get(name)
        editor = self.editor
        modules = self.modules_of(extension)
        owners = set(modules)
        main = sys.modules.get(extension.module)
        deactivate = getattr(main, "deactivate", None)
        if callable(deactivate):
            editor.guard(f"deactivating the extension {name}", deactivate)
        for info in list(editor.tasks):
            if info.owner in owners:
                info.cancel()
        for process in list(editor.processes):
            if process.owner in owners:
                process.kill()
        # Buffers in one of its modes go back to the fundamental mode.
        for buffer in editor.buffers:
            for minor in list(buffer.minor_modes):
                if type(minor).__module__ in owners:
                    buffer.disable_minor(type(minor))
            if buffer.mode is not None and type(buffer.mode).__module__ in owners:
                buffer.set_mode("fundamental")
        for mode in list(editor.global_modes):
            if type(mode).__module__ in owners:
                editor.global_modes.remove(mode)
        for owner in (*modules, extension.stub_owner):
            ownership.release(owner)
        for module in modules:
            sys.modules.pop(module, None)
            parent, _, child = module.rpartition(".")
            if parent in sys.modules:
                with contextlib.suppress(AttributeError):
                    delattr(sys.modules[parent], child)
        importlib.invalidate_caches()
        extension.state, extension.error = "disabled", ""
        editor.update_frames()
        return extension

    @provisional("0.1")
    async def enable(self, name: str) -> Extension:
        """Load an extension that was disabled, or that failed to load."""
        extension = self.get(name)
        if extension.state == "loaded":
            return extension
        if extension.kind == "file":
            await self.load_file(extension)
        else:
            self.load(extension)
        self.editor.update_frames()
        return extension

    async def load_file(self, extension: Extension) -> bool:
        """Evaluate a single-file extension (EXTENSIONS 3.2)."""
        from neomacs import startup

        gil_before = _gil_enabled()
        ok = await startup.load_module(
            self.editor, Path(extension.file), extension.module, f"extension {extension.name}"
        )
        extension.state = "loaded" if ok else "failed"
        extension.error = "" if ok else "see *messages*"
        if ok:
            self.after_load(extension, gil_before)
        return ok

    @provisional("0.1")
    async def reload(self, name: str) -> Extension:
        """Evaluate the extension's modules again, in place.  What a module
        registered before and does not register now is removed (ENGINE 9).

        An extension that says it does not survive this, or that has a
        compiled module, is marked as needing a restart instead.
        """
        from neomacs import startup

        extension = self.get(name)
        if extension.state in ("known", "disabled"):
            return extension  # nothing of it is running
        if extension.kind == "file":
            await self.load_file(extension)
            return extension
        if extension.requires_restart:
            extension.state = "restart"
            extension.error = "it says it does not survive being reloaded"
            return extension
        gil_before = _gil_enabled()
        for module_name in self.modules_of(extension):
            module = sys.modules.get(module_name)
            file = getattr(module, "__file__", None)
            if file is None:
                continue  # a namespace package: nothing to evaluate
            if not str(file).endswith(".py"):
                extension.state = "restart"
                extension.error = f"{module_name} is compiled and cannot be reloaded"
                return extension
            ok = await startup.load_module(
                self.editor, Path(file), module_name, f"extension {name} ({module_name})"
            )
            if not ok:
                extension.state, extension.error = "failed", "see *messages*"
                return extension
        extension.state, extension.error = "loaded", ""
        self.after_load(extension, gil_before)
        self.editor.update_frames()
        return extension

    # -- after the environment has changed -----------------------------------------------------

    async def refresh(self, site_packages: Path | None) -> list[str]:
        """The environment was installed into: take up what is new, reload
        what changed, and drop what has gone.  Returns what was done, in words."""
        self.use_environment(site_packages)
        done: list[str] = []
        before = {e.name: e for e in self.known.values() if e.kind == "package"}
        now = {e.name: e for e in self.rescan()}
        for name, old in before.items():
            if name not in now:
                if old.state not in ("disabled",):
                    self.disable(name)
                del self.known[name]
                done.append(f"removed {name}")
        for name, new in now.items():
            old = before.get(name)
            if old is None:
                self.known[name] = new
                self.make_available(new)
                done.append(f"added {name} {new.version}")
                continue
            if (
                old.version == new.version
                and old.commands == new.commands
                and old.modes == new.modes
            ):
                continue
            new.state, new.gil = old.state, old.gil
            self.known[name] = new
            if old.state == "known":
                ownership.release(old.stub_owner)
                self.make_available(new)
                done.append(f"updated {name} to {new.version}")
            elif old.state == "loaded":
                await self.reload(name)
                if new.state == "restart":
                    done.append(f"{name} {new.version} is installed; restart to finish upgrading")
                else:
                    done.append(f"reloaded {name} at {new.version}")
        self.editor.update_frames()
        return done
