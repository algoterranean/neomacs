"""Standard commands: extensions (EXTENSIONS 4.2, 5)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from neomacs.commands import Ask, command
from neomacs.completion import Candidate, FunctionCompleter, rank
from neomacs.editor import ed
from neomacs.errors import NeomacsError
from neomacs.ext import manifest as manifest_module
from neomacs.ext.environment import Environment
from neomacs.ext.lock import Plan
from neomacs.ext.registry import Extension
from neomacs.keymap import Keymap
from neomacs.live import run as live
from neomacs.text.buffer import Buffer

LIST = "*extensions*"

STATES = {
    "known": "loaded on first use",
    "loaded": "loaded",
    "disabled": "disabled",
    "failed": "failed to load",
    "restart": "restart to finish",
}


class ExtensionsMode(live.SpecialMode):
    """The mode of the list of extensions."""

    name = "extensions"
    interactive = False
    keymap = Keymap("extensions")


assert ExtensionsMode.keymap is not None
ExtensionsMode.keymap.bind("g", "extensions.list")


def _show(text: str, *, focus: bool) -> Buffer:
    buffer = live.special_buffer(LIST, ExtensionsMode)
    buffer.insert(0, text)
    live.finish_special(buffer)
    view = ed.show(buffer, "help", focus=focus)
    view.selections.set([(0, 0)])
    view.reveal(0, "top")
    return buffer


def _names(states: tuple[str, ...] | None = None) -> FunctionCompleter:
    def complete(text: str) -> list[Candidate]:
        found = [
            Candidate(e.name, f"{STATES.get(e.state, e.state)}  {e.summary}".strip())
            for e in ed.extensions
            if states is None or e.state in states
        ]
        return rank(found, text)

    return FunctionCompleter(complete)


async def _ask_extension(prompt: str, states: tuple[str, ...] | None = None) -> str:
    return await ed.minibuffer.read(prompt, completer=_names(states), history="extensions")


# -- installing: each of these works in the background ----------------------------------


async def _confirm(plan: Plan, what: str) -> bool:
    """Show what would be installed and ask.  Nothing is installed unasked (EXTENSIONS 8)."""
    if plan.empty:
        return True
    _show(f"{what}\n\n{plan.describe()}", focus=False)
    answer = await ed.minibuffer.read("Install these? (yes or no) ")
    if answer.strip().lower() not in ("y", "yes"):
        ed.message("Nothing was installed")
        return False
    return True


def _replaced(plan: Plan) -> set[str]:
    """The distributions a plan puts other files in place of."""
    return {new.name for _, new in plan.changed}


def _in_background(
    name: str, work: Callable[[Environment], Awaitable[tuple[str, set[str]] | None]]
) -> None:
    """Run WORK as a task, with what it is doing shown in the task list, and
    then have the running editor take up what changed.  WORK returns what to
    say and which distributions were replaced, or None if nothing was done.

    The environment is held from deciding what to install until it is
    installed, so two of these cannot each install without the other's change."""
    env = Environment()

    async def task() -> None:
        try:
            with env.exclusive():
                result = await work(env)
        except NeomacsError as exc:
            # Said in full where it can be read: a conflict takes sentences to explain.
            _show(f"{name}\n\n{exc}\n", focus=False)
            ed.message(str(exc).split(". ", 1)[0] + ". See *extensions*.", kind="error")
            return
        if result is None:
            return
        said, replaced = result
        changes = await ed.extensions.refresh(env.site_packages(), replaced)
        ed.message("; ".join([said, *changes]))

    info = ed.start_task(task(), name=name)

    def progress(text: str) -> None:
        info.progress = text
        ed.update_frames()

    env.progress = progress


@command(name="extensions.add")
def add_extension(
    request: Annotated[
        str, Ask.string("Add extension (NAME, NAME>=1.0, NAME @ git+URL, or a directory): ")
    ],
) -> None:
    """Add an extension: resolve it with the others, show what would be
    installed, and on a yes install and activate it."""
    wanted = manifest_module.parse_request(request)

    async def work(env: Environment) -> tuple[str, set[str]] | None:
        plan = await env.plan(env.with_change(add=wanted))
        if not await _confirm(plan, f"Adding {wanted.name}"):
            return None
        await env.apply(plan, add=wanted)
        return f"Added {wanted.name}", _replaced(plan)

    _in_background(f"extensions: add {wanted.name}", work)


@command(name="extensions.remove")
async def remove_extension() -> None:
    """Remove an extension: deactivate it, take it out of `extensions.toml`,
    and resolve the rest again."""
    listed = [Candidate(w.name) for w in Environment().manifest().wanted.values()]
    if not listed:
        raise NeomacsError("extensions.toml lists no extensions")
    name = await ed.minibuffer.read(
        "Remove extension: ", completer=FunctionCompleter(lambda text: rank(listed, text))
    )

    async def work(env: Environment) -> tuple[str, set[str]] | None:
        plan = await env.plan(env.with_change(remove=name))
        if not await _confirm(plan, f"Removing {name}"):
            return None
        # Deactivated while its code is still there to be asked.
        gone = {manifest_module.normalize(d.name) for d in plan.removed}
        for extension in list(ed.extensions):
            if manifest_module.normalize(extension.distribution) in gone:
                if extension.state != "disabled":
                    ed.extensions.disable(extension.name)
        await env.apply(plan, remove=name)
        return f"Removed {name}", _replaced(plan)

    _in_background(f"extensions: remove {name}", work)


@command(name="extensions.upgrade")
def upgrade_extensions(
    name: Annotated[str, Ask.string("Upgrade (a distribution, or nothing for all): ")] = "",
) -> None:
    """Resolve again allowing newer versions, install them, and reload what changed."""
    only = name.strip()

    async def work(env: Environment) -> tuple[str, set[str]] | None:
        plan = await env.plan(upgrade=[only] if only else True)
        if plan.empty:
            return "Nothing newer", set()
        if not await _confirm(plan, "Upgrading " + (only or "everything")):
            return None
        await env.apply(plan)
        return "Upgraded", _replaced(plan)

    _in_background("extensions: upgrade " + (only or "all"), work)


@command(name="extensions.sync")
def sync_extensions() -> None:
    """Make the extensions environment match the lock file.  If
    `extensions.toml` has been edited since the lock file was written, what it
    now asks for is resolved first."""

    async def work(env: Environment) -> tuple[str, set[str]] | None:
        if env.stale():
            plan = await env.plan()
            if not await _confirm(plan, "Installing what extensions.toml asks for"):
                return None
            await env.apply(plan)
            return "The environment matches extensions.toml", _replaced(plan)
        await env.sync()
        return "The environment matches the lock file", set()

    _in_background("extensions: sync", work)


# -- what there is, and turning it off and on ---------------------------------------------


def _line(extension: Extension) -> str:
    state = STATES.get(extension.state, extension.state)
    if extension.error:
        state += f" ({extension.error})"
    gil = "  turned the GIL on" if extension.gil else ""
    where = extension.version or ("single file" if extension.kind == "file" else "")
    summary = f"  {extension.summary}" if extension.summary else ""
    return f"{extension.name}  {where}  {state}{gil}{summary}\n"


@command(name="extensions.list")
def list_extensions() -> None:
    """List the extensions: name, version, state, and whether one turned the GIL back on."""
    extensions = sorted(ed.extensions, key=lambda e: e.name)
    text = "".join(_line(e) for e in extensions) or "No extensions are installed.\n"
    if any(e.state == "restart" for e in extensions):
        text += "\nSome changes need the editor to be restarted to take effect.\n"
    check: Any = getattr(__import__("sys"), "_is_gil_enabled", None)
    if check is not None:
        text += f"\nThe GIL is {'on' if check() else 'off'}.\n"
    _show(text, focus=True)


@command(name="extensions.disable")
async def disable_extension() -> None:
    """Turn an extension off: its commands, options, hooks, modes, keys,
    timers and tasks go.  It stays installed, and off until the editor restarts."""
    name = await _ask_extension("Disable extension: ", ("known", "loaded", "failed", "restart"))
    ed.extensions.disable(name)
    ed.message(f"Disabled {name}")


@command(name="extensions.enable")
async def enable_extension() -> None:
    """Turn a disabled extension back on."""
    name = await _ask_extension("Enable extension: ", ("disabled", "failed"))
    extension = await ed.extensions.enable(name)
    if extension.state != "loaded":
        raise NeomacsError(f"{name} could not be loaded: {extension.error}")
    ed.message(f"Enabled {name}")


@command(name="extensions.reload")
async def reload_extension() -> None:
    """Evaluate an extension's code again, in place.  What it registered
    before and no longer registers is removed."""
    name = await _ask_extension("Reload extension: ", ("loaded", "failed", "restart"))
    extension = await ed.extensions.reload(name)
    if extension.state == "restart":
        ed.message(f"{name} cannot be reloaded: {extension.error}. Restart to take up changes.",
                   kind="warning")  # fmt: skip
    elif extension.state != "loaded":
        raise NeomacsError(f"{name} could not be reloaded: {extension.error}")
    else:
        ed.message(f"Reloaded {name}")
