from __future__ import annotations

import os
from pathlib import Path

from pipeview.model import Report

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_VENDOR_DIR = Path(__file__).parent.parent / "vendor"


def render_html(report: Report, output_path: str) -> None:
    template_path = _TEMPLATE_DIR / "report.html"
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    dagre_path = _VENDOR_DIR / "dagre.min.js"
    dagre_js = ""
    if dagre_path.is_file():
        with open(dagre_path, "r", encoding="utf-8") as f:
            dagre_js = f.read()

    whatif_path = _TEMPLATE_DIR / "whatif.js"
    whatif_js = ""
    if whatif_path.is_file():
        with open(whatif_path, "r", encoding="utf-8") as f:
            whatif_js = f.read()

    model_json = report.to_json()

    html = template.replace("/*DAGRE_PLACEHOLDER*/", dagre_js)
    html = html.replace("/*WHATIF_PLACEHOLDER*/", whatif_js)
    html = html.replace("/*MODEL_JSON_PLACEHOLDER*/{}", model_json)
    html = html.replace("{{ROOT}}", _escape_html(report.root))
    html = html.replace("{{GENERATED_AT}}", _escape_html(report.generated_at))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
