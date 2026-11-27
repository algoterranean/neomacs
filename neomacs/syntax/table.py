"""The syntax table: a small declarative description every mode has."""

from __future__ import annotations

from dataclasses import dataclass

from neomacs.stability import provisional


@provisional("0.1")
@dataclass(frozen=True)
class Syntax:
    word_chars: str = "_"  # word characters besides Unicode letters and digits
    symbol_chars: str = ""  # further characters allowed in an identifier, e.g. "-"
    brackets: tuple[tuple[str, str], ...] = (("(", ")"), ("[", "]"), ("{", "}"))
    quotes: tuple[str, ...] = ('"', "'")  # string delimiters, longest first
    escape: str | None = "\\"
    line_comment: str | None = None  # e.g. "#"
    block_comment: tuple[str, str] | None = None  # e.g. ("/*", "*/")
    comment_padding: str = " "
    auto_pairs: tuple[tuple[str, str], ...] | None = None  # default: brackets and quotes

    @provisional("0.1")
    def is_word(self, ch: str) -> bool:
        """True if the code point CH is part of a word."""
        return ch.isalnum() or ch in self.word_chars

    @provisional("0.1")
    def is_symbol(self, ch: str) -> bool:
        """True if CH can be part of an identifier."""
        return ch.isalnum() or ch in self.word_chars or ch in self.symbol_chars

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        """The pairs to close automatically."""
        if self.auto_pairs is not None:
            return self.auto_pairs
        return (*self.brackets, *((q, q) for q in self.quotes))


DEFAULT = Syntax()
