"""`neomacs ext`: managing extensions from the command line (EXTENSIONS 4.2).

The same operations as the `extensions.*` commands in the editor.  An editor
that is running does not notice what is done here until it is restarted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from neomacs.errors import NeomacsError
from neomacs.ext import manifest as manifest_module
from neomacs.ext.environment import Environment
from neomacs.ext.lock import Plan
from neomacs.ext.manifest import Wanted


def _confirm(plan: Plan, yes: bool) -> bool:
    print(plan.describe(), end="")
    if plan.empty or yes:
        return True
    if not sys.stdin.isatty():
        print("Not installed: say --yes to go ahead without being asked.", file=sys.stderr)
        return False
    return input("Go ahead? [y/N] ").strip().lower() in ("y", "yes")


def _request(args: argparse.Namespace) -> Wanted:
    if args.git:
        return manifest_module.parse_entry(
            args.name, {"git": args.git, **({"rev": args.rev} if args.rev else {})}
        )
    if args.path:
        return manifest_module.parse_entry(
            args.name,
            {"path": str(Path(args.path).expanduser().resolve()), "editable": args.editable},
        )
    return manifest_module.parse_request(args.name, base=Path.cwd())


async def _run(args: argparse.Namespace) -> int:
    env = Environment()
    env.progress = lambda text: print(f"{text}...", file=sys.stderr)
    if args.action == "add":
        wanted = _request(args)
        plan = await env.plan(env.with_change(add=wanted))
        if not _confirm(plan, args.yes):
            return 1
        await env.apply(plan, add=wanted)
        print(f"Added {wanted.name}.")
    elif args.action == "remove":
        plan = await env.plan(env.with_change(remove=args.name))
        if not _confirm(plan, args.yes):
            return 1
        await env.apply(plan, remove=args.name)
        print(f"Removed {args.name}.")
    elif args.action == "upgrade":
        plan = await env.plan(upgrade=[args.name] if args.name else True)
        if not _confirm(plan, args.yes):
            return 1
        await env.apply(plan)
        print("Nothing newer." if plan.empty else "Upgraded.")
    elif args.action == "sync":
        if env.lock() is None:
            plan = await env.plan()
            if not _confirm(plan, args.yes):
                return 1
            await env.apply(plan)
        else:
            await env.sync()
        print("The environment matches the lock file.")
    elif args.action == "list":
        _list(env)
    return 0


def _list(env: Environment) -> None:
    from neomacs.ext import registry

    site_packages = env.site_packages()
    if site_packages is not None and str(site_packages) not in sys.path:
        import site

        site.addsitedir(str(site_packages))
    found = registry.scan()
    wanted = env.manifest().wanted
    if not found and not wanted:
        print("No extensions are installed.")
        return
    width = max((len(e.name) for e in found), default=4)
    for extension in found:
        how = "loaded on first use" if extension.lazy else "loaded at startup"
        print(f"{extension.name:<{width}}  {extension.version:<10}  {how:<20}  {extension.summary}")
    installed = {manifest_module.normalize(e.distribution) for e in found}
    for name, entry in wanted.items():
        if name not in installed:
            print(f"{entry.name}: in extensions.toml and not installed; run `neomacs ext sync`")
    print("Whether one turned the GIL back on is known only in a running editor: extensions.list.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="neomacs ext", description="Manage extensions.")
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="add an extension, resolve, and install")
    add.add_argument("name", help="a name, name>=version, NAME @ git+URL[@REV], or a directory")
    add.add_argument("--git", metavar="URL", help="install NAME from this git repository")
    add.add_argument("--rev", metavar="REV", help="with --git: the branch, tag or commit")
    add.add_argument("--path", metavar="DIR", help="install NAME from this directory")
    add.add_argument("--editable", action="store_true", help="with --path: use the source in place")
    remove = actions.add_parser("remove", help="remove an extension and resolve again")
    remove.add_argument("name")
    upgrade = actions.add_parser("upgrade", help="resolve again, allowing newer versions")
    upgrade.add_argument("name", nargs="?", help="only this distribution")
    actions.add_parser("sync", help="make the environment match the lock file")
    actions.add_parser("list", help="say what is installed")
    for sub in (add, remove, upgrade, actions.choices["sync"]):
        sub.add_argument("--yes", "-y", action="store_true", help="do not ask before installing")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except NeomacsError as exc:
        print(f"neomacs ext: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
