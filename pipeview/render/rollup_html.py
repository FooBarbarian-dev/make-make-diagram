"""Render a rollup document (pipeview.gitlab.rollup) into one offline
HTML file, the same way html.py renders a single report."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeview.render.html import script_safe_json

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_VENDOR_DIR = Path(__file__).parent.parent / "vendor"


def render_rollup_html(rollup: dict, output_path: str) -> None:
    template_path = _TEMPLATE_DIR / "rollup.html"
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    dagre_path = _VENDOR_DIR / "dagre.min.js"
    dagre_js = ""
    if dagre_path.is_file():
        with open(dagre_path, "r", encoding="utf-8") as f:
            dagre_js = f.read()

    html = template.replace("/*DAGRE_PLACEHOLDER*/", dagre_js)
    html = html.replace("{{HOST}}", _escape_html(str(rollup.get("host") or "")))
    html = html.replace("{{GENERATED_AT}}",
                        _escape_html(str(rollup.get("generated_at") or "")))
    # Rollup JSON is spliced last so payload text can never collide with a
    # pending placeholder.
    html = html.replace(
        "/*ROLLUP_JSON_PLACEHOLDER*/{}", script_safe_json(json.dumps(rollup))
    )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
