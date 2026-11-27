"""Generate and check the API manifest (STABILITY 3.4).

python -m neomacs.tools.manifest --write    regenerate api/manifest.json
python -m neomacs.tools.manifest --check    fail if it is out of date
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path
from typing import Any, cast

import neomacs
from neomacs import stability

FORMAT = 1


def _signature(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except TypeError, ValueError:
        return ""


def _entry(name: str, kind: str, obj: Any, mark: dict[str, str]) -> dict[str, str]:
    return {
        "name": name,
        "kind": kind,
        "signature": _signature(obj),
        "tier": mark["tier"],
        "since": mark["since"],
    }


def _class_entries(qualname: str, cls: type) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for attr, value in vars(cls).items():
        mark = stability.info(value)
        if mark is None:
            continue
        if isinstance(value, property):
            entries.append(_entry(f"{qualname}.{attr}", "property", value.fget, mark))
        elif isinstance(value, (classmethod, staticmethod)):
            func = cast(Any, value).__func__
            entries.append(_entry(f"{qualname}.{attr}", "method", func, mark))
        elif inspect.isfunction(value):
            entries.append(_entry(f"{qualname}.{attr}", "method", value, mark))
    return entries


def collect() -> dict[str, Any]:
    """Walk the package and return the manifest as a dictionary."""
    entries: list[dict[str, str]] = []
    modules = [neomacs]
    for found in pkgutil.walk_packages(neomacs.__path__, "neomacs."):
        try:
            modules.append(importlib.import_module(found.name))
        except ImportError:
            continue  # an optional dependency is missing
    for module in modules:
        for attr, value in vars(module).items():
            if getattr(value, "__module__", None) != module.__name__:
                continue
            mark = stability.info(value)
            qualname = f"{module.__name__}.{attr}"
            if inspect.isclass(value):
                if mark is not None:
                    entries.append(_entry(qualname, "class", value, mark))
                entries.extend(_class_entries(qualname, value))
            elif mark is not None and callable(value):
                entries.append(_entry(qualname, "function", value, mark))
    for name, mark in stability.constants().items():
        entries.append(
            {"name": name, "kind": "constant", "signature": "", "tier": mark["tier"],
             "since": mark["since"]}
        )  # fmt: skip
    entries.sort(key=lambda e: e["name"])
    return {"format": FORMAT, "names": entries}


def render(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"


def default_path() -> Path:
    return Path(neomacs.__file__).resolve().parent.parent / "api" / "manifest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neomacs.tools.manifest")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    parser.add_argument("--path", type=Path, default=None)
    args = parser.parse_args(argv)
    path: Path = args.path or default_path()
    text = render(collect())
    if args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
        return 0
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current != text:
        print(
            f"{path} is out of date; run `python -m neomacs.tools.manifest --write` "
            "and commit the result",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
