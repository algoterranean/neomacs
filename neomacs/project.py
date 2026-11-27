"""Project settings (ENGINE 6, EXTENSIONS 8.1).

A directory with `.neomacs/settings.toml` in it is a project root.  The file
is data: it can give values to options that were declared with
`project=True`, and can do nothing else.  It is read without asking.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from neomacs import options as options_module
from neomacs.hooks import mode_enabled
from neomacs.safeio import read_regular
from neomacs.stability import provisional
from neomacs.text.buffer import Buffer, after_save, buffer_created

if TYPE_CHECKING:
    from neomacs.editor import Editor

DIRECTORY = ".neomacs"
FILE = "settings.toml"
MAX_SIZE = 1_000_000  # bytes of a settings file that are read


@provisional("0.1")
@dataclass
class Project:
    """One project's settings, as read from its file."""

    root: Path
    file: Path
    # Option name to value: for every buffer, and for buffers in each mode.
    general: dict[str, Any] = field(default_factory=dict[str, Any])
    by_mode: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    # What the file said that was not applied, each with the reason.
    skipped: list[str] = field(default_factory=list[str])
    error: str | None = None  # why the file could not be read at all

    def values_for(self, buffer: Buffer) -> dict[str, Any]:
        """The values that apply to BUFFER: the general section, then the
        sections for its mode's ancestors, then for its mode."""
        values = dict(self.general)
        mode = buffer.mode
        for cls in reversed(type(mode).__mro__) if mode is not None else ():
            name = vars(cls).get("name")
            if isinstance(name, str) and name in self.by_mode:
                values.update(self.by_mode[name])
        return values


def _options(table: Any, where: str, project: Project) -> dict[str, Any]:
    """The acceptable settings in one `options` table; the rest are noted as skipped."""
    accepted: dict[str, Any] = {}
    if not isinstance(table, dict):
        project.skipped.append(f"{where} (not a table)")
        return accepted
    for key, value in cast(dict[str, Any], table).items():
        name = options_module.resolve(key)
        option = options_module.options.get(name)
        if option is None:
            project.skipped.append(f"{where}.{key} (no such option)")
        elif not option.project:
            project.skipped.append(f"{where}.{key} (not an option a project may set)")
        else:
            try:
                accepted[name] = option.check(value)
            except (TypeError, ValueError) as exc:
                project.skipped.append(f"{where}.{key} ({exc})")
    return accepted


def read(root: Path) -> Project:
    """Read the settings file of the project at ROOT."""
    file = root / DIRECTORY / FILE
    project = Project(root, file)
    try:
        data = tomllib.loads(read_regular(file, MAX_SIZE).decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        project.error = str(exc)
        return project
    for key, value in data.items():
        if key == "options":
            project.general = _options(value, "options", project)
        elif key == "mode" and isinstance(value, dict):
            for mode, section in cast(dict[str, Any], value).items():
                if not isinstance(section, dict):
                    project.skipped.append(f"mode.{mode}")
                    continue
                for inner, table in cast(dict[str, Any], section).items():
                    if inner == "options":
                        found = _options(table, f"mode.{mode}.options", project)
                        project.by_mode[str(mode)] = found
                    else:
                        project.skipped.append(f"mode.{mode}.{inner}")
        else:
            project.skipped.append(str(key))
    return project


def root_for(file: Path) -> Path | None:
    """The nearest directory above FILE that is a project root."""
    for directory in file.parents:
        try:
            if (directory / DIRECTORY / FILE).is_file():
                return directory
        except OSError:
            return None
    return None


@provisional("0.1")
class Projects:
    """The projects whose settings an editor has read."""

    def __init__(self, editor: Editor) -> None:
        self.editor = editor
        self._loaded: dict[Path, Project] = {}

    @provisional("0.1")
    def of(self, buffer: Buffer) -> Project | None:
        """The project BUFFER's file is in, if it is in one that has been read."""
        if buffer.project_file is None:
            return None
        return self._loaded.get(buffer.project_file.parent.parent)

    def load(self, root: Path) -> Project:
        """Read ROOT's settings file, and say once what in it was not applied."""
        project = self._loaded[root] = read(root)
        if project.error is not None:
            self.editor.message(f"{project.file}: {project.error}", kind="error")
        elif project.skipped:
            self.editor.message(
                f"{project.file}: skipped {', '.join(project.skipped)}", kind="warning"
            )
        return project

    def apply(self, buffer: Buffer) -> None:
        """Give BUFFER the values of the project its file is in."""
        root = root_for(buffer.file) if buffer.file is not None else None
        project = None
        if root is not None:
            project = self._loaded.get(root) or self.load(root)
        values = project.values_for(buffer) if project is not None else {}
        file = project.file if project is not None else None
        if values == buffer.project_values and file == buffer.project_file:
            return
        buffer.project_values = values
        buffer.project_file = file
        buffer.invalidate(0, len(buffer))
        self.editor.update_frames()

    @provisional("0.1")
    def reload(self, root: Path | None = None) -> list[Project]:
        """Read the settings files again: ROOT's, or with no ROOT every one
        that a buffer is in, including ones made since the buffer was opened."""
        buffers = [b for b in self.editor.buffers if b.file is not None]
        if root is None:
            self._loaded.clear()
        else:
            self._loaded.pop(root, None)
        for buffer in buffers:
            self.apply(buffer)
        return list(self._loaded.values())


def _projects() -> Projects | None:
    from neomacs.editor import _current  # pyright: ignore[reportPrivateUsage]

    editor = _current.get(None)
    return editor.projects if editor is not None else None


def _created(buffer: Buffer) -> None:
    projects = _projects()
    if projects is not None and buffer.file is not None:
        projects.apply(buffer)


def _mode_changed(buffer: Buffer, mode: Any) -> None:
    projects = _projects()
    if projects is not None and buffer.project_file is not None and not mode.minor:
        projects.apply(buffer)  # another mode, so perhaps another section


def _saved(buffer: Buffer) -> None:
    projects = _projects()
    if projects is None or buffer.file is None:
        return
    file = buffer.file
    if file.name == FILE and file.parent.name == DIRECTORY:
        projects.reload(file.parent.parent)  # the settings themselves were edited
    else:
        projects.apply(buffer)  # saved under another name, perhaps in another project


buffer_created.add(_created)
mode_enabled.add(_mode_changed)
after_save.add(_saved)
