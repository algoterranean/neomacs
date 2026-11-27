from __future__ import annotations

from neomacs.stability import info, provisional, stable
from neomacs.tools import manifest


def test_committed_manifest_is_current() -> None:
    assert manifest.main(["--check"]) == 0, "run: python -m neomacs.tools.manifest --write"


def test_markers() -> None:
    @stable("1.0")
    def f(a: int, b: str = "x") -> None: ...

    @provisional("0.1")
    class C:
        @provisional("0.2")
        def m(self) -> None: ...

        @property
        @stable("1.0")
        def p(self) -> int:
            return 1

        def internal(self) -> None: ...

    class D(C):
        pass

    assert info(f) == {"tier": "stable", "since": "1.0"}
    assert info(C) == {"tier": "provisional", "since": "0.1"}
    assert info(D) is None  # a marker is not inherited
    assert info(vars(C)["m"]) == {"tier": "provisional", "since": "0.2"}
    assert info(vars(C)["p"]) == {"tier": "stable", "since": "1.0"}
    assert info(vars(C)["internal"]) is None


def test_manifest_contents() -> None:
    names = {e["name"]: e for e in manifest.collect()["names"]}
    assert names["neomacs.text.buffer.Buffer"]["kind"] == "class"
    replace = names["neomacs.text.buffer.Buffer.replace"]
    assert replace["kind"] == "method" and "start" in replace["signature"]
    assert names["neomacs.text.marker.Cursor.line"]["kind"] == "property"
    assert all(e["tier"] in ("stable", "provisional") for e in names.values())


def test_a_changed_signature_is_detected(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "manifest.json"
    assert manifest.main(["--write", "--path", str(path)]) == 0
    assert manifest.main(["--check", "--path", str(path)]) == 0
    from neomacs.text.buffer import Buffer

    def insert(self: Buffer, pos: int, text: str, extra: int = 0) -> None: ...

    insert.__neomacs_api__ = {"tier": "provisional", "since": "0.1"}  # type: ignore[attr-defined]
    monkeypatch.setattr(Buffer, "insert", insert)
    assert manifest.main(["--check", "--path", str(path)]) == 1
