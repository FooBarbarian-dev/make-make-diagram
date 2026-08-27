#!/usr/bin/env node
/* Runner for the scenario-YAML export helper: executes the shipped
 * whatif.js against configs handed over by tests/test_whatif_export.py.
 * Input (argv[2]): JSON with {whatifPath, cases: [{name, config}]}.
 * Output (stdout): JSON — per case the scenarioYaml text. */
'use strict';

const fs = require('fs');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const W = require(input.whatifPath);

process.stdout.write(JSON.stringify(input.cases.map(c => ({
  name: c.name,
  yaml: W.scenarioYaml(c.config)
}))));
