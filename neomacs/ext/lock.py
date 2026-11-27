"""`extensions.lock`: exactly what is in the extensions environment (EXTENSIONS 4.1).

The format is this module's own.  It is made from what the resolver decided
and is what `sync` installs from, so the environment can be built again on
another machine from `extensions.toml` and this file alone.
"""

from __future__ import annotations

import json
import sysconfig
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from neomacs.errors import NeomacsError
from neomacs.ext.wheel import normalize, sha256
from neomacs.stability import provisional

FORMAT = 1
ENGINE = "engine"  # the source of the distribution that stands for the engine itself


@provisional("0.1")
class LockError(NeomacsError):
    """The lock file cannot be read, or cannot be installed from here."""


@provisional("0.1")
@dataclass(frozen=True)
class Locked:
    """One distribution in the environment."""

    name: str
    version: str
    # "registry", "path" (a wheel or source archive), "directory", "editable",
    # "git", or "engine".
    source: str = "registry"
    location: str = ""  # an index address, a directory of wheels, a path, or a git address
    revision: str = ""  # the commit, for git
    hashes: tuple[str, ...] = ()
    # Whether there is a wheel this interpreter can use without building.
    # None where that cannot be told (a path, a git repository).
    wheel: bool | None = None

    @property
    def what(self) -> str:
        """How it is described to the user."""
        where = {
            "git": f" from {self.location}"
            + (f" at {self.revision[:12]}" if self.revision else ""),
            "path": f" from {self.location}",
            "directory": f" from {self.location}",
            "editable": f" from {self.location} (editable)",
        }.get(self.source, "")
        return f"{self.name} {self.version}{where}"


@provisional("0.1")
@dataclass
class Lock:
    distributions: dict[str, Locked] = field(default_factory=dict[str, Locked])
    # A mark of the `extensions.toml` this was resolved for.
    wanted: str = ""

    def write(self, path: Path) -> None:
        lines = [
            "# Written by neomacs.  Do not edit: change extensions.toml and run",
            "# `neomacs ext sync`, or use the extensions commands in the editor.",
            f"format = {FORMAT}",
            f"wanted = {json.dumps(self.wanted)}",
            "",
        ]
        for name in sorted(self.distributions):
            dist = self.distributions[name]
            lines += ["[[distribution]]", f"name = {json.dumps(dist.name)}"]
            lines.append(f"version = {json.dumps(dist.version)}")
            lines.append(f"source = {json.dumps(dist.source)}")
            if dist.location:
                lines.append(f"location = {json.dumps(dist.location)}")
            if dist.revision:
                lines.append(f"revision = {json.dumps(dist.revision)}")
            if dist.hashes:
                lines.append("hashes = [")
                lines += [f"    {json.dumps(h)}," for h in dist.hashes]
                lines.append("]")
            if dist.wheel is not None:
                lines.append(f"wheel = {'true' if dist.wheel else 'false'}")
            lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(path)


@provisional("0.1")
def read(path: Path) -> Lock | None:
    """Read `extensions.lock`; None if there is none."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LockError(f"{path}: {exc}") from None
    if data.get("format") != FORMAT:
        raise LockError(
            f"{path} is in format {data.get('format')!r}, which this neomacs does not read; "
            "run `neomacs ext upgrade` to write it again"
        )
    lock = Lock(wanted=str(data.get("wanted", "")))
    for entry in cast(list[dict[str, Any]], data.get("distribution", [])):
        try:
            dist = Locked(
                str(entry["name"]),
                str(entry["version"]),
                str(entry.get("source", "registry")),
                str(entry.get("location", "")),
                str(entry.get("revision", "")),
                tuple(str(h) for h in entry.get("hashes", ())),
                entry.get("wheel"),
            )
        except (KeyError, TypeError) as exc:
            raise LockError(f"{path}: a distribution has no {exc}") from None
        lock.distributions[normalize(dist.name)] = dist
    return lock


# -- from the resolver's answer -----------------------------------------------------


def free_threaded() -> bool:
    return bool(sysconfig.get_config_var("Py_GIL_DISABLED"))


def wheel_usable_without_gil(filename: str) -> bool:
    """Whether a wheel of this name can be used by a free-threaded interpreter
    as it is: one with no compiled code, or one built for free threading."""
    parts = filename.removesuffix(".whl").split("-")
    if len(parts) < 5:
        return False
    return any(abi == "none" or abi.endswith("t") for abi in parts[-2].split("."))


def _is_url(text: str) -> bool:
    return "://" in text


def from_uv(text: str, project: Path, engine: set[str]) -> Lock:
    """Our lock, from the `uv.lock` the resolver wrote in PROJECT.  ENGINE is
    the names of the distributions that stand for the engine's own packages."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LockError(f"the resolver's answer could not be read: {exc}") from None
    lock = Lock()
    for package in cast(list[dict[str, Any]], data.get("package", [])):
        source = cast(dict[str, Any], package.get("source", {}))
        if "virtual" in source:
            continue  # the made-up project that asks for the extensions
        name, version = str(package["name"]), str(package.get("version", ""))
        wheels = cast(list[dict[str, Any]], package.get("wheels", []))
        sdist = cast(dict[str, Any], package.get("sdist", {}))
        hashes = [str(w["hash"]) for w in wheels if "hash" in w]
        if "hash" in sdist:
            hashes.append(str(sdist["hash"]))

        def absolute(value: str) -> str:
            return value if _is_url(value) else str((project / value).resolve())

        if normalize(name) in engine:
            dist = Locked(name, version, ENGINE)
        elif "registry" in source:
            location = absolute(str(source["registry"]))
            if not _is_url(location):
                # A directory of files: the resolver records no hashes for those.
                for wheel in wheels:
                    file = Path(location) / str(wheel.get("path", ""))
                    if "hash" not in wheel and file.is_file():
                        hashes.append(sha256(file))
            names = [
                str(w.get("filename") or w.get("path") or str(w.get("url", "")).rsplit("/", 1)[-1])
                for w in wheels
            ]
            usable = (
                any(wheel_usable_without_gil(n) for n in names) if free_threaded() else bool(names)
            )
            dist = Locked(
                name, version, "registry", location, "", tuple(sorted(set(hashes))), usable
            )
        elif "git" in source:
            address, _, commit = str(source["git"]).partition("#")
            dist = Locked(name, version, "git", address.split("?", 1)[0], commit)
        elif "editable" in source:
            dist = Locked(name, version, "editable", absolute(str(source["editable"])))
        elif "directory" in source:
            dist = Locked(name, version, "directory", absolute(str(source["directory"])))
        elif "path" in source:
            dist = Locked(
                name, version, "path", absolute(str(source["path"])), "", tuple(sorted(set(hashes)))
            )
        elif "url" in source:
            dist = Locked(name, version, "path", str(source["url"]), "", tuple(sorted(set(hashes))))
        else:
            raise LockError(f"{name}: the resolver gave a source this neomacs does not know")
        lock.distributions[normalize(name)] = dist
    return lock


# -- to something that can be installed ------------------------------------------------


def requirements(
    lock: Lock, engine_wheels: dict[str, Path], engine_versions: dict[str, str]
) -> tuple[str, list[str]]:
    """The requirements file that installs LOCK, and the arguments that say
    where to look.  ENGINE_WHEELS has the file standing for each of the
    engine's packages, and ENGINE_VERSIONS the version this engine has of each.

    A lock resolved for another engine is refused: what it holds was chosen
    to go with that one, and may not go with this."""
    versions = {normalize(name): version for name, version in engine_versions.items()}
    lines: list[str] = []
    indexes: list[str] = []
    links: list[str] = []
    for name in sorted(lock.distributions):
        dist = lock.distributions[name]
        hashes = "".join(f" --hash={h}" for h in dist.hashes)
        if dist.source == ENGINE:
            wheel = engine_wheels.get(name)
            if wheel is None or versions.get(name) != dist.version:
                have = versions.get(name)
                raise LockError(
                    f"the lock file is for an engine that has {dist.name} {dist.version}, and "
                    + (f"this one has {have}" if have else "this one has no such package")
                    + "; run `neomacs ext upgrade` to resolve again for this one"
                )
            lines.append(f"{dist.name} @ {wheel.as_uri()}")
        elif dist.source == "registry":
            lines.append(f"{dist.name}=={dist.version}{hashes}")
            target = indexes if _is_url(dist.location) else links
            if dist.location and dist.location not in target:
                target.append(dist.location)
        elif dist.source == "git":
            at = f"@{dist.revision}" if dist.revision else ""
            lines.append(f"{dist.name} @ git+{dist.location}{at}")
        elif dist.source == "editable":
            lines.append(f"-e {dist.location}")
        elif dist.source in ("directory", "path"):
            address = dist.location if _is_url(dist.location) else Path(dist.location).as_uri()
            lines.append(f"{dist.name} @ {address}{hashes}")
        else:
            raise LockError(f"{dist.name}: unknown source {dist.source!r} in the lock file")
    arguments: list[str] = []
    if indexes:
        arguments += ["--index-url", indexes[0]]
        for extra in indexes[1:]:
            arguments += ["--extra-index-url", extra]
    elif links:
        arguments.append("--no-index")
    for directory in links:
        arguments += ["--find-links", directory]
    return "\n".join(lines) + "\n", arguments


# -- what would change ------------------------------------------------------------------


@provisional("0.1")
@dataclass
class Plan:
    """What installing a new lock would do to the environment."""

    lock: Lock
    added: list[Locked] = field(default_factory=list[Locked])
    changed: list[tuple[Locked, Locked]] = field(default_factory=list[tuple[Locked, Locked]])
    removed: list[Locked] = field(default_factory=list[Locked])

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.removed)

    @property
    def without_wheel(self) -> list[Locked]:
        """Distributions to be installed that have no wheel this interpreter
        can use as it is.  They are built from source, and what is built may
        turn the GIL back on (EXTENSIONS 4.3)."""
        incoming = self.added + [new for _, new in self.changed]
        return [d for d in incoming if d.wheel is False]

    def describe(self) -> str:
        if self.empty:
            return "Nothing to change.\n"
        lines: list[str] = []
        if self.added:
            lines.append("To be added:")
            lines += [f"  + {d.what}" for d in self.added]
        if self.changed:
            lines.append("To be changed:")
            lines += [f"  ~ {old.what}  ->  {new.what}" for old, new in self.changed]
        if self.removed:
            lines.append("To be removed:")
            lines += [f"  - {d.what}" for d in self.removed]
        if self.without_wheel:
            kind = "free-threaded " if free_threaded() else ""
            lines.append(f"No {kind}wheel; these are built from source, and may turn the GIL on:")
            lines += [f"  ! {d.name} {d.version}" for d in self.without_wheel]
        return "\n".join(lines) + "\n"


def plan(old: Lock | None, new: Lock) -> Plan:
    """The difference between the environment as locked before and NEW."""
    before = old.distributions if old is not None else {}
    result = Plan(new)
    for name in sorted(new.distributions):
        dist = new.distributions[name]
        if dist.source == ENGINE:
            continue
        previous = before.get(name)
        if previous is None:
            result.added.append(dist)
        elif (previous.version, previous.source, previous.location, previous.revision) != (
            dist.version, dist.source, dist.location, dist.revision,
        ):  # fmt: skip
            result.changed.append((previous, dist))
    for name in sorted(before):
        if name not in new.distributions and before[name].source != ENGINE:
            result.removed.append(before[name])
    return result
