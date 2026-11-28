"""Dispatch (ENGINE 7.1, 8.1): keys to commands, command tasks, the lane, cancellation."""

from __future__ import annotations

import _thread
import asyncio
import inspect
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neomacs import commands as commands_module
from neomacs import keys as keys_module
from neomacs.commands import Ask, Command, KeyName, KeyText, Prefix, PrefixArg, RawPrefix
from neomacs.errors import NeomacsError, Quit, RpcError
from neomacs.hooks import post_command, pre_command
from neomacs.keymap import Keymap, global_map
from neomacs.modes import Mode
from neomacs.text import undo

if TYPE_CHECKING:
    from neomacs.editor import Editor
    from neomacs.window import Frame, View

INTERRUPT_FALLBACK = 0.2  # seconds before a command that never awaits is interrupted
SWALLOW_QUIT = 0.3  # a C-g key this soon after a cancel belongs to that cancel


class Dispatcher:
    """Input from every frame, handled in order; foreground commands one at a time."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self.lane = asyncio.Lock()
        self._queue: (
            asyncio.Queue[tuple[int, Frame, str, Any, asyncio.Future[Any] | None]] | None
        ) = None
        self._epoch = 0  # bumped by a cancel: input queued before it is discarded
        # What commands that gave up the lane are waiting for.
        self._answers: set[asyncio.Future[Any]] = set()
        self._holder: asyncio.Task[Any] | None = None  # the task that holds the lane
        self._running = 0  # commands begun and not finished, including ones waiting for input
        self._taking = False
        self._swallow_until = 0.0
        # Fallback interrupt state, shared with the reader thread.
        self._interrupt_seq = 0
        self._interrupt_handled = 0
        self.want_interrupt = False
        self.worker: asyncio.Task[None] | None = None
        # Set while the debugger is handing the editor to the other loop:
        # input is set aside for that loop instead of being started here.
        self.parked = False
        self._set_aside: list[tuple[int, Frame, str, Any, asyncio.Future[Any] | None]] = []

    def start(self) -> None:
        loop = self.editor.loop
        assert loop is not None
        self.lane = asyncio.Lock()
        self._queue = asyncio.Queue()
        self.worker = loop.create_task(self._worker())
        self.editor.keep_task(self.worker)

    def put(self, frame: Frame, kind: str, value: Any) -> None:
        if self._queue is not None:
            self._queue.put_nowait((self._epoch, frame, kind, value, None))

    def take_pending(self) -> list[Any]:
        """Remove and return the input that has not been started (the debugger's hand-over)."""
        items: list[Any] = list(self._set_aside)
        self._set_aside = []
        while self._queue is not None and not self._queue.empty():
            items.append(self._queue.get_nowait())
        return items

    def give(self, items: list[Any]) -> None:
        """Queue input that another dispatcher set aside."""
        if self._queue is not None:
            for _, frame, kind, value, future in items:
                self._queue.put_nowait((self._epoch, frame, kind, value, future))

    @property
    def busy(self) -> bool:
        queued = self._queue is not None and not self._queue.empty() and not self.parked
        return queued or self._taking or self._holder is not None

    # -- the lane -----------------------------------------------------------

    async def wait[T](self, future: asyncio.Future[T]) -> T:
        """Await the user's answer, giving up the lane meanwhile.

        The keys that answer can then run their own commands.  Once the answer
        has arrived, the waiting command takes the lane back ahead of any input
        typed after it, so keys still apply in the order they were typed.
        """
        task = asyncio.current_task()
        held = task is not None and self._holder is task
        if held:
            self._holder = None
            self._answers.add(future)
            self.lane.release()
        try:
            return await future
        finally:
            if held:
                await self.lane.acquire()
                self._holder = task
                self._answers.discard(future)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            epoch, frame, kind, value, future = await self._queue.get()
            if self.parked:
                self._set_aside.append((epoch, frame, kind, value, future))
                continue
            self._taking = True
            try:
                await self.lane.acquire()
                while any(answer.done() for answer in self._answers):
                    # A command has its answer and is about to resume: it goes first.
                    self.lane.release()
                    await asyncio.sleep(0)
                    await self.lane.acquire()
                started = False
                try:
                    if self.parked:
                        # The command ahead of this input ended the pause: the
                        # input belongs to whichever loop serves next.
                        self._set_aside.append((epoch, frame, kind, value, future))
                    elif epoch != self._epoch:
                        # Typed ahead of a command that was then cancelled.
                        if future is not None and not future.done():
                            future.set_exception(Quit("cancelled"))
                    else:
                        started = await self._begin(frame, kind, value, future)
                except Exception as exc:
                    self.editor.report_exception(f"handling {kind} {value!r}", exc)
                    if future is not None and not future.done():
                        future.set_exception(exc)
                if not started:
                    self.lane.release()
                    self.editor.update_frames()
            finally:
                self._taking = False

    async def _begin(
        self, frame: Frame, kind: str, value: Any, future: asyncio.Future[Any] | None
    ) -> bool:
        """Turn one input event into a command.  True if a task now holds the lane."""
        if frame.id not in self.editor.frames:
            return False
        if kind in ("mouse", "drop"):
            frame.last_event = value
        if kind == "key":
            key = str(value)
            if key == "C-g" and time.monotonic() < self._swallow_until:
                return False
            if frame.key_waiters:
                frame.key_waiters.pop(0).set_result(key)
                return False
            found = self._resolve(frame, key)
            if found is None:
                return False
            command, extend = found
            return self._launch(frame, command, key=key, extend=extend)
        if kind == "mouse":
            return await self._begin(frame, "key", value["key"], None)
        names = {"text": "insert_text", "paste": "paste", "drop": "drop"}
        if kind in names:
            command = commands_module.lookup(names[kind])
            if command is None:
                return False
            args = (value,) if kind == "text" else (value["text"],) if kind == "paste" else ()
            return self._launch(frame, command, args=args)
        if kind == "call":
            name, args = value
            command = commands_module.lookup(name)
            if command is None:
                raise RpcError(f"no such command: {name}")
            return self._launch(frame, command, args=tuple(args), future=future)
        return False

    def _launch(
        self,
        frame: Frame,
        command: Command,
        *,
        key: str | None = None,
        args: tuple[Any, ...] = (),
        extend: bool = False,
        future: asyncio.Future[Any] | None = None,
    ) -> bool:
        loop = self.editor.loop
        assert loop is not None
        coro = self._run(frame, command, key, args, extend, future, not command.background)
        if command.background:
            self.editor.start_task(coro, name=command.name, frame=frame)
            return False
        task = loop.create_task(coro)
        self._holder = task
        self.editor.keep_task(task)
        return True

    # -- keys ---------------------------------------------------------------

    def active_keymaps(self, frame: Frame, view: View | None = None) -> list[Keymap]:
        """The keymaps consulted for a key, in order (ENGINE 7.2)."""
        view = view or frame.focus
        buffer = view.buffer
        maps: list[Keymap] = []
        if frame.transient is not None:
            maps.append(frame.transient)
        pos = view.cursor.pos
        spans = [s for layer in buffer.layers for s in layer.at(pos) if s.get("keymap")]
        spans.sort(key=lambda s: -s.priority)
        maps.extend(s.get("keymap") for s in spans if isinstance(s.get("keymap"), Keymap))
        for mode in reversed([*self.editor.global_modes, *buffer.minor_modes]):
            if isinstance(mode, Mode):
                maps.extend(type(mode).keymaps())
        debug_keys = self.editor.debugger.keymap
        if debug_keys is not None and self.editor.debugger.status:
            maps.append(debug_keys)  # stepping keys, in every buffer while paused
        if isinstance(buffer.keymap, Keymap):
            maps.append(buffer.keymap)
        if isinstance(buffer.mode, Mode):
            maps.extend(type(buffer.mode).keymaps())
        maps.append(global_map)
        return maps

    def _lookup(self, maps: list[Keymap], keys: list[str]) -> str | Keymap | None:
        for keymap in maps:
            found = keymap.lookup(keys)
            if found is not None:
                return found
        return None

    def _resolve(self, frame: Frame, key: str) -> tuple[Command, bool] | None:
        """Feed KEY to the frame's pending sequence.  A command when it completes one."""
        frame.pending_keys.append(key)
        pending = frame.pending_keys
        maps = self.active_keymaps(frame)
        frame.transient = None
        binding = self._lookup(maps, pending)
        extend = False
        if binding is None and len(pending) == 1:
            mods, base = keys_module.split(key)
            if "S" in mods and len(base) > 1:
                # A shifted motion key with no binding of its own extends the selection.
                plain = "".join(f"{m}-" for m in mods if m != "S") + base
                binding = self._lookup(maps, [plain])
                extend = isinstance(binding, str)
            if binding is None and keys_module.text_of(key) is not None:
                binding = next((m.default for m in maps if m.default), None)
        if isinstance(binding, Keymap):
            frame.show_keys(keys_module.describe(pending) + "-", delay=True)
            return None
        described = keys_module.describe(pending)
        frame.pending_keys = []
        frame.clear_keys()
        if binding is None:
            frame.prefix = None
            if "mouse-" not in key:  # an unbound mouse event is not worth a message
                self.editor.message(f"{described} is undefined", frame=frame)
            return None
        command = commands_module.lookup(binding)
        if command is None:
            frame.prefix = None
            self.editor.message(f"{described} runs {binding}, which is not defined", frame=frame)
            return None
        return command, extend

    # -- running ------------------------------------------------------------

    async def _run(
        self,
        frame: Frame,
        command: Command,
        key: str | None,
        args: tuple[Any, ...],
        extend: bool,
        future: asyncio.Future[Any] | None,
        foreground: bool,
    ) -> None:
        self._running += 1
        try:
            result = await self.invoke(frame, command, key=key, args=args, extend=extend)
            if future is not None and not future.done():
                future.set_result(result)
        except BaseException as exc:
            if future is not None and not future.done():
                future.set_exception(exc if isinstance(exc, Exception) else Quit("cancelled"))
            if not isinstance(exc, Exception):
                raise
        finally:
            self._running -= 1
            task = asyncio.current_task()
            if foreground and self._holder is task:
                self._holder = None
                self.lane.release()
            self.editor.update_frames()

    async def invoke(
        self,
        frame: Frame,
        command: Command,
        *,
        key: str | None = None,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        extend: bool = False,
    ) -> Any:
        """Run COMMAND for FRAME: context, arguments, hooks, undo, selections."""
        from neomacs import editor as editor_module

        editor = self.editor
        view = frame.focus
        buffer = view.buffer
        prefix = frame.prefix
        if not command.prefix:
            # Taken by this command.  Left in place it would also apply to the
            # keys that answer a prompt the command opens.
            frame.prefix = None
        last = frame.last_command
        # Typing is undone a run of characters at a time, not one by one.
        invocation = undo.new_invocation()
        if (
            command.amalgamate
            and last is not None
            and last[0] == command.name
            and frame.typing_run[0] == (view.id, buffer.tick)
            and frame.typing_run[1] < int(editor.options.typing_undo_limit)
        ):
            invocation = frame.typing_run[2]
            frame.typing_run = (frame.typing_run[0], frame.typing_run[1] + 1, invocation)
        else:
            frame.typing_run = ((view.id, buffer.tick), 1, invocation)
        tokens = (
            undo.invocation.set(invocation),
            editor_module.frame_var.set(frame),
            editor_module.pins_var.set((view, frame.window, buffer)),
        )
        before = (view.selections.ranges(), view.selections.primary_index)
        tick = buffer.tick
        was_extending = view.selections.extending
        if extend:
            view.selections.extending = True
        result: Any = None
        try:
            await pre_command.arun(command)
            # Positional arguments go to the parameters after the selection, by name.
            plain = [p.name for p in command.params if not p.is_selection]
            if len(args) > len(plain):
                raise NeomacsError(f"{command.name} takes at most {len(plain)} arguments")
            resolved = dict(zip(plain, args, strict=False))
            resolved.update(kwargs or {})
            resolved.update(await self._arguments(command, frame, prefix, key, (), resolved))
            if command.per_selection:
                name = next(p.name for p in command.params if p.is_selection)
                for selection in list(view.selections):
                    if selection.head.buffer is not view.buffer:
                        break
                    result = command.fn(**{name: selection, **resolved})
                    if inspect.isawaitable(result):
                        result = await result
            else:
                result = command.fn(**resolved)
                if inspect.isawaitable(result):
                    result = await result
            await post_command.arun(command)
        except Quit:
            self._quit(frame)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            self._quit(frame)
        except NeomacsError as exc:
            editor.message(str(exc), kind="error", frame=frame)
        except Exception as exc:
            editor.report_exception(f"in {command.name}", exc, frame=frame, keep=True)
        finally:
            for token, var in zip(
                tokens,
                (undo.invocation, editor_module.frame_var, editor_module.pins_var),
                strict=True,
            ):
                var.reset(token)  # pyright: ignore[reportArgumentType]
            if extend and view.selections.extending:
                view.selections.extending = was_extending
            self._finish(frame, command, view, before, tick, invocation, args)
        return result

    def _quit(self, frame: Frame) -> None:
        frame.prefix = None
        frame.pending_keys = []
        frame.transient = None
        self.editor.message("Quit", frame=frame)

    def _finish(
        self,
        frame: Frame,
        command: Command,
        view: View,
        before: tuple[list[tuple[int, int]], int],
        tick: int,
        invocation: int,
        args: tuple[Any, ...],
    ) -> None:
        if view.id in frame.views:
            buffer = view.buffer
            view.selections.normalize()
            if buffer.tick != tick:
                group = buffer.undo.last
                if group is not None and group.invocation == invocation:
                    if group.before is None:
                        group.before = before
                    group.after = (view.selections.ranges(), view.selections.primary_index)
                frame.typing_run = (
                    (view.id, buffer.tick),
                    frame.typing_run[1],
                    frame.typing_run[2],
                )
        if not command.prefix:
            frame.prefix = None
            frame.clear_keys()
            frame.last_command = (command.name, args)
        focused = frame.focus
        if focused.id in frame.views:
            focused.reveal()

    async def _arguments(
        self,
        command: Command,
        frame: Frame,
        prefix: PrefixArg | None,
        key: str | None,
        args: tuple[Any, ...],
        given: dict[str, Any],
    ) -> dict[str, Any]:
        """Values for the parameters that were not supplied (ENGINE 7.1)."""
        values: dict[str, Any] = {}
        positional = [p for p in command.params if not p.is_selection]
        for index, param in enumerate(positional):
            if index < len(args) or param.name in given:
                continue
            spec = param.spec
            if isinstance(spec, Prefix):
                values[param.name] = prefix.value if prefix is not None else spec.default
            elif spec is RawPrefix:
                values[param.name] = prefix
            elif spec is KeyText:
                values[param.name] = keys_module.text_of(key or "") or ""
            elif spec is KeyName:
                values[param.name] = key or ""
            elif isinstance(spec, Ask):
                values[param.name] = await self._ask(spec, param.convert)
            elif param.required:
                raise NeomacsError(f"{command.name} needs a value for {param.name}")
        return values

    async def _ask(self, ask: Ask, convert: Any) -> Any:
        from neomacs import completion

        completer = {
            "file": completion.files_completer,
            "buffer": completion.buffers_completer,
            "command": completion.commands_completer,
        }.get(ask.kind)
        if ask.kind == "choice":
            completer = completion.choices(ask.choices)
        initial = ask.initial
        if ask.kind == "file" and not initial:
            # A file name starts where the project is, not where the engine
            # happened to be started (WORKSPACE 4.3).
            frame = self.editor.frame
            root = frame.workspace.root if frame is not None else None
            if root is not None:
                home = str(Path.home())
                initial = str(root) + "/"
                if initial.startswith(home + "/"):
                    initial = "~" + initial[len(home) :]
        text = await self.editor.minibuffer.read(
            ask.prompt,
            completer=completer,
            history=ask.history,
            initial=initial,
            submit_selects=ask.kind in ("buffer", "command", "choice"),
        )
        if ask.kind == "file" and not text.strip():
            raise NeomacsError("No file name given")
        if ask.kind == "file" or convert is Path:
            return Path(text).expanduser()
        if ask.kind == "integer" or convert is int:
            try:
                return int(text)
            except ValueError:
                raise NeomacsError(f"not a number: {text}") from None
        return text

    async def call(self, frame: Frame, name: str, args: list[Any] | tuple[Any, ...] = ()) -> Any:
        """Run the command NAME as if a key had invoked it, and return its result.

        From inside a running command it runs at once, as part of that command.
        Otherwise it takes its turn in the lane behind any input already queued.
        """
        command = commands_module.lookup(name)
        if command is None:
            raise RpcError(f"no such command: {name}")
        task = asyncio.current_task()
        if task is not None and self._holder is task:
            return await self.invoke(frame, command, args=tuple(args))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        assert self._queue is not None
        self._queue.put_nowait((self._epoch, frame, "call", (name, list(args)), future))
        return await future

    # -- cancelling (ENGINE 3) ----------------------------------------------

    def cancel_foreground(self) -> bool:
        """Cancel the foreground command, if one holds the lane.  True if it did."""
        task = self._holder
        if task is None or task.done():
            return False
        task.cancel()
        self._swallow_until = time.monotonic() + SWALLOW_QUIT
        self._epoch += 1  # whatever was typed ahead of the cancel is dropped
        return True

    def interrupt_from_thread(self) -> None:
        """Called on a reader thread when `input.interrupt` arrives."""
        loop = self.editor.loop
        if loop is None:
            return
        self._interrupt_seq += 1
        seq = self._interrupt_seq
        try:
            loop.call_soon_threadsafe(self._interrupt_on_loop, seq)
        except RuntimeError:
            return
        timer = threading.Timer(INTERRUPT_FALLBACK, self._interrupt_fallback, [seq])
        timer.daemon = True
        timer.start()

    def _interrupt_on_loop(self, seq: int) -> None:
        self._interrupt_handled = seq
        self.cancel_foreground()

    def _interrupt_fallback(self, seq: int) -> None:
        # The loop has not picked up the request: something is computing without
        # awaiting.  Raise in the main thread; the handler there decides whether
        # that something is a command (Editor._on_sigint).
        if self._interrupt_handled < seq and self._interrupt_seq == seq:
            self.want_interrupt = True
            _thread.interrupt_main()

    def in_foreground_command(self) -> bool:
        """True if the code now running on the loop thread is the lane's holder."""
        try:
            return self._holder is not None and asyncio.current_task() is self._holder
        except RuntimeError:
            return False
