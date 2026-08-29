import * as assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildReportArgs,
  cliCandidates,
  defaultPython,
  needsGitLabSetupHint,
  needsTokenHint,
  reportHtmlPaths,
} from "../cli";

test("buildReportArgs: upstream on by default flow", () => {
  assert.deepEqual(
    buildReportArgs("/repo", "/out", {
      useUpstream: true,
      upstreamRemote: "",
      extraArgs: [],
    }),
    ["/repo", "-o", "/out", "--format", "html,json", "--upstream"],
  );
});

test("buildReportArgs: remote override and extra args", () => {
  assert.deepEqual(
    buildReportArgs("/repo", "/out", {
      useUpstream: true,
      upstreamRemote: "fork",
      extraArgs: ["--no-enrich", "-v"],
    }),
    ["/repo", "-o", "/out", "--format", "html,json",
     "--upstream", "--upstream-remote", "fork", "--no-enrich", "-v"],
  );
});

test("buildReportArgs: upstream disabled", () => {
  const args = buildReportArgs("/repo", "/out", {
    useUpstream: false,
    upstreamRemote: "fork",
    extraArgs: [],
  });
  assert.ok(!args.includes("--upstream"));
  assert.ok(!args.includes("--upstream-remote"));
});

test("reportHtmlPaths: main CLI glob line", () => {
  assert.deepEqual(
    reportHtmlPaths("Report generated: /out/gitlab-ci.*\n"),
    ["/out/gitlab-ci.report.html"],
  );
});

test("reportHtmlPaths: gitlab report explicit files", () => {
  const stdout = [
    "Report generated: /out/group-app@main.report.html",
    "Report generated: /out/group-app@main.model.json",
    "",
  ].join("\n");
  assert.deepEqual(reportHtmlPaths(stdout),
                   ["/out/group-app@main.report.html"]);
});

test("reportHtmlPaths: sync entries, severity markers, rollup", () => {
  const stdout = [
    "group/app: /out/group-app@main.report.html",
    "group/lib@stable: /out/group-lib@stable.report.html [warning]",
    "rollup: /out/rollup.report.html (2 projects, 1/2 cross-project links resolved)",
    "not a report line",
  ].join("\n");
  assert.deepEqual(reportHtmlPaths(stdout), [
    "/out/group-app@main.report.html",
    "/out/group-lib@stable.report.html",
    "/out/rollup.report.html",
  ]);
});

test("reportHtmlPaths: duplicates collapse, junk ignored", () => {
  const stdout = [
    "Report generated: /out/x.*",
    "Report generated: /out/x.*",
    "Trigger docs generated: /out/x.trigger-docs/",
  ].join("\n");
  assert.deepEqual(reportHtmlPaths(stdout), ["/out/x.report.html"]);
});

test("needsTokenHint matches only the upstream degradation notice", () => {
  assert.ok(needsTokenHint(
    "  /r/.gitlab-ci.yml: [warning] --upstream: no API token for " +
    "https://gitlab.example.com — cross-repository includes stay unresolved."));
  assert.ok(!needsTokenHint("  /r/.gitlab-ci.yml: 2 warning(s)"));
});

test("needsGitLabSetupHint matches the gitlab CLI's setup errors", () => {
  assert.ok(needsGitLabSetupHint("No GitLab host configured. Pass --host …"));
  assert.ok(needsGitLabSetupHint(
    "No API token for https://gitlab.example.com. Run `pipeview gitlab auth`"));
  assert.ok(!needsGitLabSetupHint("Error: 404 project not found"));
});

test("cliCandidates: explicit setting wins outright", () => {
  const c = cliCandidates("/opt/pipeview", "", "linux");
  assert.equal(c.length, 1);
  assert.deepEqual(c[0].prefix, []);
  assert.equal(c[0].command, "/opt/pipeview");
});

test("cliCandidates: PATH first, python -m fallback", () => {
  const c = cliCandidates("", "", "linux");
  assert.deepEqual(c.map((x) => x.command), ["pipeview", "python3"]);
  assert.deepEqual(c[1].prefix, ["-m", "pipeview"]);
});

test("defaultPython per platform", () => {
  assert.equal(defaultPython("win32"), "python");
  assert.equal(defaultPython("linux"), "python3");
  assert.equal(defaultPython("darwin"), "python3");
});
