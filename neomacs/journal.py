"""The recovery journal (ENGINE 11.1): unsaved text, kept on disk in case the engine dies.

Each engine has a directory of its own under the data directory, holding one
file for each buffer with unsaved changes: the buffer's whole text, written
again a moment after it changes.  An entry goes when its buffer is saved or
killed.  Entries still there when an engine
starts, in directories no running engine holds, are offered back.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from neomacs.hooks import ui_attached
from neomacs.options import Option
from neomacs.stability import provisional
from neomacs.text.buffer import Buffer, after_change, after_save, buffer_killed

if TYPE_CHECKING:
    from neomacs.editor import Editor

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

FORMAT = 1
LOCK = "lock"
_ID = "_journal_id"

Option("journal", bool, True, "Keep unsaved text on disk in case the engine dies.")
Option(
    "journal_delay",
    float,
    1.0,
    "Seconds after a change before the buffer's text is written to the journal.",
    validate=lambda n: n >= 0,
)
Option(
    "journal_limit",
    int,
    50_000_000,
    "Buffers longer than this many characters are not kept in the journal.",
    validate=lambda n: n > 0,
)


@provisional("0.1")
@dataclass(frozen=True)
class Entry:
    """Unsaved text left by an engine that is no longer running."""

    path: Path  # the journal file
    name: str  # the buffer's name
    file: Path | None  # the file the buffer was visiting
    time: float  # when the text was written, in seconds since the epoch
    text: str
    # Whether the file has changed on disk since the text was written.
    file_changed: bool = False


def _stat_key(file: Path | None) -> list[int] | None:
    if file is None:
        return None
    try:
        st = file.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _write(path: Path, record: dict[str, Any]) -> None:
    """Write one entry.  Runs in a worker thread, so it touches nothing of the editor's."""
    file = record.get("file")
    record["stat"] = _stat_key(Path(file) if file else None)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        json.dump(record, out)  # ASCII, so text that is not valid Unicode survives
    os.replace(tmp, path)


def _read(path: Path) -> Entry | None:
    try:
        data: Any = json.loads(path.read_text(encoding="ascii"))
        record = cast(dict[str, Any], data)
        if record.get("format") != FORMAT:
            return None
        file = Path(record["file"]) if record.get("file") else None
        return Entry(
            path,
            str(record["name"]),
            file,
            float(record.get("time", 0.0)),
            str(record["text"]),
            file is not None and _stat_key(file) != record.get("stat"),
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


def _held(directory: Path) -> bool:
    """Whether a running engine holds DIRECTORY."""
    lock = directory / LOCK
    if fcntl is None:
        # No file locks here: go by whether the process named in the file is alive.
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
        except OSError, ValueError:
            return False
        return True
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # which also lets go of the lock, if it was got
    return False


@provisional("0.1")
class Journal:
    """One engine's journal."""

    def __init__(self, editor: Editor, root: Path) -> None:
        self.editor = editor
        self.root = root
        self.directory = root / f"{os.getpid()}-{time.time_ns() // 1_000_000}"
        self._lock_fd: int | None = None
        self._ids = itertools.count(1)
        self._changed: set[Buffer] = set()
        self._timer: Any = None
        self._flushing = False
        self._cost = 0.0  # seconds the last round took of the main loop
        self._offered = False
        self._too_long: set[str] = set()

    @property
    def pending(self) -> bool:
        """Whether a change is still waiting to be written."""
        return bool(self._changed) or self._flushing or self._timer is not None

    # -- which buffers --------------------------------------------------------

    def wanted(self, buffer: Buffer) -> bool:
        """Whether BUFFER's text should be in the journal now."""
        editor = self.editor
        if not editor.options.journal or not buffer.modified:
            return False
        if editor.buffers.get(buffer.name) is not buffer or buffer.locals.get("no_journal"):
            return False
        # Buffers the editor writes itself are named like *this*; the scratch
        # buffer is the one of them that holds the user's own text.
        if buffer.file is None and buffer.name.startswith("*") and buffer.name != "*scratch*":
            return False
        if len(buffer) > int(editor.options.journal_limit):
            if buffer.name not in self._too_long:
                self._too_long.add(buffer.name)
                editor.message(
                    f"{buffer.name} is too long for the recovery journal (journal_limit)",
                    kind="warning",
                )
            return False
        return True

    def _path(self, buffer: Buffer) -> Path:
        ident = buffer.locals.get(_ID)
        if ident is None:
            ident = buffer.locals[_ID] = next(self._ids)
        return self.directory / f"{ident}.json"

    # -- writing --------------------------------------------------------------

    def note(self, buffer: Buffer) -> None:
        """BUFFER changed: write it out soon."""
        if buffer.file is None and buffer.name.startswith("*") and buffer.name != "*scratch*":
            return  # the common case of a buffer that is never kept, settled cheaply
        self._changed.add(buffer)
        if self._timer is None and not self._flushing:
            # A long buffer costs the main loop something each time its text is
            # taken, so the longer that took, the less often it is done.
            delay = max(float(self.editor.options.journal_delay), 50 * self._cost)
            self._timer = self.editor.timers.add("after", delay, self.flush, __name__)

    def _open(self) -> None:
        """Make this engine's directory, and hold it."""
        if self._lock_fd is not None:
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.directory / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        os.write(fd, str(os.getpid()).encode())
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._lock_fd = fd

    def _take(self) -> tuple[list[tuple[Path, dict[str, Any]]], list[Path]]:
        """What to write and what to remove, for the buffers that have changed."""
        writes: list[tuple[Path, dict[str, Any]]] = []
        removes: list[Path] = []
        changed, self._changed = self._changed, set()
        for buffer in changed:
            if self.wanted(buffer):
                record = {
                    "format": FORMAT,
                    "name": buffer.name,
                    "file": str(buffer.file) if buffer.file is not None else None,
                    "time": time.time(),
                    "text": buffer.store.snapshot(),
                }
                writes.append((self._path(buffer), record))
            elif _ID in buffer.locals:
                removes.append(self._path(buffer))
        return writes, removes

    @staticmethod
    def _apply(writes: list[tuple[Path, dict[str, Any]]], removes: list[Path]) -> None:
        for path, record in writes:
            _write(path, record)
        for path in removes:
            with contextlib.suppress(OSError):
                path.unlink()

    async def flush(self) -> None:
        """Write the buffers that have changed.  The text is taken here, on the
        owner thread, and written in a worker thread."""
        self._timer = None
        if self._flushing:
            return
        self._flushing = True
        try:
            began = time.perf_counter()
            writes, removes = self._take()
            if writes:
                self._open()
            self._cost = time.perf_counter() - began
            if writes or removes:
                await self.editor.in_thread(self._apply, writes, removes)
        except OSError as exc:
            self.editor.note_once(f"The recovery journal could not be written: {exc}")
        finally:
            self._flushing = False
        if self._changed:
            self.note(next(iter(self._changed)))  # changed again meanwhile

    def forget(self, buffer: Buffer) -> None:
        """BUFFER has nothing unsaved any more: its entry goes at once."""
        self._changed.discard(buffer)
        if _ID in buffer.locals:
            with contextlib.suppress(OSError):
                self._path(buffer).unlink()

    def close(self) -> None:
        """The engine is stopping: write what is waiting, and let go of the directory.
        Unsaved text stays in the journal, to be offered back next time."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        with contextlib.suppress(OSError):
            writes, removes = self._take()
            if writes:
                self._open()
            self._apply(writes, removes)
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            entries = [p for p in self.directory.iterdir() if p.suffix == ".json"]
            if not entries:
                shutil.rmtree(self.directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            os.close(fd)

    # -- what earlier engines left --------------------------------------------

    @provisional("0.1")
    def left_behind(self) -> list[Entry]:
        """Unsaved text from engines that are no longer running, oldest first."""
        entries: list[Entry] = []
        try:
            directories = [d for d in self.root.iterdir() if d.is_dir()]
        except OSError:
            return entries
        for directory in directories:
            if directory == self.directory or _held(directory):
                continue
            found = [e for p in sorted(directory.glob("*.json")) if (e := _read(p)) is not None]
            if not found:
                shutil.rmtree(directory, ignore_errors=True)  # nothing in it worth keeping
            entries.extend(found)
        return sorted(entries, key=lambda e: e.time)

    @provisional("0.1")
    def discard(self, entry: Entry) -> None:
        """Remove ENTRY from the journal."""
        with contextlib.suppress(OSError):
            entry.path.unlink()

    @provisional("0.1")
    def recover(self, entry: Entry) -> Buffer:
        """Put ENTRY's text in a buffer and take it out of the journal.

        The buffer is the one visiting the entry's file, or one of the
        entry's name.  It is left unsaved, and undoing gives back what it
        held before.
        """
        if not entry.path.exists():
            raise FileNotFoundError(f"{entry.name} is no longer in the journal")
        buffers = self.editor.buffers
        if entry.file is not None:
            buffer = buffers.visit(entry.file)
        else:
            buffer = buffers.get(entry.name) if entry.name == "*scratch*" else None
            if buffer is None:
                buffer = buffers.new(entry.name)
        if buffer.text != entry.text:
            buffer.replace(0, len(buffer), entry.text)
        self.discard(entry)
        return buffer


def _journal() -> Journal | None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    return editor.journal if editor is not None else None


def _changed(buffer: Buffer, start: int, old_end: int, new_end: int) -> None:
    journal = _journal()
    if journal is not None:
        journal.note(buffer)


def _settled(buffer: Buffer) -> None:
    journal = _journal()
    if journal is not None:
        journal.forget(buffer)


def _offer(frame: Any) -> None:
    """Tell the first frame to attach that there is unsaved text to be had back."""
    editor: Editor = frame.editor
    journal = editor.journal
    if journal is None or journal._offered:  # pyright: ignore[reportPrivateUsage]
        return
    journal._offered = True  # pyright: ignore[reportPrivateUsage]
    count = len(journal.left_behind())
    if count:
        what = "a buffer" if count == 1 else f"{count} buffers"
        editor.message(
            f"Unsaved text from {what} was kept when the editor last stopped: M-x recover",
            kind="warning",
            frame=frame,
        )


after_change.add(_changed)
after_save.add(_settled)
buffer_killed.add(_settled)
ui_attached.add(_offer)
