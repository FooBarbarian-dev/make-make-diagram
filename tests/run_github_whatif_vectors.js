#!/usr/bin/env node
/* Vector runner: executes the shipped whatif_github.js evaluator against
 * the pre-compiled vectors handed over by
 * tests/test_github_whatif_evaluator.py. Input (argv[2]): JSON with
 * {whatifPath, expr: [{name, ast, ctx}], scenarios: [{name, report,
 * config}]}. Output (stdout): JSON results.
 */
'use strict';

const fs = require('fs');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const W = require(input.whatifPath);

const out = { expr: [], scenarios: [] };

for (const v of input.expr) {
  const ctx = v.ctx || {};
  if (!ctx.contexts) ctx.contexts = {};
  const got = W.evalCondition(v.ast, ctx, []);
  out.expr.push({ name: v.name, got: got === null ? 'unknown' : got });
}

for (const s of input.scenarios) {
  out.scenarios.push({ name: s.name, got: W.evaluateEvent(s.report, s.config) });
}

process.stdout.write(JSON.stringify(out));
