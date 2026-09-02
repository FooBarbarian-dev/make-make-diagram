import * as assert from "node:assert/strict";
import { test } from "node:test";

import {
  batchSpawnArgs,
  buildReportArgs,
  cliCandidates,
  defaultPython,
  escapeCmdArgument,
  escapeCmdCommand,
  isWindowsBatchFile,
  needsGitLabSetupHint,
  needsTokenHint,
  reportHtmlPaths,
  runProcess,
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

test("cliCandidates: Windows adds the py launcher after python", () => {
  const c = cliCandidates("", "", "win32");
  assert.deepEqual(c.map((x) => x.command), ["pipeview", "python", "py"]);
  assert.deepEqual(c[1].prefix, ["-m", "pipeview"]);
  assert.deepEqual(c[2].prefix, ["-3", "-m", "pipeview"]);
});

test("cliCandidates: configured interpreter keeps the py fallback on Windows", () => {
  const c = cliCandidates("", "C:\\venv\\Scripts\\python.exe", "win32");
  assert.deepEqual(c.map((x) => x.command),
                   ["pipeview", "C:\\venv\\Scripts\\python.exe", "py"]);
  assert.ok(!cliCandidates("", "", "linux").some((x) => x.command === "py"));
  assert.ok(!cliCandidates("", "", "darwin").some((x) => x.command === "py"));
});

test("reportHtmlPaths: Windows paths, mixed separators, CRLF", () => {
  const stdout = [
    "Report generated: C:\\Users\\me\\AppData\\Roaming\\Code\\reports/gitlab-ci.*",
    "group/app: C:\\out\\group-app@main.report.html [warning]",
    "",
  ].join("\r\n");
  assert.deepEqual(reportHtmlPaths(stdout), [
    "C:\\Users\\me\\AppData\\Roaming\\Code\\reports/gitlab-ci.report.html",
    "C:\\out\\group-app@main.report.html",
  ]);
});

test("isWindowsBatchFile: only .cmd/.bat on Windows go through cmd.exe", () => {
  assert.ok(isWindowsBatchFile("C:\\tools\\pipeview.cmd", "win32"));
  assert.ok(isWindowsBatchFile("python.BAT", "win32"));
  assert.ok(!isWindowsBatchFile("C:\\Python312\\python.exe", "win32"));
  assert.ok(!isWindowsBatchFile("pipeview", "win32"));
  assert.ok(!isWindowsBatchFile("/usr/local/bin/pipeview.cmd", "linux"));
});

test("cmd.exe escaping follows the cross-spawn rules", () => {
  assert.equal(escapeCmdCommand("C:\\tools\\pipeview.cmd"), "C:\\tools\\pipeview.cmd");
  assert.equal(escapeCmdCommand("C:\\Program Files\\x\\p.cmd"),
               "C:\\Program^ Files\\x\\p.cmd");
  // quoted, meta characters caret-escaped twice (cmd, then the batch file)
  assert.equal(escapeCmdArgument("--format"), '^^^"--format^^^"');
  assert.equal(escapeCmdArgument("C:\\out dir"), '^^^"C:\\out^^^ dir^^^"');
  // trailing backslashes double up so the closing quote survives
  assert.equal(escapeCmdArgument("C:\\out\\"), '^^^"C:\\out\\\\^^^"');
  // embedded quotes are escaped for the CRT and then for cmd
  assert.equal(escapeCmdArgument('a"b'), '^^^"a\\^^^"b^^^"');
});

test("batchSpawnArgs: one verbatim command line for cmd.exe /d /s /c", () => {
  const { file, args } = batchSpawnArgs("C:\\t\\pipeview.cmd", ["--version"],
                                        "C:\\Windows\\System32\\cmd.exe");
  assert.equal(file, "C:\\Windows\\System32\\cmd.exe");
  assert.deepEqual(args.slice(0, 3), ["/d", "/s", "/c"]);
  assert.equal(args[3], '"C:\\t\\pipeview.cmd ^^^"--version^^^""');
  // an empty ComSpec falls back to the bare name (undefined would read
  // the real ComSpec through the parameter default — set on Windows)
  assert.equal(batchSpawnArgs("x.cmd", [], "").file, "cmd.exe");
});

test("runProcess: forces UTF-8 Python I/O and captures output", async () => {
  const cli = { command: process.execPath, prefix: ["-e"], source: "node" };
  const r = await runProcess(cli, [
    "process.stdout.write('utf8=' + process.env.PYTHONUTF8); " +
      "process.stderr.write('err'); process.exitCode = 3;",
  ], { env: { ...process.env, PYTHONUTF8: "0" } });
  assert.equal(r.code, 3);
  assert.equal(r.stdout, "utf8=1");
  assert.equal(r.stderr, "err");
});

test("runProcess: a missing executable rejects instead of hanging", async () => {
  const cli = { command: "definitely-not-a-real-binary-pipeview", prefix: [], source: "x" };
  await assert.rejects(runProcess(cli, ["--version"]));
});

