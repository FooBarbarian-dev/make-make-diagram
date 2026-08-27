#!/usr/bin/env node
/* Runner for the plain-text listing + delta helpers: executes the shipped
 * whatif.js against cases handed over by tests/test_whatif_textdiff.py.
 * Input (argv[2]): JSON with {whatifPath, cases: [{name, report, configA,
 * configB?}]}. Output (stdout): JSON results — per case the textSummary of
 * A (and, when configB is present, the structured diff plus textDiff).
 */
'use strict';

const fs = require('fs');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const W = require(input.whatifPath);

const out = { cases: [] };

for (const c of input.cases) {
  const resA = W.evaluateEvent(c.report, c.configA);
  const entry = {
    name: c.name,
    labelA: W.describeConfig(c.configA),
    summaryA: W.textSummary(c.report, resA, c.configA),
    markdownA: W.markdownSummary(c.report, resA, c.configA)
  };
  if (c.configB) {
    const resB = W.evaluateEvent(c.report, c.configB);
    const diff = W.diffEvents(resA, resB);
    entry.labelB = W.describeConfig(c.configB);
    entry.summaryB = W.textSummary(c.report, resB, c.configB);
    entry.counts = diff.counts;
    entry.order = diff.order;
    entry.deltas = {};
    diff.order.forEach(id => { entry.deltas[id] = diff.jobs[id].delta; });
    entry.pairs = diff.pairs.map(p => ({
      key: p.key,
      a: p.a ? p.a.label : null,
      b: p.b ? p.b.label : null,
      ids: p.ids,
      deltas: p.deltas
    }));
    entry.textDiff = W.textDiff(c.report, diff, entry.labelA, entry.labelB);
    entry.markdownDiff = W.markdownDiff(c.report, diff, entry.labelA, entry.labelB);
  }
  out.cases.push(entry);
}

process.stdout.write(JSON.stringify(out));
