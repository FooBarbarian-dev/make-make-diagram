"""Mermaid escaping helpers, shared by the graph exports and the
trigger-docs renderer."""

from __future__ import annotations


def escape_label(s: str) -> str:
    """Make a string safe as a mermaid node label: quotes become
    apostrophes (labels are emitted inside double quotes or bare), and
    square brackets — which close a bare node — become parens."""
    return s.replace('"', "'").replace("[", "(").replace("]", ")")


def node_id(s: str) -> str:
    """Flatten an arbitrary node id into mermaid's identifier alphabet.
    Anything outside [0-9A-Za-z_] would break the parser — Make ids like
    `$(OBJS)` or `%.o` included — so it all becomes `_`."""
    return "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_"
                   for ch in s)
