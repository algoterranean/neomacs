"""Command line entry point: start an engine."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import neomacs
from neomacs import paths
from neomacs.editor import Editor


def _watch_parent(editor: Editor) -> None:
    """Stop the engine when the process that started it goes away."""
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(1.0)
            if os.getppid() != parent:
                try:
                    editor.call_from_thread(editor.stop)
                except RuntimeError, AssertionError:
                    os._exit(0)
                return

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _tui(attach: str | None, file: str | None) -> int:
    """Run the terminal frontend, on its own engine unless told to attach to one."""
    from neomacs.frontends import tui

    if attach is not None:
        tui.run(attach, file)
        return 0
    path = paths.socket_path(f"tui-{os.getpid()}")
    engine = subprocess.Popen(
        [sys.executable, "-m", "neomacs", "--listen", str(path), "--exit-with-parent"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert engine.stdout is not None
        if not engine.stdout.readline().startswith("NEOMACS-LISTENING"):
            print("neomacs: the engine did not start", file=sys.stderr)
            return 1
        tui.run(path, file)
    finally:
        engine.terminate()
        try:
            engine.wait(timeout=5)
        except subprocess.TimeoutExpired:
            engine.kill()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neomacs", description="The Neomacs engine.")
    parser.add_argument("--version", action="store_true", help="print versions and exit")
    parser.add_argument("--listen", metavar="PATH", help="listen on a Unix socket at PATH")
    parser.add_argument("--stdio", action="store_true", help="serve one client on stdio")
    parser.add_argument(
        "--exit-with-parent",
        action="store_true",
        help="exit when the parent process does",
    )
    parser.add_argument("--tui", action="store_true", help="run the terminal frontend")
    parser.add_argument(
        "--attach", metavar="PATH", help="with --tui: attach to the engine listening at PATH"
    )
    parser.add_argument("file", nargs="?", help="with --tui: a file to visit")
    args = parser.parse_args(argv)

    if args.version:
        gil = getattr(sys, "_is_gil_enabled", lambda: True)()
        from neomacs.rpc.api import PROTOCOL

        print(f"neomacs {neomacs.__version__}")
        print(f"protocol {PROTOCOL}")
        print(f"python {sys.version.split()[0]} (GIL {'enabled' if gil else 'disabled'})")
        return 0

    if args.tui:
        return _tui(args.attach, args.file)

    listen: Path | None = None
    if args.listen:
        listen = Path(args.listen)
        paths.check_socket_path(listen)
    elif not args.stdio:
        listen = paths.socket_path()

    editor = Editor()
    if args.exit_with_parent:
        _watch_parent(editor)

    def ready() -> None:
        if listen is not None and not args.stdio:
            # A parent process waits for this line before connecting.
            print(f"NEOMACS-LISTENING {listen}", flush=True)

    try:
        editor.run(listen=listen, stdio=args.stdio, ready=ready)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
