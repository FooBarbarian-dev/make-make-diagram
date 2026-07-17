from __future__ import annotations

from pipeview.model import Edge, Node, Report

_EDGE_STYLE = {
    "prerequisite": "solid",
    "order_only": "dashed",
    "needs": "bold",
    "stage_order": "dotted",
    "includes": "solid",
    "invokes": "bold",
    "extends": "dashed",
}

_EDGE_LABEL = {
    "prerequisite": "",
    "order_only": "order-only",
    "needs": "needs",
    "stage_order": "stage",
    "includes": "includes",
    "invokes": "invokes",
    "extends": "extends",
}

_NODE_SHAPE_DOT = {
    "target": "box",
    "pattern_rule": "hexagon",
    "job": "box",
    "stage": "ellipse",
    "ghost": "box",
}

_NODE_SHAPE_MMD = {
    "target": ("[", "]"),
    "pattern_rule": ("{{", "}}"),
    "job": ("[", "]"),
    "stage": ("([", "])"),
    "ghost": ("[", "]"),
}


def export_json(report: Report, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(report.to_json(indent=2))


def export_dot(report: Report, path: str) -> None:
    lines = ["digraph pipeline {", '  rankdir=LR;', '  node [fontname="sans-serif"];']

    for node in report.nodes:
        shape = _NODE_SHAPE_DOT.get(node.kind, "box")
        style = "dashed" if node.kind == "ghost" else "solid"
        label = _escape_dot(node.name)
        lines.append(
            f'  "{_escape_dot(node.id)}" '
            f'[label="{label}", shape={shape}, style={style}];'
        )

    for edge in report.edges:
        style = _EDGE_STYLE.get(edge.kind, "solid")
        label = _EDGE_LABEL.get(edge.kind, "")
        label_attr = f', label="{label}"' if label else ""
        lines.append(
            f'  "{_escape_dot(edge.src)}" -> "{_escape_dot(edge.dst)}" '
            f"[style={style}{label_attr}];"
        )

    lines.append("}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def export_mermaid(report: Report, path: str) -> None:
    lines = ["flowchart LR"]

    for node in report.nodes:
        open_br, close_br = _NODE_SHAPE_MMD.get(node.kind, ("[", "]"))
        name = _escape_mmd(node.name)
        nid = _mmd_id(node.id)
        if node.kind == "ghost":
            lines.append(f"  {nid}{open_br}{name}{close_br}")
            lines.append(f"  style {nid} stroke-dasharray: 5 5")
        else:
            lines.append(f"  {nid}{open_br}{name}{close_br}")

    for edge in report.edges:
        label = _EDGE_LABEL.get(edge.kind, "")
        src = _mmd_id(edge.src)
        dst = _mmd_id(edge.dst)
        if edge.kind in ("order_only", "extends"):
            arrow = f"-. {label} .->" if label else "-..->"
        elif edge.kind in ("stage_order",):
            arrow = f"-. {label} .->" if label else "-..->"
        else:
            arrow = f"-- {label} -->" if label else "-->"
        lines.append(f"  {src} {arrow} {dst}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def export_svg(report: Report, path: str) -> None:
    nodes = report.nodes
    edges = report.edges

    node_positions: dict[str, tuple[float, float]] = {}
    node_sizes: dict[str, tuple[float, float]] = {}

    col_width = 200
    row_height = 70
    margin_x = 40
    margin_y = 40
    node_w = 160
    node_h = 40

    adj: dict[str, set[str]] = {n.id: set() for n in nodes}
    for e in edges:
        if e.src in adj:
            adj[e.src].add(e.dst)

    levels: dict[str, int] = {}
    _assign_levels(nodes, edges, levels)

    level_groups: dict[int, list[str]] = {}
    for nid, level in levels.items():
        level_groups.setdefault(level, []).append(nid)

    max_level = max(levels.values()) if levels else 0

    for level, nids in level_groups.items():
        for row, nid in enumerate(nids):
            x = margin_x + level * col_width
            y = margin_y + row * row_height
            node_positions[nid] = (x, y)
            node_sizes[nid] = (node_w, node_h)

    max_rows = max(len(g) for g in level_groups.values()) if level_groups else 1
    svg_w = margin_x * 2 + (max_level + 1) * col_width
    svg_h = margin_y * 2 + max_rows * row_height

    node_map = {n.id: n for n in nodes}
    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {svg_w} {svg_h}" '
        f'width="{svg_w}" height="{svg_h}" style="font-family:sans-serif;font-size:12px;">',
        '<defs><marker id="arrow" markerWidth="10" markerHeight="7" '
        'refX="10" refY="3.5" orient="auto">'
        '<polygon points="0 0, 10 3.5, 0 7" fill="#666"/></marker></defs>',
    ]

    for edge in edges:
        if edge.src in node_positions and edge.dst in node_positions:
            sx, sy = node_positions[edge.src]
            dx, dy = node_positions[edge.dst]
            sx2 = sx + node_w
            sy2 = sy + node_h / 2
            dx2 = dx
            dy2 = dy + node_h / 2
            style = _EDGE_STYLE.get(edge.kind, "solid")
            dash = 'stroke-dasharray="5,5"' if style in ("dashed", "dotted") else ""
            svg_lines.append(
                f'<line x1="{sx2}" y1="{sy2}" x2="{dx2}" y2="{dy2}" '
                f'stroke="#666" stroke-width="1.5" marker-end="url(#arrow)" {dash}/>'
            )

    for nid, (x, y) in node_positions.items():
        node = node_map.get(nid)
        if not node:
            continue
        fill = "#fff"
        stroke = "#333"
        dash = ""
        if node.kind == "ghost":
            dash = 'stroke-dasharray="5,5"'
            fill = "#f8f8f8"
        elif node.kind == "stage":
            fill = "#e8f0fe"
        elif "phony" in node.flags:
            fill = "#e8ffe8"
        svg_lines.append(
            f'<rect x="{x}" y="{y}" width="{node_w}" height="{node_h}" '
            f'rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.5" {dash}/>'
        )
        tx = x + node_w / 2
        ty = y + node_h / 2 + 4
        label = node.name[:20]
        svg_lines.append(
            f'<text x="{tx}" y="{ty}" text-anchor="middle" fill="#333">{_escape_svg(label)}</text>'
        )

    svg_lines.append("</svg>")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(svg_lines))


def _assign_levels(nodes: list[Node], edges: list[Edge], levels: dict[str, int]) -> None:
    node_ids = {n.id for n in nodes}
    children: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for e in edges:
        if e.src in children and e.dst in node_ids:
            children[e.src].add(e.dst)

    for nid in node_ids:
        if nid not in levels:
            _dfs_level(nid, children, levels, set())


def _dfs_level(
    nid: str, children: dict[str, set[str]], levels: dict[str, int], visiting: set[str]
) -> int:
    if nid in levels:
        return levels[nid]
    if nid in visiting:
        levels[nid] = 0
        return 0
    visiting.add(nid)
    child_max = -1
    for child in children.get(nid, []):
        child_max = max(child_max, _dfs_level(child, children, levels, visiting))
    levels[nid] = child_max + 1
    visiting.discard(nid)
    return levels[nid]


def _escape_dot(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _escape_mmd(s: str) -> str:
    return s.replace('"', "'").replace("[", "(").replace("]", ")")


def _mmd_id(s: str) -> str:
    for ch in ":/.-  ":
        s = s.replace(ch, "_")
    return s


def _escape_svg(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
