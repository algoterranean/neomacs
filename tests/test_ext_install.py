"""Step 12, X2: the manifest, the lock file and the extensions environment."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from extkit import config, dirs, extension, library, needs_uv

from neomacs.ext import lock as lock_module
from neomacs.ext import manifest as manifest_module
from neomacs.ext.environment import Environment, ExtensionError
from neomacs.ext.lock import Lock, Locked
from neomacs.ext.manifest import ManifestError, Wanted

# -- extensions.toml ------------------------------------------------------------------


def test_the_manifest_is_read(tmp_path: Path) -> None:
    path = tmp_path / "extensions.toml"
    path.write_text(
        "[extensions]\n"
        'neomacs-git = ">=0.3"\n'
        'Neomacs_Any = "*"\n'
        'neomacs-rust = { git = "https://example.org/neomacs-rust.git", rev = "v0.2.1" }\n'
        'my-tools = { path = "~/src/my-tools", editable = true }\n'
        "\n[resolver]\n"
        'index = "https://example.org/simple"\nfind-links = "~/wheels"\noffline = true\n'
    )
    manifest = manifest_module.read(path)
    assert manifest.wanted == {
        "neomacs-git": Wanted("neomacs-git", ">=0.3"),
        "neomacs-any": Wanted("Neomacs_Any"),
        "neomacs-rust": Wanted(
            "neomacs-rust", git="https://example.org/neomacs-rust.git", rev="v0.2.1"
        ),
        "my-tools": Wanted("my-tools", path="~/src/my-tools", editable=True),
    }
    assert manifest.resolver.index == "https://example.org/simple"
    assert manifest.resolver.find_links == ["~/wheels"] and manifest.resolver.offline is True
    assert manifest_module.read(tmp_path / "absent.toml").wanted == {}


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("[extensions\n", "extensions.toml"),
        ('[extensions]\n"not a name!" = "*"\n', "is not a distribution name"),
        ("[extensions]\na = 3\n", "expected a version or a table"),
        ('[extensions]\na = { git = "x", path = "y" }\n', "not both"),
        ("[extensions]\na = { editable = true }\n", "give one of"),
        ('[extensions]\na = { git = "x", branch = "y" }\n', "unknown keys"),
        ('[extensions]\na-b = "*"\na_b = "*"\n', "are the same distribution"),
        ("[resolver]\nmirror = 1\n", "unknown keys in [resolver]"),
    ],
)
def test_a_manifest_that_cannot_be_acted_on_is_refused(
    tmp_path: Path, text: str, complaint: str
) -> None:
    path = tmp_path / "extensions.toml"
    path.write_text(text)
    with pytest.raises(ManifestError, match=complaint.replace("[", r"\[").replace("]", r"\]")):
        manifest_module.read(path)


def test_what_the_user_types_to_add_an_extension(tmp_path: Path) -> None:
    parse = manifest_module.parse_request
    assert parse("neomacs-git") == Wanted("neomacs-git")
    assert parse(" neomacs-git >=0.3,<1 ") == Wanted("neomacs-git", ">=0.3,<1")
    assert parse("neomacs-git==0.3.1") == Wanted("neomacs-git", "==0.3.1")
    assert parse("neomacs-rust @ git+https://example.org/r.git@v0.2.1") == Wanted(
        "neomacs-rust", git="https://example.org/r.git", rev="v0.2.1"
    )
    assert parse("x @ git+https://example.org/r.git") == Wanted(
        "x", git="https://example.org/r.git"
    )
    assert parse("x @ git+ssh://git@example.org/r.git") == Wanted(
        "x", git="ssh://git@example.org/r.git"
    )
    assert parse("my-tools @ ~/src/my-tools") == Wanted("my-tools", path="~/src/my-tools")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "from-its-project"\n')
    assert parse(str(project)) == Wanted("from-its-project", path=str(project))
    with pytest.raises(ManifestError, match="no pyproject.toml naming a project"):
        parse(str(tmp_path))
    with pytest.raises(ManifestError, match="no extension named"):
        parse("  ")


def test_editing_the_manifest_keeps_what_the_user_wrote(tmp_path: Path) -> None:
    path = tmp_path / "extensions.toml"
    path.write_text(
        "# My extensions.\n"
        "[extensions]\n"
        'neomacs-git = ">=0.3"   # the one I use most\n'
        "\n"
        "[resolver]\n"
        "offline = true  # on the train\n"
    )
    manifest_module.edit(path, add=Wanted("neomacs-rust", git="https://example.org/r.git"))
    manifest_module.edit(path, add=Wanted("Neomacs_Git", ">=0.4"))  # the same one: replaced
    assert path.read_text() == (
        "# My extensions.\n"
        "[extensions]\n"
        'neomacs-rust = { git = "https://example.org/r.git" }\n'
        'Neomacs_Git = ">=0.4"\n'
        "\n"
        "[resolver]\n"
        "offline = true  # on the train\n"
    )
    manifest_module.edit(path, remove="neomacs_rust")
    assert "rust" not in path.read_text() and "# My extensions." in path.read_text()
    fresh = tmp_path / "new" / "extensions.toml"
    manifest_module.edit(fresh, add=Wanted("a"))
    assert fresh.read_text() == '[extensions]\na = "*"\n'
    manifest_module.edit(fresh, remove="absent")
    assert fresh.read_text() == '[extensions]\na = "*"\n'


# -- extensions.lock ---------------------------------------------------------------------

UV_LOCK = """\
version = 1
requires-python = "==3.15.*"

[[package]]
name = "neomacs"
version = "0.0.1"
source = { path = "../provided/neomacs-0.0.1-py3-none-any.whl" }

[[package]]
name = "neomacs-extensions-environment"
version = "0"
source = { virtual = "." }

[[package]]
name = "pure"
version = "1.2"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/pure-1.2.tar.gz", hash = "sha256:aa" }
wheels = [
    { url = "https://files.example/pure-1.2-py3-none-any.whl", hash = "sha256:bb" },
]

[[package]]
name = "compiled-for-both"
version = "3.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.example/compiled_for_both-3.0-cp315-cp315-macosx_11_0_arm64.whl", hash = "sha256:cc" },
    { url = "https://files.example/compiled_for_both-3.0-cp315-cp315t-macosx_11_0_arm64.whl", hash = "sha256:dd" },
]

[[package]]
name = "compiled-with-gil-only"
version = "4.0"
source = { registry = "https://pypi.org/simple" }
wheels = [
    { url = "https://files.example/compiled_with_gil_only-4.0-cp315-cp315-macosx_11_0_arm64.whl", hash = "sha256:ee" },
    { url = "https://files.example/compiled_with_gil_only-4.0-cp38-abi3-macosx_11_0_arm64.whl", hash = "sha256:ff" },
]

[[package]]
name = "source-only"
version = "5.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.example/source-only-5.0.tar.gz", hash = "sha256:11" }

[[package]]
name = "from-git"
version = "0.2.1"
source = { git = "https://example.org/r.git?rev=v0.2.1#0123456789abcdef0123456789abcdef01234567" }

[[package]]
name = "in-place"
version = "0.1"
source = { editable = "../../src/in-place" }

[[package]]
name = "a-directory"
version = "0.1"
source = { directory = "../../src/a-directory" }
"""


def test_the_lock_is_made_from_what_the_resolver_decided(tmp_path: Path) -> None:
    project = tmp_path / "ext-env" / "resolve"
    project.mkdir(parents=True)
    lock = lock_module.from_uv(UV_LOCK, project, {"neomacs"})
    d = lock.distributions
    assert sorted(d) == [
        "a-directory", "compiled-for-both", "compiled-with-gil-only", "from-git", "in-place",
        "neomacs", "pure", "source-only",
    ]  # fmt: skip
    assert d["neomacs"] == Locked("neomacs", "0.0.1", "engine")
    assert d["pure"] == Locked(
        "pure", "1.2", "registry", "https://pypi.org/simple", "", ("sha256:aa", "sha256:bb"), True
    )
    assert d["from-git"] == Locked(
        "from-git",
        "0.2.1",
        "git",
        "https://example.org/r.git",
        "0123456789abcdef0123456789abcdef01234567",
    )
    assert d["in-place"].source == "editable"
    assert d["in-place"].location == str((tmp_path / "src" / "in-place").resolve())
    assert d["a-directory"].source == "directory" and d["a-directory"].wheel is None
    if lock_module.free_threaded():
        # What matters on a free-threaded interpreter is a wheel it can use as it is.
        assert d["compiled-for-both"].wheel is True
        assert d["compiled-with-gil-only"].wheel is False
        assert d["source-only"].wheel is False


def test_which_wheels_a_free_threaded_interpreter_can_use() -> None:
    usable = lock_module.wheel_usable_without_gil
    assert usable("pure-1.2-py3-none-any.whl")
    assert usable("x-1.0-cp315-cp315t-macosx_11_0_arm64.whl")
    assert usable("x-1.0-1-cp315-cp315t-manylinux_2_28_x86_64.whl")  # with a build number
    assert usable("x-1.0-cp315-abi3.abi3t-macosx_11_0_arm64.whl")
    assert not usable("x-1.0-cp315-cp315-macosx_11_0_arm64.whl")
    assert not usable("x-1.0-cp38-abi3-macosx_11_0_arm64.whl")
    assert not usable("not-a-wheel.whl")


def test_the_lock_file_round_trips_and_says_what_to_install(tmp_path: Path) -> None:
    project = tmp_path / "ext-env" / "resolve"
    project.mkdir(parents=True)
    lock = lock_module.from_uv(UV_LOCK, project, {"neomacs"})
    path = tmp_path / "extensions.lock"
    lock.write(path)
    assert lock_module.read(path) == lock
    assert lock_module.read(tmp_path / "absent.lock") is None
    stub = tmp_path / "neomacs-0.0.1-py3-none-any.whl"
    text, where = lock_module.requirements(lock, {"neomacs": stub})
    assert text.split("\n") == [
        f"a-directory @ {(tmp_path / 'src' / 'a-directory').resolve().as_uri()}",
        "compiled-for-both==3.0 --hash=sha256:cc --hash=sha256:dd",
        "compiled-with-gil-only==4.0 --hash=sha256:ee --hash=sha256:ff",
        "from-git @ git+https://example.org/r.git@0123456789abcdef0123456789abcdef01234567",
        f"-e {(tmp_path / 'src' / 'in-place').resolve()}",
        f"neomacs @ {stub.as_uri()}",
        "pure==1.2 --hash=sha256:aa --hash=sha256:bb",
        "source-only==5.0 --hash=sha256:11",
        "",
    ]
    assert where == ["--index-url", "https://pypi.org/simple"]
    # A lock made for another engine cannot be installed for this one.
    with pytest.raises(lock_module.LockError, match="neomacs ext upgrade"):
        lock_module.requirements(lock, {})
    path.write_text("format = 99\n")
    with pytest.raises(lock_module.LockError, match="format 99"):
        lock_module.read(path)


def test_a_plan_says_what_would_change() -> None:
    old = Lock(
        {
            "neomacs": Locked("neomacs", "0.0.1", "engine"),
            "stays": Locked("stays", "1.0"),
            "moves": Locked("moves", "1.0"),
            "goes": Locked("goes", "1.0"),
        }
    )
    new = Lock(
        {
            "neomacs": Locked("neomacs", "0.0.2", "engine"),
            "stays": Locked("stays", "1.0"),
            "moves": Locked("moves", "2.0"),
            "comes": Locked("comes", "0.5", wheel=False),
            "from-git": Locked(
                "from-git", "1", "git", "https://example.org/r.git", "0123456789abcdef"
            ),
        }
    )
    plan = lock_module.plan(old, new)
    assert [d.name for d in plan.added] == ["comes", "from-git"]
    assert [(a.version, b.version) for a, b in plan.changed] == [("1.0", "2.0")]
    assert [d.name for d in plan.removed] == ["goes"]
    assert [d.name for d in plan.without_wheel] == ["comes"]
    text = plan.describe()
    assert "  + comes 0.5\n" in text
    assert "  + from-git 1 from https://example.org/r.git at 0123456789ab\n" in text
    assert "  ~ moves 1.0  ->  moves 2.0\n" in text and "  - goes 1.0\n" in text
    assert "these are built from source, and may turn the GIL on:\n  ! comes 0.5\n" in text
    assert "neomacs" not in text  # the engine's own package is not something being installed
    assert lock_module.plan(new, new).empty
    assert lock_module.plan(new, new).describe() == "Nothing to change.\n"
    assert [d.name for d in lock_module.plan(None, old).added] == ["goes", "moves", "stays"]


# -- the environment, with uv --------------------------------------------------------------


def make(tmp_path: Path) -> tuple[Environment, Path]:
    wheels = tmp_path / "wheels"
    library(wheels, "shared", "1.0")
    library(wheels, "shared", "2.0")
    extension(wheels, "a", requires=("neomacs>=0.0.1,<1", "shared>=1,<3"))
    extension(wheels, "b", requires=("shared>=2",))
    extension(wheels, "c", requires=("shared<2",))
    extension(wheels, "future", requires=("neomacs>=99",))
    config(tmp_path / "config", wheels)
    env = Environment(config=tmp_path / "config", data=tmp_path / "data", cache=tmp_path / "cache")
    return env, wheels


async def add(env: Environment, request: str) -> lock_module.Plan:
    wanted = manifest_module.parse_request(request)
    plan = await env.plan(env.with_change(add=wanted))
    await env.apply(plan, add=wanted)
    return plan


def installed(env: Environment) -> list[str]:
    site = env.site_packages()
    assert site is not None
    return sorted(p.name for p in site.iterdir() if p.name.endswith(".dist-info"))


@needs_uv
def test_two_extensions_with_a_shared_dependency_install_and_a_third_that_conflicts_is_refused(
    tmp_path: Path,
) -> None:
    env, _ = make(tmp_path)

    async def scenario() -> None:
        first = await add(env, "neomacs-a")
        assert [d.what for d in first.added] == ["neomacs-a 0.1", "shared 2.0"]
        second = await add(env, "neomacs-b")
        assert [d.what for d in second.added] == ["neomacs-b 0.1"]  # the shared one is there
        assert installed(env) == [
            "neomacs-0.0.1.dist-info", "neomacs_a-0.1.dist-info", "neomacs_b-0.1.dist-info",
            "shared-2.0.dist-info",
        ]  # fmt: skip
        before = (env.manifest_path.read_text(), env.lock_path.read_text())
        with pytest.raises(ExtensionError) as refused:
            await env.plan(env.with_change(add=manifest_module.parse_request("neomacs-c")))
        message = str(refused.value)
        assert message.startswith("These extensions cannot be installed together.")
        # Both requirements are named, with who makes each.
        assert "neomacs-b==0.1 depends on shared>=2" in message
        assert "neomacs-c==0.1 depends on shared<2" in message
        assert "extensions.toml asks for neomacs-b and neomacs-c" in message
        # And nothing was changed by asking.
        assert (env.manifest_path.read_text(), env.lock_path.read_text()) == before
        assert "neomacs_c" not in " ".join(installed(env))

    asyncio.run(scenario())
    # What was installed is the engine's interpreter's, and is not the engine.
    site = env.site_packages()
    assert site is not None and (site / "neomacs_a" / "__init__.py").is_file()
    assert not (site / "neomacs").exists()  # only a record that the engine provides it


@needs_uv
def test_an_extension_that_needs_another_engine_is_refused(tmp_path: Path) -> None:
    env, _ = make(tmp_path)
    with pytest.raises(ExtensionError, match=r"neomacs-future==0.1 depends on neomacs>=99"):
        asyncio.run(env.plan(env.with_change(add=manifest_module.parse_request("neomacs-future"))))
    with pytest.raises(ExtensionError, match="is part of the engine"):
        asyncio.run(env.plan(env.with_change(add=manifest_module.parse_request("neomacs"))))
    with pytest.raises(ExtensionError, match="neomacs-nowhere was not found"):
        asyncio.run(env.plan(env.with_change(add=manifest_module.parse_request("neomacs-nowhere"))))


@needs_uv
def test_removing_upgrading_and_building_again_from_the_two_files(tmp_path: Path) -> None:
    env, wheels = make(tmp_path)

    async def scenario() -> None:
        await add(env, "neomacs-a")
        await add(env, "neomacs-b")
        # A newer version appears.  Nothing moves until an upgrade is asked for.
        extension(wheels, "a", "0.2", requires=("shared>=1,<3",))
        library(wheels, "shared", "2.1")
        assert (await env.plan()).empty
        only = await env.plan(upgrade=["neomacs-a"])
        assert [(o.what, n.what) for o, n in only.changed] == [("neomacs-a 0.1", "neomacs-a 0.2")]
        everything = await env.plan(upgrade=True)
        assert [n.what for _, n in everything.changed] == ["neomacs-a 0.2", "shared 2.1"]
        await env.apply(everything)
        assert "neomacs_a-0.2.dist-info" in installed(env) and "shared-2.1.dist-info" in installed(
            env
        )
        # Removing b lets go of nothing a still needs.
        gone = await env.plan(env.with_change(remove="neomacs-b"))
        assert [d.name for d in gone.removed] == ["neomacs-b"]
        await env.apply(gone, remove="neomacs-b")
        assert installed(env) == [
            "neomacs-0.0.1.dist-info", "neomacs_a-0.2.dist-info", "shared-2.1.dist-info",
        ]  # fmt: skip
        with pytest.raises(ExtensionError, match="is not in extensions.toml"):
            env.with_change(remove="neomacs-b")

    asyncio.run(scenario())
    # Another machine: the two files and nothing else.
    elsewhere = Environment(
        config=tmp_path / "config", data=tmp_path / "data2", cache=tmp_path / "cache2"
    )
    assert elsewhere.site_packages() is None
    asyncio.run(elsewhere.sync())
    assert installed(elsewhere) == installed(env)


@needs_uv
def test_a_file_that_is_not_the_one_locked_is_not_installed(tmp_path: Path) -> None:
    env, wheels = make(tmp_path)
    asyncio.run(add(env, "neomacs-a"))
    # The same name and version, other contents: as when an index is tampered with.
    library(wheels, "shared", "2.0", requires=("something-else",))
    elsewhere = Environment(
        config=tmp_path / "config", data=tmp_path / "data2", cache=tmp_path / "cache2"
    )
    with pytest.raises(ExtensionError, match="(?i)hash"):
        asyncio.run(elsewhere.sync())


def test_without_uv_the_reason_is_said(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Environment(config=tmp_path / "c", data=tmp_path / "d", cache=tmp_path / "k")
    monkeypatch.setenv("NEOMACS_UV", str(tmp_path / "no-such-uv"))
    with pytest.raises(ExtensionError, match="uv is needed to install extensions"):
        asyncio.run(env.plan())


# -- neomacs ext ---------------------------------------------------------------------------------


def ext(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "neomacs", "ext", *arguments],
        capture_output=True, text=True, timeout=120, env={**os.environ, **dirs(root)},
    )  # fmt: skip


@needs_uv
def test_the_command_line(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    library(wheels, "shared", "1.0")
    library(wheels, "shared", "2.0")
    extension(wheels, "a", requires=("shared>=1,<3",),
              declarations='[autoload]\ncommands = ["a.hello"]\n')  # fmt: skip
    extension(wheels, "b", requires=("shared>=2",))
    extension(wheels, "c", requires=("shared<2",))
    config(tmp_path / "config", wheels)

    assert ext(tmp_path, "list").stdout == "No extensions are installed.\n"
    # Not asked, and not on a terminal: nothing is installed.
    unasked = ext(tmp_path, "add", "neomacs-a")
    assert unasked.returncode == 1 and "  + neomacs-a 0.1\n" in unasked.stdout
    assert "--yes" in unasked.stderr and not (tmp_path / "config" / "extensions.lock").exists()

    done = ext(tmp_path, "add", "neomacs-a", "--yes")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "To be added:\n  + neomacs-a 0.1\n  + shared 2.0\nAdded neomacs-a.\n"
    assert ext(tmp_path, "add", "neomacs-b", "-y").returncode == 0
    listing = ext(tmp_path, "list").stdout.split("\n")
    assert listing[0].split() == [
        "a",
        "0.1",
        "loaded",
        "on",
        "first",
        "use",
        "The",
        "a",
        "extension",
    ]
    assert listing[1].split() == ["b", "0.1", "loaded", "at", "startup", "The", "b", "extension"]

    refused = ext(tmp_path, "add", "neomacs-c", "--yes")
    assert refused.returncode == 1 and refused.stdout == ""
    assert (
        "\nneomacs ext: These extensions cannot be installed together. Because " in refused.stderr
    )
    assert "depends on shared>=2" in refused.stderr and "depends on shared<2" in refused.stderr

    assert ext(tmp_path, "upgrade", "--yes").stdout == "Nothing to change.\nNothing newer.\n"
    assert ext(tmp_path, "sync").stdout == "The environment matches the lock file.\n"
    removed = ext(tmp_path, "remove", "neomacs-b", "--yes")
    assert removed.stdout == "To be removed:\n  - neomacs-b 0.1\nRemoved neomacs-b.\n"
    assert ext(tmp_path, "remove", "neomacs-b", "--yes").stderr == (
        "neomacs ext: neomacs-b is not in extensions.toml\n"
    )
    assert [line.split()[0] for line in ext(tmp_path, "list").stdout.split("\n")[:1]] == ["a"]
