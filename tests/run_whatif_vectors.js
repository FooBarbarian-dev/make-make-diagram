#!/usr/bin/env node
/* Vector runner: executes the shipped whatif.js evaluator against the
 * pre-compiled vectors handed over by tests/test_whatif_evaluator.py.
 * Input (argv[2]): JSON with {whatifPath, expr: [{name, ast, env}],
 * scenarios: [{name, report, config}]}. Output (stdout): JSON results.
 */
'use strict';

const fs = require('fs');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const W = require(input.whatifPath);

const out = { expr: [], scenarios: [] };

for (const v of input.expr) {
  const got = W.evalExpr(v.ast, v.env, [], v.controlled || null);
  out.expr.push({ name: v.name, got: got === null ? 'unknown' : got });
}

for (const s of input.scenarios) {
  out.scenarios.push({ name: s.name, got: W.evaluateEvent(s.report, s.config) });
}

process.stdout.write(JSON.stringify(out));
