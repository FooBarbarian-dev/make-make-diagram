"""Mermaid escaping helpers, shared by the graph exports and the
trigger-docs renderer."""

from __future__ import annotations


def escape_label(s: str) -> str:
    """Make a string safe as a mermaid node label: quotes become
    apostrophes (labels are emitted inside double quotes or bare), and
    square brackets — which close a bare node — become parens."""
    return s.replace('"', "'").replace("[", "(").replace("]", ")")


def node_id(s: str) -> str:
    """Flatten an arbitrary node id into mermaid's identifier alphabet."""
    for ch in ":/.-  ":
        s = s.replace(ch, "_")
    return s
