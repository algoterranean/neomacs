"""`extensions.toml`: the extensions the user asked for (EXTENSIONS 4.1).

```toml
[extensions]
neomacs-git = ">=0.3"
neomacs-rust = { git = "https://example.org/neomacs-rust.git", rev = "v0.2.1" }
my-tools = { path = "~/src/my-tools", editable = true }

[resolver]                        # optional: where distributions come from
index = "https://pypi.org/simple"
find-links = ["~/wheels"]
no-index = false
offline = false
```
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from neomacs.errors import NeomacsError
from neomacs.ext.wheel import normalize
from neomacs.stability import provisional

_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_SPLIT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$")


@provisional("0.1")
class ManifestError(NeomacsError):
    """`extensions.toml` says something that cannot be acted on."""


@provisional("0.1")
@dataclass(frozen=True)
class Wanted:
    """One extension the user asked for."""

    name: str
    version: str = ""  # a version specifier such as ">=0.3"; empty for any
    git: str = ""
    rev: str = ""
    path: str = ""
    editable: bool = False

    def toml(self) -> str:
        """The value written for this entry in `extensions.toml`."""
        if self.git:
            rev = f", rev = {json.dumps(self.rev)}" if self.rev else ""
            return f"{{ git = {json.dumps(self.git)}{rev} }}"
        if self.path:
            editable = ", editable = true" if self.editable else ""
            return f"{{ path = {json.dumps(self.path)}{editable} }}"
        return json.dumps(self.version or "*")


@provisional("0.1")
@dataclass
class Resolver:
    """Where distributions are looked for."""

    index: str = ""
    extra_index: list[str] = field(default_factory=list[str])
    find_links: list[str] = field(default_factory=list[str])
    no_index: bool = False
    offline: bool = False


@provisional("0.1")
@dataclass
class Manifest:
    path: Path
    wanted: dict[str, Wanted] = field(default_factory=dict[str, Wanted])
    resolver: Resolver = field(default_factory=Resolver)


def _strings(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in cast(list[Any], value)):
        return cast(list[str], value)
    raise ManifestError(f"{where} must be a string or a list of strings")


def parse_entry(name: str, value: Any) -> Wanted:
    if not _NAME.match(name):
        raise ManifestError(f"{name!r} is not a distribution name")
    if isinstance(value, str):
        return Wanted(name, "" if value.strip() in ("", "*") else value.strip())
    if isinstance(value, dict):
        table = cast(dict[str, Any], value)
        unknown = set(table) - {"git", "rev", "path", "editable", "version"}
        if unknown:
            raise ManifestError(f"{name}: unknown keys {sorted(unknown)}")
        git, path = table.get("git", ""), table.get("path", "")
        if git and path:
            raise ManifestError(f"{name}: give `git` or `path`, not both")
        if not git and not path and not table.get("version"):
            raise ManifestError(f"{name}: give one of a version, `git` or `path`")
        return Wanted(
            name,
            str(table.get("version", "")),
            str(git),
            str(table.get("rev", "")),
            str(path),
            bool(table.get("editable", False)),
        )
    raise ManifestError(f"{name}: expected a version or a table, not {value!r}")


@provisional("0.1")
def read(path: Path) -> Manifest:
    """Read `extensions.toml`.  A file that is not there is an empty manifest."""
    manifest = Manifest(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return manifest
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{path}: {exc}") from None
    extensions = data.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ManifestError(f"{path}: [extensions] must be a table")
    seen: dict[str, str] = {}
    for name, value in cast(dict[str, Any], extensions).items():
        entry = parse_entry(name, value)
        key = normalize(name)
        if key in seen:
            raise ManifestError(f"{path}: {name} and {seen[key]} are the same distribution")
        seen[key] = name
        manifest.wanted[key] = entry
    table = data.get("resolver", {})
    if not isinstance(table, dict):
        raise ManifestError(f"{path}: [resolver] must be a table")
    resolver = cast(dict[str, Any], table)
    unknown = set(resolver) - {"index", "extra-index", "find-links", "no-index", "offline"}
    if unknown:
        raise ManifestError(f"{path}: unknown keys in [resolver]: {sorted(unknown)}")
    manifest.resolver = Resolver(
        str(resolver.get("index", "")),
        _strings(resolver.get("extra-index", []), "extra-index"),
        _strings(resolver.get("find-links", []), "find-links"),
        bool(resolver.get("no-index", False)),
        bool(resolver.get("offline", False)),
    )
    return manifest


@provisional("0.1")
def parse_request(text: str, *, base: Path | None = None) -> Wanted:
    """What the user typed to add an extension, as an entry.

    `neomacs-git`, `neomacs-git>=0.3`, `neomacs-rust @ git+https://host/repo.git@v1`,
    `my-tools @ ~/src/my-tools`, or a path to a directory with a `pyproject.toml`.
    """
    text = text.strip()
    if not text:
        raise ManifestError("no extension named")
    if "@" in text and (" @ " in text or text.partition("@")[2].strip().startswith("git+")):
        name, _, target = text.partition("@")
        name, target = name.strip(), target.strip()
        if not target.startswith("git+"):
            directory = Path(target).expanduser()
            if base is not None and not directory.is_absolute():
                # As typed where the command was run, not where the manifest is.
                return parse_entry(name, {"path": str(base / directory)})
            return parse_entry(name, {"path": target})
        url, rev = target[4:], ""
        # A revision follows the last "@", when that comes after the last "/"
        # (an "@" earlier than that is part of the address: `git@host:...`).
        if "@" in url.rsplit("/", 1)[-1]:
            url, _, rev = url.rpartition("@")
        return parse_entry(name, {"git": url, **({"rev": rev} if rev else {})})
    looks_like_path = text.startswith(("/", "~", ".")) or (base is not None and "/" in text)
    if looks_like_path:
        directory = Path(text).expanduser()
        if base is not None and not directory.is_absolute():
            directory = base / directory
        try:
            project = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
            name = str(project["project"]["name"])
        except OSError, KeyError, TypeError, tomllib.TOMLDecodeError:
            raise ManifestError(
                f"{text} has no pyproject.toml naming a project; write it as NAME @ {text}"
            ) from None
        return parse_entry(name, {"path": str(directory)})
    found = _SPLIT.match(text)
    if found is None:
        raise ManifestError(f"cannot tell what extension {text!r} is")
    return parse_entry(found.group(1), found.group(2).strip())


_SECTION = re.compile(r"""^\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?$""")
_KEY = re.compile(r"""^\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*(\.|=)""")


def _unquote(key: str) -> str:
    key = key.strip()
    return key[1:-1] if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'" else key


def _without(lines: list[str], target: str) -> list[str]:
    """LINES without the entry for TARGET, however it was written: a line of
    the `[extensions]` table, dotted keys there, or a table of its own
    (`[extensions.NAME]`)."""
    kept: list[str] = []
    section = ""  # the table the line is in
    dropping = False  # inside `[extensions.TARGET]`
    for line in lines:
        heading = _SECTION.match(line)
        if heading is not None:
            if dropping and kept and kept[-1].strip():
                kept.append("")  # the blank line that stood before the table removed
            section = heading.group(1)
            parts = [_unquote(p) for p in section.split(".", 1)]
            dropping = (
                len(parts) == 2 and parts[0] == "extensions" and normalize(parts[1]) == target
            )
            if dropping:
                while kept and kept[-1].strip() == "":
                    kept.pop()
                continue
        if dropping:
            continue
        if section.strip() == "extensions":
            key = _KEY.match(line)
            name = next((g for g in key.groups()[:3] if g), "") if key is not None else ""
            if name and normalize(name) == target:
                continue
        kept.append(line)
    return kept


@provisional("0.1")
def edit(path: Path, *, add: Wanted | None = None, remove: str | None = None) -> None:
    """Add, replace or remove one entry of `extensions.toml`, leaving the rest
    of the file, comments included, as the user wrote it.

    The result is checked before the file is replaced: if it would not be a
    manifest, `ManifestError` is raised and the file is left as it was.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    target = normalize(add.name if add is not None else remove or "")
    lines = _without(original.split("\n") if original else [], target)
    if add is not None:
        entry = f"{add.name} = {add.toml()}"
        headings = [i for i, line in enumerate(lines) if _SECTION.match(line) is not None]
        start = next(
            (i for i in headings if _SECTION.match(lines[i]).group(1).strip() == "extensions"),  # type: ignore[union-attr]
            None,
        )
        if start is None:
            while lines and lines[-1].strip() == "":
                lines.pop()
            lines += ([""] if lines else []) + ["[extensions]", entry]
        else:
            # After the last line of the table that has anything on it.
            end = next((i for i in headings if i > start), len(lines))
            last = max((i for i in range(start, end) if lines[i].strip()), default=start)
            lines.insert(last + 1, entry)
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if text == original:
        # Nothing was found to take out.  If the entry is there all the same,
        # written some way this does not follow, say so: do not leave it behind.
        if remove is not None and original and target in read(path).wanted:
            raise ManifestError(f"{path}: the entry for {target} could not be removed by editing")
        return
    # It must still be a manifest, with the entry there or gone, before it is the file.
    path.parent.mkdir(parents=True, exist_ok=True)
    trial = path.with_name(path.name + ".tmp")
    trial.write_text(text, encoding="utf-8")
    try:
        result = read(trial)
        if (target in result.wanted) != (add is not None):
            what = "added" if add is not None else "removed"
            raise ManifestError(f"{path}: the entry for {target} could not be {what} by editing")
    except ManifestError as exc:
        trial.unlink(missing_ok=True)
        raise ManifestError(str(exc).replace(str(trial), str(path))) from None
    trial.replace(path)
