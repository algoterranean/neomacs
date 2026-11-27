"""The extensions environment (EXTENSIONS 4): resolving what the user asked
for, and making the environment on disk match the lock file.

Resolution and installation are done by `uv` (X8).  It is given a made-up
project whose dependencies are the extensions, with a distribution standing
for each of the engine's own packages pinned to the engine's version, so that
an extension needing another version of one of them fails to resolve.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import sys
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

import neomacs
from neomacs import paths
from neomacs.errors import NeomacsError
from neomacs.ext import lock as lock_module
from neomacs.ext import manifest as manifest_module
from neomacs.ext.lock import Lock, Plan
from neomacs.ext.manifest import Manifest, Wanted
from neomacs.ext.wheel import normalize, write_wheel
from neomacs.proc import Completed, run
from neomacs.stability import provisional

# The version of uv the distribution ships (DISTRIBUTION 3.2), and the oldest
# that has what is used here.
UV_PINNED = "0.8.19"
UV_MINIMUM = (0, 5, 3)
PROJECT = "neomacs-extensions-environment"


@provisional("0.1")
class ExtensionError(NeomacsError):
    """Extensions could not be resolved or installed."""


try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


def fingerprint(manifest: Manifest) -> str:
    """Something that changes when what `extensions.toml` asks for changes."""
    wanted = sorted(dataclasses.astuple(w) for w in manifest.wanted.values())
    record = json.dumps([wanted, dataclasses.astuple(manifest.resolver)], sort_keys=True)
    return hashlib.sha256(record.encode()).hexdigest()[:16]


def find_uv() -> str:
    """The `uv` to run: `NEOMACS_UV` if set, otherwise the one on the path."""
    named = os.environ.get("NEOMACS_UV")
    found = named if named else shutil.which("uv")
    if not found or (named and not Path(named).exists()):
        raise ExtensionError(
            "uv is needed to install extensions and was not found. Install it "
            "(https://docs.astral.sh/uv/) or set NEOMACS_UV to where it is"
        )
    return found


def _version(text: str) -> tuple[int, ...]:
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in found.groups()) if found else ()


def _plain(message: str) -> str:
    """The resolver's explanation, in terms of what the user wrote."""
    lines = [
        line
        for line in message.replace("\r", "").split("\n")
        if line.strip() and not line.lstrip().startswith(("Using CPython", "Resolved "))
    ]
    text = "\n".join(line.replace("╰─▶", " ").replace("│", " ").replace("×", " ") for line in lines)
    text = re.sub(r"\n\s+", " ", text).strip()
    # Versions of uv differ in what they put before the explanation itself.
    if "Because " in text:
        text = text[text.index("Because ") :]
    text = text.replace("your project's requirements are", "what extensions.toml asks for is")
    text = text.replace("your project depends on", "extensions.toml asks for")
    text = text.replace("your project requires", "extensions.toml asks for")
    text = re.sub(r"\s*\(we can conclude that\)\s*", " ", text)
    return re.sub(r"[ \t]+", " ", text)


@provisional("0.1")
class Environment:
    """The extensions environment and the two files that describe it."""

    def __init__(
        self,
        *,
        config: Path | None = None,
        data: Path | None = None,
        cache: Path | None = None,
    ) -> None:
        config = config if config is not None else paths.config_dir()
        data = data if data is not None else paths.data_dir()
        self.cache = cache if cache is not None else paths.cache_dir()
        self.manifest_path = config / "extensions.toml"
        self.lock_path = config / "extensions.lock"
        self.root = data / "ext-env"
        self.project = self.root / "resolve"  # where the resolver works
        self.provided = self.root / "provided"  # what stands for the engine's packages
        self.venv = self.root / "venv"
        # Called with a few words as work goes on.
        self.progress: Callable[[str], None] = lambda text: None

    # -- one at a time ------------------------------------------------------------

    @contextlib.contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold the environment for one whole operation: deciding what to
        install, asking, and installing.  Two at once, in one editor or in two
        processes, would each install what it decided without the other's
        change.  A second is refused, not made to wait: the first may be
        waiting for the user."""
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise ExtensionError(
                        "Extensions are being installed or removed already, here or in "
                        "another neomacs. Try again when that has finished"
                    ) from None
            yield
        finally:
            os.close(fd)

    # -- what is there ------------------------------------------------------------

    @property
    def python(self) -> Path:
        if sys.platform == "win32":
            return self.venv / "Scripts" / "python.exe"
        return self.venv / "bin" / "python"

    @provisional("0.1")
    def site_packages(self) -> Path | None:
        """Where the environment's packages are, if it has been made."""
        if sys.platform == "win32":
            found = self.venv / "Lib" / "site-packages"
            return found if found.is_dir() else None
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        for name in (version + "t", version):  # the free-threaded build's directory first
            found = self.venv / "lib" / name / "site-packages"
            if found.is_dir():
                return found
        return None

    @provisional("0.1")
    def manifest(self) -> Manifest:
        return manifest_module.read(self.manifest_path)

    @provisional("0.1")
    def lock(self) -> Lock | None:
        return lock_module.read(self.lock_path)

    def engine_packages(self) -> dict[str, str]:
        """The engine's own distributions and their versions.  The engine has
        no dependencies of its own (ENGINE 12), so this is the engine."""
        return {"neomacs": neomacs.__version__}

    def engine_wheels(self) -> dict[str, Path]:
        wheels: dict[str, Path] = {}
        for name, version in self.engine_packages().items():
            wheels[normalize(name)] = write_wheel(
                self.provided, name, version, summary="Stands for the engine's own package."
            )
        return wheels

    # -- running uv ------------------------------------------------------------------

    async def uv(self, *arguments: str, cwd: Path | None = None) -> Completed:
        uv = find_uv()
        env = {"UV_CACHE_DIR": str(self.cache / "uv"), "UV_NO_PROGRESS": "1", "NO_COLOR": "1"}
        try:
            return await run([uv, *arguments], cwd=cwd, env=env)
        except OSError as exc:
            raise ExtensionError(f"uv could not be run: {exc}") from None

    async def check_uv(self) -> str:
        """The version of uv that will be used; an error if it is too old."""
        done = await self.uv("--version")
        version = _version(done.stdout)
        if done.returncode != 0 or not version:
            raise ExtensionError(f"uv did not say what version it is: {done.stderr.strip()}")
        if version < UV_MINIMUM:
            wanted = ".".join(map(str, UV_MINIMUM))
            raise ExtensionError(
                f"uv {'.'.join(map(str, version))} is too old; {wanted} or newer is needed"
            )
        return ".".join(map(str, version))

    # -- resolving --------------------------------------------------------------------

    def _path(self, text: str) -> str:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.manifest_path.parent / path
        return str(path.resolve())

    def project_text(self, manifest: Manifest) -> str:
        """The `pyproject.toml` given to the resolver."""
        wheels = self.engine_wheels()
        dependencies = [f"{n}=={v}" for n, v in self.engine_packages().items()]
        sources = [f"{name} = {{ path = {json.dumps(str(wheels[name]))} }}" for name in wheels]
        for wanted in manifest.wanted.values():
            if normalize(wanted.name) in wheels:
                raise ExtensionError(f"{wanted.name} is part of the engine, not an extension")
            dependencies.append(f"{wanted.name}{wanted.version}")
            if wanted.git:
                rev = f", rev = {json.dumps(wanted.rev)}" if wanted.rev else ""
                sources.append(f"{wanted.name} = {{ git = {json.dumps(wanted.git)}{rev} }}")
            elif wanted.path:
                editable = ", editable = true" if wanted.editable else ""
                where = json.dumps(self._path(wanted.path))
                sources.append(f"{wanted.name} = {{ path = {where}{editable} }}")
        resolver = manifest.resolver
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        marker = f"sys_platform == '{sys.platform}' and platform_machine == '{platform.machine()}'"
        lines = [
            "# Written by neomacs for the resolver.  Not to be edited.",
            "[project]",
            f"name = {json.dumps(PROJECT)}",
            'version = "0"',
            f'requires-python = "=={version}.*"',
            "dependencies = [",
            *[f"    {json.dumps(d)}," for d in dependencies],
            "]",
            "",
            "[tool.uv]",
            "package = false",
            # Resolve for this machine only: the environment is built for the
            # engine's exact interpreter (EXTENSIONS 4).
            f"environments = [{json.dumps(marker)}]",
        ]
        if resolver.no_index:
            lines.append("no-index = true")
        if resolver.find_links:
            links = ", ".join(json.dumps(self._path(p)) for p in resolver.find_links)
            lines.append(f"find-links = [{links}]")
        lines += ["", "[tool.uv.sources]", *sources]
        if resolver.index:
            lines += [
                "",
                "[[tool.uv.index]]",
                f"url = {json.dumps(resolver.index)}",
                "default = true",
            ]
        for extra in resolver.extra_index:
            lines += ["", "[[tool.uv.index]]", f"url = {json.dumps(extra)}"]
        return "\n".join(lines) + "\n"

    @provisional("0.1")
    async def resolve(self, manifest: Manifest, *, upgrade: bool | Sequence[str] = False) -> Lock:
        """Decide exactly what the environment should hold for MANIFEST.

        Versions decided before are kept unless UPGRADE says otherwise: True
        for everything, or the names to let move.  A set of extensions that
        cannot share an environment raises `ExtensionError` saying why.
        """
        await self.check_uv()
        self.progress("resolving")
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "pyproject.toml").write_text(self.project_text(manifest), encoding="utf-8")
        arguments = ["lock", "--python", sys.executable]
        if upgrade is True:
            arguments.append("--upgrade")
        elif upgrade:
            for name in upgrade:
                arguments += ["--upgrade-package", name]
        if manifest.resolver.offline:
            arguments.append("--offline")
        done = await self.uv(*arguments, cwd=self.project)
        if done.returncode != 0:
            raise ExtensionError(
                f"These extensions cannot be installed together. {_plain(done.stderr)}"
            )
        text = (self.project / "uv.lock").read_text(encoding="utf-8")
        lock = lock_module.from_uv(text, self.project, set(self.engine_wheels()))
        lock.wanted = fingerprint(manifest)
        return lock

    @provisional("0.1")
    def stale(self) -> bool:
        """Whether `extensions.toml` has been changed since the lock file was
        written for it, so that the lock no longer says what it asks for."""
        lock = self.lock()
        return lock is None or lock.wanted != fingerprint(self.manifest())

    @provisional("0.1")
    async def plan(
        self, manifest: Manifest | None = None, *, upgrade: bool | Sequence[str] = False
    ) -> Plan:
        """What resolving MANIFEST (default: the one on disk) would change."""
        manifest = manifest if manifest is not None else self.manifest()
        new = await self.resolve(manifest, upgrade=upgrade)
        return lock_module.plan(self.lock(), new)

    # -- installing --------------------------------------------------------------------

    @provisional("0.1")
    async def sync(self, lock: Lock | None = None) -> None:
        """Make the environment hold exactly what LOCK (default: the lock file) says."""
        lock = lock if lock is not None else self.lock()
        if lock is None:
            raise ExtensionError(f"there is no {self.lock_path.name} to install from")
        await self.check_uv()
        self.progress("installing")
        text, where = lock_module.requirements(lock, self.engine_wheels(), self.engine_packages())
        self.root.mkdir(parents=True, exist_ok=True)
        made = await self.uv("venv", "--allow-existing", "--python", sys.executable, str(self.venv))
        if made.returncode != 0:
            raise ExtensionError(f"The environment could not be made. {_plain(made.stderr)}")
        requirements = self.root / "requirements.txt"
        requirements.write_text(text, encoding="utf-8")
        arguments = ["pip", "sync", "--python", str(self.python), *where, str(requirements)]
        if self.manifest().resolver.offline:
            arguments.append("--offline")
        done = await self.uv(*arguments)
        if done.returncode != 0:
            raise ExtensionError(f"Installing failed. {_plain(done.stderr)}")

    @provisional("0.1")
    async def apply(
        self, plan: Plan, *, add: Wanted | None = None, remove: str | None = None
    ) -> None:
        """Carry out PLAN: record the change in `extensions.toml`, write the
        lock file, and make the environment match it."""
        if add is not None or remove is not None:
            manifest_module.edit(self.manifest_path, add=add, remove=remove)
            # The plan was made for the manifest as it would be; it is that now.
            plan.lock.wanted = fingerprint(self.manifest())
        plan.lock.write(self.lock_path)
        await self.sync(plan.lock)

    def with_change(self, *, add: Wanted | None = None, remove: str | None = None) -> Manifest:
        """The manifest as it would be after a change, without writing it."""
        manifest = self.manifest()
        wanted = dict(manifest.wanted)
        if remove is not None:
            if normalize(remove) not in wanted:
                raise ExtensionError(f"{remove} is not in {self.manifest_path.name}")
            del wanted[normalize(remove)]
        if add is not None:
            wanted[normalize(add.name)] = add
        return dataclasses.replace(manifest, wanted=wanted)
