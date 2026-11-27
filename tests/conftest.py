from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("NEOMACS_CHECK_OWNER", "1")

from neomacs.testing import Client, running_engine  # noqa: E402


@pytest.fixture(scope="module")
def engine_path() -> Iterator[Path]:
    with running_engine() as path:
        yield path


@pytest.fixture
def client(engine_path: Path) -> Iterator[Client]:
    with Client(engine_path) as c:
        yield c
