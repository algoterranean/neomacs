from __future__ import annotations

from pathlib import Path

from neomacs.testing import Client


def test_open_edit_undo_save(client: Client, tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("first\nsecond\n")
    client.eval(f"b = ed.buffers.visit({str(path)!r})")
    assert client.eval("b.line_count, b.line_text(2)") == "(3, 'second')"
    client.eval("b.insert(0, 'zeroth\\n')")
    client.eval("b.insert(len(b), 'third\\n')")
    assert client.eval("b.undo.undo() is not None") == "True"
    assert client.eval("b.text") == repr("zeroth\nfirst\nsecond\n")
    client.eval("b.save()")
    assert path.read_text() == "zeroth\nfirst\nsecond\n"
    assert client.eval("b.modified") == "False"
