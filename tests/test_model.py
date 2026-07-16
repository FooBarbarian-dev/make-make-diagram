import json
from pipeview.model import (
    SCHEMA_VERSION,
    Diagnostic,
    Edge,
    Node,
    Report,
    SourceFile,
    SourceLocation,
    Variable,
    VariableEvent,
)


def _sample_report() -> Report:
    return Report(
        schema_version=SCHEMA_VERSION,
        root="Makefile",
        format="makefile",
        generated_at="2025-01-01T00:00:00Z",
        tool_version="0.1.0",
        nodes=[
            Node(
                id="all",
                name="all",
                kind="target",
                source=SourceLocation(file="Makefile", line=1),
                recipe=["echo done"],
                doc="Build everything",
                flags={"phony", "default_goal"},
                annotations={"double_colon": False},
            ),
            Node(id="ghost:missing", name="missing", kind="ghost"),
        ],
        edges=[
            Edge(src="all", dst="ghost:missing", kind="prerequisite"),
        ],
        variables=[
            Variable(
                name="CC",
                events=[
                    VariableEvent(
                        source=SourceLocation(file="Makefile", line=3),
                        operator=":=",
                        scope="global",
                        raw_value="gcc",
                        resolved_value="gcc",
                    ),
                ],
                used_by=["all"],
            ),
        ],
        files=[
            SourceFile(path="Makefile", kind="makefile", status="ok"),
        ],
        diagnostics=[
            Diagnostic(
                severity="warning",
                message="Missing prerequisite: missing",
                source=SourceLocation(file="Makefile", line=1),
                related_node="ghost:missing",
            ),
        ],
        default_goal="all",
    )


class TestRoundTrip:
    def test_json_round_trip(self):
        report = _sample_report()
        js = report.to_json(indent=2)
        restored = Report.from_json(js)
        assert restored.to_json(indent=2) == js

    def test_dict_round_trip(self):
        report = _sample_report()
        d = report.to_dict()
        restored = Report.from_dict(d)
        assert restored.to_dict() == d

    def test_schema_version(self):
        report = _sample_report()
        d = report.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION

    def test_flags_sorted_in_dict(self):
        report = _sample_report()
        d = report.to_dict()
        flags = d["nodes"][0]["flags"]
        assert flags == sorted(flags)


class TestNodeLookup:
    def test_node_by_id_found(self):
        report = _sample_report()
        n = report.node_by_id("all")
        assert n is not None
        assert n.name == "all"

    def test_node_by_id_missing(self):
        report = _sample_report()
        assert report.node_by_id("nonexistent") is None


class TestSeverity:
    def test_max_severity_warning(self):
        report = _sample_report()
        assert report.max_severity() == "warning"

    def test_max_severity_error(self):
        report = _sample_report()
        report.diagnostics.append(
            Diagnostic(severity="error", message="bad")
        )
        assert report.max_severity() == "error"

    def test_max_severity_empty(self):
        report = Report()
        assert report.max_severity() is None


class TestEdgeKinds:
    def test_edge_serialization(self):
        e = Edge(src="a", dst="b", kind="order_only")
        d = e.to_dict()
        assert d == {"src": "a", "dst": "b", "kind": "order_only"}
        assert Edge.from_dict(d) == e


class TestVariableEvents:
    def test_event_chain(self):
        v = Variable(
            name="X",
            events=[
                VariableEvent(
                    source=SourceLocation("a.mk", 1),
                    operator="=",
                    scope="global",
                    raw_value="1",
                ),
                VariableEvent(
                    source=SourceLocation("b.mk", 5),
                    operator="+=",
                    scope="global",
                    raw_value="2",
                ),
            ],
            used_by=["target_a"],
        )
        d = v.to_dict()
        restored = Variable.from_dict(d)
        assert len(restored.events) == 2
        assert restored.events[0].operator == "="
        assert restored.events[1].operator == "+="
        assert restored.events[0].resolved_value is None


class TestSourceFile:
    def test_unresolved_file(self):
        sf = SourceFile(path="remote.yml", kind="gitlab_yaml", status="unresolved")
        d = sf.to_dict()
        assert d["status"] == "unresolved"
        assert SourceFile.from_dict(d) == sf


class TestEmptyReport:
    def test_empty_report_round_trip(self):
        r = Report()
        js = r.to_json()
        restored = Report.from_json(js)
        assert restored.schema_version == SCHEMA_VERSION
        assert restored.nodes == []
        assert restored.default_goal is None
