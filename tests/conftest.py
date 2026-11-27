from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("NEOMACS_CHECK_OWNER", "1")

# No engine a test starts, however it starts it, loads the configuration of
# whoever runs the tests or writes among their files.  (`running_engine` gives
# each engine directories of its own besides.)
_HOME = tempfile.mkdtemp(prefix="neomacs-tests-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
for _var, _name in (
    ("NEOMACS_CONFIG_DIR", "config"),
    ("NEOMACS_DATA_DIR", "data"),
    ("NEOMACS_CACHE_DIR", "cache"),
):
    os.environ[_var] = os.path.join(_HOME, _name)

from neomacs.testing import Client, running_engine  # noqa: E402


@pytest.fixture(scope="module")
def engine_path() -> Iterator[Path]:
    with running_engine() as path:
        yield path


@pytest.fixture
def client(engine_path: Path) -> Iterator[Client]:
    with Client(engine_path) as c:
        yield c
