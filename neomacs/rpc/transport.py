"""Byte transports: newline-delimited messages over a socket or stdio.

Each connection has a reader thread and a writer thread, so the engine's
event loop never blocks on I/O and an interrupt can be seen while the loop
is busy (ENGINE 3, 4.8).
"""

from __future__ import annotations

import contextlib
import os
import queue
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path

QUEUE_LIMIT = 16 * 1024 * 1024  # bytes queued for a frontend before we give up on it


class Transport:
    """One connection.  Callbacks run on the reader thread."""

    def __init__(
        self,
        read_line: Callable[[], bytes],
        write: Callable[[bytes], None],
        close_io: Callable[[], None],
        *,
        name: str,
    ) -> None:
        self.name = name
        self._read_line = read_line
        self._write = write
        self._close_io = close_io
        self._out: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._queued = 0
        self._lock = threading.Lock()
        self._closed = False
        self._writer_thread: threading.Thread | None = None
        self.on_line: Callable[[bytes], None] = lambda line: None
        self.on_close: Callable[[], None] = lambda: None

    def start(self) -> None:
        threading.Thread(target=self._reader, name=f"{self.name}-reader", daemon=True).start()
        self._writer_thread = threading.Thread(
            target=self._writer, name=f"{self.name}-writer", daemon=True
        )
        self._writer_thread.start()

    def send(self, data: bytes) -> None:
        """Queue DATA (one message, without its newline).  Never blocks."""
        with self._lock:
            if self._closed:
                return
            self._queued += len(data)
            over = self._queued > QUEUE_LIMIT
        if over:
            self.close()
            return
        self._out.put(data)

    @property
    def queued_bytes(self) -> int:
        return self._queued

    def close(self, drain: float = 0.0) -> None:
        """Close the connection.  With DRAIN, first give queued output that many
        seconds to be written, so a reply sent just before shutdown is not lost."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._out.put(None)
        writer = self._writer_thread
        if drain > 0 and writer is not None and writer is not threading.current_thread():
            writer.join(drain)
        with contextlib.suppress(OSError):
            self._close_io()

    def _reader(self) -> None:
        try:
            while True:
                line = self._read_line()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if line:
                    self.on_line(line)
        except OSError, ValueError:
            pass
        finally:
            # Do not close here: replies to lines already read may still be
            # on their way.  The owner closes once it has handled them.
            self.on_close()

    def _writer(self) -> None:
        while True:
            data = self._out.get()
            if data is None:
                return
            try:
                self._write(data + b"\n")
            except OSError, ValueError:
                self.close()
                return
            with self._lock:
                self._queued -= len(data)


def socket_transport(sock: socket.socket, name: str) -> Transport:
    sock.setblocking(True)
    reader = sock.makefile("rb")

    def close_io() -> None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        sock.close()

    return Transport(reader.readline, sock.sendall, close_io, name=name)


def stdio_transport() -> Transport:
    # Read and write the file descriptors directly.  A daemon thread blocked
    # inside sys.stdin's buffered reader makes interpreter shutdown abort.
    in_fd = sys.stdin.fileno()
    out_fd = sys.stdout.fileno()
    buffer = bytearray()

    def read_line() -> bytes:
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                line = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                return line
            chunk = os.read(in_fd, 65536)
            if not chunk:
                rest = bytes(buffer)
                buffer.clear()
                return rest
            buffer.extend(chunk)

    def write(data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(out_fd, view)
            view = view[written:]

    return Transport(read_line, write, lambda: None, name="stdio")


class Listener:
    """Accepts connections on a Unix socket; ON_ACCEPT runs on the accept thread."""

    def __init__(self, path: Path, on_accept: Callable[[socket.socket], None]) -> None:
        self.path = path
        self._on_accept = on_accept
        self._stop = False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            self._sock.bind(str(path))
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept, name="listener", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._on_accept(conn)

    def close(self) -> None:
        self._stop = True
        with contextlib.suppress(OSError):
            self._sock.close()
        with contextlib.suppress(OSError):
            self.path.unlink()
