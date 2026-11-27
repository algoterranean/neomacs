"""Making small extensions for tests, as wheels in a directory, with no network."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from neomacs.ext.wheel import write_wheel

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")

COMMAND = """\
from neomacs import ed
from neomacs.commands import command

loaded = True


@command(name="{name}.hello")
def hello() -> None:
    '''Say hello from {name}.'''
    ed.message("hello from {name} {version}")
"""


def extension(
    wheels: Path,
    name: str,
    version: str = "0.1",
    *,
    requires: tuple[str, ...] = (),
    source: str | None = None,
    declarations: str | None = None,
    extra: dict[str, str] | None = None,
) -> Path:
    """Write the wheel of an extension called neomacs-NAME, whose package is neomacs_NAME."""
    package = f"neomacs_{name}"
    files = {f"{package}/__init__.py": source or COMMAND.format(name=name, version=version)}
    if declarations is not None:
        files[f"{package}/neomacs_extension.toml"] = declarations
    for path, text in (extra or {}).items():
        files[f"{package}/{path}"] = text
    return write_wheel(
        wheels,
        f"neomacs-{name}",
        version,
        requires=requires,
        files=files,
        entry_points={"neomacs.extensions": {name: package}},
        summary=f"The {name} extension",
    )


def library(wheels: Path, name: str, version: str, requires: tuple[str, ...] = ()) -> Path:
    module = name.replace("-", "_")
    return write_wheel(
        wheels, name, version, requires=requires, files={f"{module}.py": f"VERSION = {version!r}\n"}
    )


def config(directory: Path, wheels: Path, *names: str) -> Path:
    """A configuration directory whose `extensions.toml` takes everything from WHEELS."""
    directory.mkdir(parents=True, exist_ok=True)
    text = f'[resolver]\nno-index = true\noffline = true\nfind-links = ["{wheels}"]\n'
    if names:
        text += "\n[extensions]\n" + "".join(f'{n} = "*"\n' for n in names)
    (directory / "extensions.toml").write_text(text)
    return directory


def dirs(root: Path) -> dict[str, str]:
    """The environment that keeps an engine, or `neomacs ext`, to directories under ROOT."""
    return {
        "NEOMACS_CONFIG_DIR": str(root / "config"),
        "NEOMACS_DATA_DIR": str(root / "data"),
        "NEOMACS_CACHE_DIR": str(root / "cache"),
    }
