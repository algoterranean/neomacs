"""Writing a wheel by hand.

The extensions environment holds a distribution called `neomacs` with the
engine's version and no files, so that the resolver knows what the engine is
(EXTENSIONS 4).  It is made here.  Tests make small extensions the same way,
which keeps them off the network.
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from neomacs.stability import provisional


def normalize(name: str) -> str:
    """A distribution name as it is compared: lower case, runs of `-_.` as one `-`."""
    out: list[str] = []
    for ch in name.lower():
        if ch in "-_.":
            if out and out[-1] != "-":
                out.append("-")
        else:
            out.append(ch)
    return "".join(out)


@provisional("0.1")
def write_wheel(
    directory: Path,
    name: str,
    version: str,
    *,
    requires: Sequence[str] = (),
    files: Mapping[str, str] | None = None,
    entry_points: Mapping[str, Mapping[str, str]] | None = None,
    summary: str = "",
) -> Path:
    """Write a pure-Python wheel into DIRECTORY and return its path.

    FILES maps paths inside the wheel to their text.  ENTRY_POINTS maps a
    group to its entries: `{"neomacs.extensions": {"git": "neomacs_git"}}`.
    """
    base = normalize(name).replace("-", "_")
    info = f"{base}-{version}.dist-info"
    contents: dict[str, str] = dict(files or {})
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    if summary:
        metadata += f"Summary: {summary}\n"
    metadata += "".join(f"Requires-Dist: {requirement}\n" for requirement in requires)
    contents[f"{info}/METADATA"] = metadata
    contents[f"{info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: neomacs\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    if entry_points:
        text = ""
        for group, entries in entry_points.items():
            text += f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items()) + "\n"
        contents[f"{info}/entry_points.txt"] = text
    record = ""
    for path, text in contents.items():
        data = text.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record += f"{path},sha256={digest},{len(data)}\n"
    record += f"{info}/RECORD,,\n"
    contents[f"{info}/RECORD"] = record
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{base}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in contents.items():
            # A fixed date, so that the same contents give the same file and hash.
            archive.writestr(zipfile.ZipInfo(path, (2020, 1, 1, 0, 0, 0)), text)
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()
