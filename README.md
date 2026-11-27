# Neomacs

A Python-native editor. The engine is a live Python process that owns all
editor state; frontends connect to it over JSON-RPC and draw what it sends.
Despite the name it shares no code with Emacs.

Status: early. See `doc/IMPLEMENTATION.md` for what exists and what is next,
and the other files in `doc/` for the design.

## Development

Requires free-threaded CPython 3.15 or newer.

```
python3.15t -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Run the engine and the terminal frontend:

```
.venv/bin/python -m neomacs --tui FILE
```
