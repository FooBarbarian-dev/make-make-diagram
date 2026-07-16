from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


SCHEMA_VERSION = 1


@dataclass
class SourceLocation:
    file: str
    line: int

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line}

    @classmethod
    def from_dict(cls, d: dict) -> SourceLocation:
        return cls(file=d["file"], line=d["line"])


@dataclass
class VariableEvent:
    source: SourceLocation
    operator: str
    scope: str
    raw_value: str
    resolved_value: str | None = None

    def to_dict(self) -> dict:
        return {
            "source": self.source.to_dict(),
            "operator": self.operator,
            "scope": self.scope,
            "raw_value": self.raw_value,
            "resolved_value": self.resolved_value,
        }

    @classmethod
    def from_dict(cls, d: dict) -> VariableEvent:
        return cls(
            source=SourceLocation.from_dict(d["source"]),
            operator=d["operator"],
            scope=d["scope"],
            raw_value=d["raw_value"],
            resolved_value=d.get("resolved_value"),
        )


@dataclass
class Variable:
    name: str
    events: list[VariableEvent] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "events": [e.to_dict() for e in self.events],
            "used_by": self.used_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Variable:
        return cls(
            name=d["name"],
            events=[VariableEvent.from_dict(e) for e in d["events"]],
            used_by=d.get("used_by", []),
        )


NodeKind = Literal["target", "pattern_rule", "job", "stage", "ghost"]

EdgeKind = Literal[
    "prerequisite",
    "order_only",
    "needs",
    "stage_order",
    "includes",
    "invokes",
    "extends",
]

FileKind = Literal["makefile", "gitlab_yaml"]
FileStatus = Literal["ok", "error", "unresolved"]
Severity = Literal["info", "warning", "error"]


@dataclass
class Node:
    id: str
    name: str
    kind: NodeKind
    source: SourceLocation | None = None
    recipe: list[str] = field(default_factory=list)
    doc: str | None = None
    flags: set[str] = field(default_factory=set)
    annotations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "source": self.source.to_dict() if self.source else None,
            "recipe": self.recipe,
            "doc": self.doc,
            "flags": sorted(self.flags),
            "annotations": self.annotations,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Node:
        return cls(
            id=d["id"],
            name=d["name"],
            kind=d["kind"],
            source=SourceLocation.from_dict(d["source"]) if d.get("source") else None,
            recipe=d.get("recipe", []),
            doc=d.get("doc"),
            flags=set(d.get("flags", [])),
            annotations=d.get("annotations", {}),
        )


@dataclass
class Edge:
    src: str
    dst: str
    kind: EdgeKind

    def to_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst, "kind": self.kind}

    @classmethod
    def from_dict(cls, d: dict) -> Edge:
        return cls(src=d["src"], dst=d["dst"], kind=d["kind"])


@dataclass
class SourceFile:
    path: str
    kind: FileKind
    status: FileStatus = "ok"

    def to_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "status": self.status}

    @classmethod
    def from_dict(cls, d: dict) -> SourceFile:
        return cls(path=d["path"], kind=d["kind"], status=d.get("status", "ok"))


@dataclass
class Diagnostic:
    severity: Severity
    message: str
    source: SourceLocation | None = None
    related_node: str | None = None

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "message": self.message,
            "source": self.source.to_dict() if self.source else None,
            "related_node": self.related_node,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Diagnostic:
        return cls(
            severity=d["severity"],
            message=d["message"],
            source=SourceLocation.from_dict(d["source"]) if d.get("source") else None,
            related_node=d.get("related_node"),
        )


@dataclass
class Report:
    schema_version: int = SCHEMA_VERSION
    root: str = ""
    format: str = ""
    generated_at: str = ""
    tool_version: str = ""
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    variables: list[Variable] = field(default_factory=list)
    files: list[SourceFile] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    default_goal: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "root": self.root,
            "format": self.format,
            "generated_at": self.generated_at,
            "tool_version": self.tool_version,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "variables": [v.to_dict() for v in self.variables],
            "files": [f.to_dict() for f in self.files],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "default_goal": self.default_goal,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            root=d.get("root", ""),
            format=d.get("format", ""),
            generated_at=d.get("generated_at", ""),
            tool_version=d.get("tool_version", ""),
            nodes=[Node.from_dict(n) for n in d.get("nodes", [])],
            edges=[Edge.from_dict(e) for e in d.get("edges", [])],
            variables=[Variable.from_dict(v) for v in d.get("variables", [])],
            files=[SourceFile.from_dict(f) for f in d.get("files", [])],
            diagnostics=[Diagnostic.from_dict(diag) for diag in d.get("diagnostics", [])],
            default_goal=d.get("default_goal"),
        )

    @classmethod
    def from_json(cls, s: str) -> Report:
        return cls.from_dict(json.loads(s))

    def node_by_id(self, node_id: str) -> Node | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def max_severity(self) -> Severity | None:
        dominated = {"info": 0, "warning": 1, "error": 2}
        best: Severity | None = None
        for d in self.diagnostics:
            if best is None or dominated[d.severity] > dominated[best]:
                best = d.severity
        return best
