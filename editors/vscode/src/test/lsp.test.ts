import * as assert from "node:assert/strict";
import { test } from "node:test";

import { spawnCli } from "../cli";
import {
  CLIENT_REPORT_COMMAND,
  initializationOptions,
  reportActionFor,
  serverArgs,
} from "../lsp";

test("serverArgs: the located CLI plus the lsp subcommand", () => {
  assert.deepEqual(serverArgs({ command: "pipeview", prefix: [], source: "x" }), ["lsp"]);
  assert.deepEqual(
    serverArgs({ command: "py", prefix: ["-3", "-m", "pipeview"], source: "x" }),
    ["-3", "-m", "pipeview", "lsp"],
  );
});

test("initializationOptions: server keys as pipeview/lsp.py documents them", () => {
  assert.deepEqual(
    initializationOptions({ useUpstream: false, upstreamRemote: "fork" }, "/out"),
    { announce: false, upstream: false, upstreamRemote: "fork", outputDir: "/out" },
  );
  // VS Code has a palette; the attach toast is for editors without one
  assert.equal(
    initializationOptions({ useUpstream: true, upstreamRemote: "" }, "/o").announce,
    false,
  );
});

test("reportActionFor: the server's browser actions become webview ones", () => {
  assert.deepEqual(reportActionFor("pipeview.openReport"), {
    title: "Pipeview: open pipeline report",
    upstream: true,
  });
  assert.deepEqual(reportActionFor("pipeview.openReportOffline"), {
    title: "Pipeview: open pipeline report without upstream fetch",
    upstream: false,
  });
  assert.equal(reportActionFor("pipeview.somethingElse"), undefined);
  assert.equal(reportActionFor(undefined), undefined);
  assert.equal(reportActionFor("toString"), undefined); // no prototype hits
  assert.equal(CLIENT_REPORT_COMMAND, "pipeview.showReportForFile");
});

test("spawnCli: same spawn rules as report runs (UTF-8 forced, argv after prefix)", async () => {
  const child = spawnCli(
    { command: process.execPath, prefix: ["-e"], source: "node" },
    ["process.stdout.write(process.env.PYTHONUTF8 + ':' + process.argv.slice(1).join(','))",
     "lsp"],
    { env: { ...process.env, PYTHONUTF8: "0" } },
  );
  let out = "";
  child.stdout?.on("data", (d: Buffer) => { out += d.toString(); });
  const code = await new Promise<number | null>((r) => child.on("close", r));
  assert.equal(code, 0);
  assert.equal(out, "1:lsp");
});
