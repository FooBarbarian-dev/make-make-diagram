/* pipeview what-if evaluator.
 *
 * A DOM-free, tri-state (true / false / unknown=null) interpreter over the
 * programs compiled by pipeview/parsers/gitlab_whatif.py. It never parses
 * GitLab syntax — Python did that at generation time. It is inlined into
 * report.html at generation (offline guarantee) and runnable under plain
 * `node` for the vector test suite.
 *
 * Entry point: PipeviewWhatIf.evaluateEvent(report, config)
 *   config = {
 *     scenario: 'push_branch'|'push_tag'|'mr'|'schedule'|'web'|'api'|'trigger',
 *     branch, tag, openMR, target, draft, mrFlavor, tagProtected,
 *     changedFiles: null | [paths], commitMessage, overrides: {NAME: value}
 *   }
 */
'use strict';

var PipeviewWhatIf = (function () {

  /* ---------------- tri-state logic (null = unknown) ---------------- */

  function triAnd(values) {
    var unknown = false;
    for (var i = 0; i < values.length; i++) {
      if (values[i] === false) return false;
      if (values[i] === null) unknown = true;
    }
    return unknown ? null : true;
  }
  function triOr(values) {
    var unknown = false;
    for (var i = 0; i < values.length; i++) {
      if (values[i] === true) return true;
      if (values[i] === null) unknown = true;
    }
    return unknown ? null : false;
  }
  function triNot(v) { return v === null ? null : !v; }

  /* ---------------- expression evaluation ---------------- */

  // env maps NAME -> string; a missing key is "unset" (GitLab null).
  function termValue(term, env) {
    if (term.t === 'var') {
      return Object.prototype.hasOwnProperty.call(env, term.name) ? env[term.name] : null;
    }
    if (term.t === 'str') return term.value;
    if (term.t === 'null') return null;
    return null; // regex terms have no scalar value
  }

  function compileRegex(source, flags) {
    var jsFlags = flags && flags.indexOf('i') >= 0 ? 'i' : '';
    try { return new RegExp(source, jsFlags); } catch (e) { return null; }
  }

  // Right side of =~ / !~: a /regex/ literal, a variable holding /regex/,
  // or (documented-but-discouraged fallback) a plain string → substring test
  // "left is contained in right".
  function matchAgainst(leftVal, right, env, notes) {
    if (leftVal === null) return false;
    if (right.t === 're') {
      var rx = compileRegex(right.source, right.flags);
      if (!rx) { notes.push('invalid regex /' + right.source + '/'); return null; }
      return rx.test(leftVal);
    }
    var rv = termValue(right, env);
    if (rv === null) return false;
    var m = /^\/(.*)\/([a-z]*)$/.exec(rv);
    if (m) {
      var rx2 = compileRegex(m[1], m[2]);
      if (!rx2) { notes.push('invalid regex in variable: ' + rv); return null; }
      return rx2.test(leftVal);
    }
    notes.push('right side "' + rv + '" is not /regex/ — GitLab falls back to a '
      + 'substring check (undocumented behavior)');
    return rv.indexOf(leftVal) >= 0;
  }

  function evalExpr(ast, env, notes) {
    notes = notes || [];
    if (!ast) return true;
    switch (ast.op) {
      case 'opaque': return null;
      case 'and': return triAnd(ast.args.map(function (a) { return evalExpr(a, env, notes); }));
      case 'or': return triOr(ast.args.map(function (a) { return evalExpr(a, env, notes); }));
      case 'not': return triNot(evalExpr(ast.arg, env, notes));
      case 'truthy': {
        var v = termValue(ast.term, env);
        return v !== null && v !== '';   // "false" and "0" are truthy
      }
      case 'cmp': {
        var left = termValue(ast.left, env);
        if (ast.cmp === '==' || ast.cmp === '!=') {
          var right = termValue(ast.right, env);
          var eq = left === right;       // null == null is true (both unset)
          return ast.cmp === '==' ? eq : !eq;
        }
        var matched = matchAgainst(left, ast.right, env, notes);
        if (matched === null) return null;
        return ast.cmp === '=~' ? matched : !matched;
      }
      default: return null;
    }
  }

  /* ---------------- glob matching (mirror of Python glob_to_regex) ------- */

  function globToRegExp(pattern) {
    var out = '';
    var i = 0, n = pattern.length;
    while (i < n) {
      var c = pattern[i];
      if (c === '*') {
        if (pattern.slice(i, i + 3) === '**/') { out += '(?:[^/]+/)*'; i += 3; }
        else if (pattern.slice(i, i + 2) === '**') { out += '.*'; i += 2; }
        else { out += '[^/]*'; i += 1; }
      } else if (c === '?') { out += '[^/]'; i += 1; }
      else if (c === '[') {
        var j = pattern.indexOf(']', i + 1);
        if (j < 0) { out += '\\['; i += 1; }
        else {
          var inner = pattern.slice(i + 1, j);
          if (inner[0] === '!') inner = '^' + inner.slice(1);
          out += '[' + inner + ']';
          i = j + 1;
        }
      } else if (c === '{') {
        var k = pattern.indexOf('}', i + 1);
        if (k < 0) { out += '\\{'; i += 1; }
        else {
          var alts = pattern.slice(i + 1, k).split(',').map(escapeRe);
          out += '(?:' + alts.join('|') + ')';
          i = k + 1;
        }
      } else { out += escapeRe(c); i += 1; }
    }
    try { return new RegExp('^' + out + '$'); } catch (e) { return null; }
  }
  function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  function matchChangedFiles(paths, changedFiles) {
    for (var i = 0; i < paths.length; i++) {
      var rx = globToRegExp(paths[i]);
      if (!rx) return null;
      for (var j = 0; j < changedFiles.length; j++) {
        if (rx.test(changedFiles[j])) return true;
      }
    }
    return false;
  }

  /* ---------------- environment construction ---------------- */

  var FAKE_SHA = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0';
  var ZERO_SHA = '0000000000000000000000000000000000000000';

  function slugify(ref) {
    return ref.toLowerCase().replace(/[^0-9a-z]/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 63);
  }

  function isProtectedRef(ref, whatif, config) {
    if (config && config.tagProtected && ref === config.tag) return true;
    return (whatif.protected_refs || []).indexOf(ref) >= 0;
  }

  // The predefined-variable matrix, per candidate. Facts worth restating:
  // CI_PIPELINE_SOURCE is "push" for BOTH branch and tag pipelines;
  // CI_COMMIT_BRANCH is unset in MR and tag pipelines; in MR pipelines
  // CI_COMMIT_REF_NAME is the SOURCE BRANCH NAME (not the merge-request ref
  // path, which is CI_MERGE_REQUEST_REF_PATH); CI_OPEN_MERGE_REQUESTS is set
  // in branch pipelines too when an open MR uses the branch as source.
  function buildEnv(candidate, config, whatif) {
    var msg = (config.commitMessage || 'Update code');
    var nl = msg.indexOf('\n');
    var env = {
      CI: 'true',
      GITLAB_CI: 'true',
      CI_DEFAULT_BRANCH: whatif.default_branch,
      CI_PROJECT_PATH: 'group/project',
      CI_PROJECT_NAME: 'project',
      CI_PROJECT_NAMESPACE: 'group',
      CI_PIPELINE_SOURCE: candidate.source,
      CI_COMMIT_SHA: FAKE_SHA,
      CI_COMMIT_SHORT_SHA: FAKE_SHA.slice(0, 8),
      CI_COMMIT_MESSAGE: msg,
      CI_COMMIT_TITLE: nl >= 0 ? msg.slice(0, nl) : msg,
      CI_COMMIT_DESCRIPTION: nl >= 0 ? msg.slice(nl + 1) : '',
      CI_COMMIT_REF_NAME: candidate.ref,
      CI_COMMIT_REF_SLUG: slugify(candidate.ref),
      CI_COMMIT_REF_PROTECTED: isProtectedRef(candidate.ref, whatif, config) ? 'true' : 'false',
      CI_COMMIT_BEFORE_SHA: candidate.source === 'push' ? FAKE_SHA : ZERO_SHA
    };

    if (candidate.refType === 'branch') {
      env.CI_COMMIT_BRANCH = candidate.ref;
      if (config.openMR && config.scenario !== 'push_tag') {
        env.CI_OPEN_MERGE_REQUESTS = 'group/project!1';
      }
    } else if (candidate.refType === 'tag') {
      env.CI_COMMIT_TAG = candidate.ref;
      env.CI_COMMIT_TAG_MESSAGE = '';
    } else if (candidate.refType === 'merge_request') {
      var target = candidate.target || whatif.default_branch;
      var flavor = config.mrFlavor || 'detached';
      env.CI_OPEN_MERGE_REQUESTS = 'group/project!1';
      env.CI_MERGE_REQUEST_ID = '1000';
      env.CI_MERGE_REQUEST_IID = '1';
      env.CI_MERGE_REQUEST_EVENT_TYPE = flavor;
      env.CI_MERGE_REQUEST_REF_PATH = 'refs/merge-requests/1/head';
      env.CI_MERGE_REQUEST_SOURCE_BRANCH_NAME = candidate.ref;
      env.CI_MERGE_REQUEST_TARGET_BRANCH_NAME = target;
      env.CI_MERGE_REQUEST_SOURCE_BRANCH_PROTECTED =
        isProtectedRef(candidate.ref, whatif, config) ? 'true' : 'false';
      env.CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED =
        isProtectedRef(target, whatif, config) ? 'true' : 'false';
      env.CI_MERGE_REQUEST_TITLE = 'Example merge request';
      env.CI_MERGE_REQUEST_DRAFT = config.draft ? 'true' : 'false';
      env.CI_MERGE_REQUEST_PROJECT_ID = '1';
      env.CI_MERGE_REQUEST_PROJECT_PATH = 'group/project';
      env.CI_MERGE_REQUEST_SQUASH_ON_MERGE = 'false';
      // Empty in detached MR pipelines; real SHAs only in merged results /
      // merge trains. Empty-but-set matters: bare $VAR is falsy on "".
      var merged = flavor === 'merged_result' || flavor === 'merge_train';
      env.CI_MERGE_REQUEST_SOURCE_BRANCH_SHA = merged ? FAKE_SHA : '';
      env.CI_MERGE_REQUEST_TARGET_BRANCH_SHA = merged ? FAKE_SHA : '';
    }

    if (candidate.source === 'schedule') {
      env.CI_PIPELINE_SCHEDULE_DESCRIPTION = 'nightly';
    }
    return env;
  }

  /* ---------------- rule / program evaluation ---------------- */

  function describeCondition(rule, ctx) {
    var bits = [];
    if (rule.raw_if) bits.push(rule.raw_if);
    if (rule.changes) bits.push('changes: ' + rule.changes.paths.join(', '));
    if (rule.exists) bits.push('exists: ' + rule.exists.paths.join(', '));
    return bits.join(' AND ') || '(unconditional)';
  }

  // One rule's condition → {v, notes}
  function evalRuleCondition(rule, ctx) {
    var parts = [];
    var notes = [];
    if (rule.if) {
      var v = evalExpr(rule.if, ctx.env, notes);
      parts.push(v);
      if (v === null && rule.if.op === 'opaque') {
        notes.push('expression could not be parsed — treated as unknown');
      }
    }
    if (rule.changes) {
      if (rule.changes.compare_to || rule.changes.regexp) {
        parts.push(null);
        notes.push('changes:' + (rule.changes.compare_to ? 'compare_to' : 'regexp')
          + ' is not resolvable offline');
      } else if (ctx.changesAlwaysTrue) {
        parts.push(true);
        notes.push('rules:changes is always true in pipelines with no push event '
          + '(tag, schedule, manual, api/trigger)');
      } else if (ctx.changedFiles === null || ctx.changedFiles === undefined) {
        parts.push(null);
        notes.push('depends on which files changed — fill in the changed-files list');
      } else {
        var m = matchChangedFiles(rule.changes.paths, ctx.changedFiles);
        parts.push(m);
        if (m === null) notes.push('unsupported glob pattern');
      }
    }
    if (rule.exists) {
      if (rule.exists.result === null) {
        parts.push(null);
        notes.push(rule.exists.reason || 'exists result unknown');
      } else {
        parts.push(rule.exists.result);
        notes.push('exists checked against the repo at report generation time: '
          + (rule.exists.result ? 'found' : 'not found'));
      }
    }
    return { v: parts.length ? triAnd(parts) : true, notes: notes };
  }

  function ruleOutcome(rule, defaults) {
    var when = rule.when || defaults.when || 'on_success';
    if (when === 'never') {
      return { included: false, state: 'skipped', when: 'never' };
    }
    var allowFailure = rule.allow_failure;
    if (allowFailure === null || allowFailure === undefined) {
      // rules:when:manual flips the default to a BLOCKING manual job
      allowFailure = when === 'manual' ? false : (defaults.allow_failure || false);
    }
    return {
      included: true,
      state: when === 'manual' ? 'manual' : when === 'delayed' ? 'delayed' : 'runs',
      when: when,
      allow_failure: allowFailure,
      start_in: rule.start_in || defaults.start_in || null,
      variables: rule.variables || null
    };
  }

  // First-match-wins walk producing either a definite outcome or a
  // conditional tree when an unknown condition forks the trace.
  function walkRules(rules, idx, defaults, ctx, trace) {
    if (idx >= rules.length) {
      trace.push({ rule: null, desc: 'no rule matched', verdict: 'end' });
      return { included: false, state: 'not-added', reason: 'no rule matched' };
    }
    var rule = rules[idx];
    var cond = evalRuleCondition(rule, ctx);
    var desc = describeCondition(rule, ctx);
    if (cond.v === true) {
      var out = ruleOutcome(rule, defaults);
      trace.push({ rule: idx, desc: desc, verdict: 'matched', notes: cond.notes,
                   when: out.when });
      return out;
    }
    if (cond.v === false) {
      trace.push({ rule: idx, desc: desc, verdict: 'no match', notes: cond.notes });
      return walkRules(rules, idx + 1, defaults, ctx, trace);
    }
    trace.push({ rule: idx, desc: desc, verdict: 'unknown', notes: cond.notes });
    var thenOut = ruleOutcome(rule, defaults);
    var elseOut = walkRules(rules, idx + 1, defaults, ctx, trace);
    return {
      state: 'conditional',
      condition: desc,
      conditionNotes: cond.notes,
      then: thenOut,
      otherwise: elseOut,
      included: thenOut.included || elseOut.included || elseOut.state === 'conditional'
    };
  }

  var LEGACY_KEYWORDS = {
    branches: function (c) { return c.refType === 'branch'; },
    tags: function (c) { return c.refType === 'tag'; },
    merge_requests: function (c) { return c.source === 'merge_request_event'; },
    pushes: function (c) { return c.source === 'push'; },
    schedules: function (c) { return c.source === 'schedule'; },
    triggers: function (c) { return c.source === 'trigger'; },
    api: function (c) { return c.source === 'api'; },
    web: function (c) { return c.source === 'web'; },
    pipelines: function (c) { return c.source === 'pipeline' || c.source === 'parent_pipeline'; },
    chat: function (c) { return c.source === 'chat'; },
    external: function (c) { return c.source === 'external'; },
    external_pull_requests: function (c) { return c.source === 'external_pull_request_event'; }
  };

  function legacyRefsMatch(refs, candidate, notes) {
    var results = [];
    for (var i = 0; i < refs.length; i++) {
      var entry = refs[i];
      if (LEGACY_KEYWORDS[entry]) {
        results.push(LEGACY_KEYWORDS[entry](candidate));
      } else {
        var m = /^\/(.*)\/([a-z]*)$/.exec(entry);
        if (m) {
          var rx = compileRegex(m[1], m[2]);
          if (!rx) { results.push(null); notes.push('invalid ref regex ' + entry); }
          else results.push(rx.test(candidate.ref));
        } else {
          results.push(entry === candidate.ref);
        }
      }
    }
    return triOr(results);
  }

  function legacyHalf(spec, candidate, ctx, notes, isOnly) {
    // AND over the present constraint kinds; within refs and variables the
    // entries OR together. An absent `only` defaults to branches+tags.
    if (!spec) {
      return isOnly
        ? legacyRefsMatch(['branches', 'tags'], candidate, notes)
        : false;
    }
    var parts = [];
    var refs = spec.refs;
    if (isOnly && !refs) refs = ['branches', 'tags'];
    if (refs) parts.push(legacyRefsMatch(refs, candidate, notes));
    if (spec.variables) {
      parts.push(triOr(spec.variables.map(function (ast) {
        return evalExpr(ast, ctx.env, notes);
      })));
    }
    if (spec.changes) {
      if (ctx.changesAlwaysTrue) parts.push(true);
      else if (ctx.changedFiles == null) {
        parts.push(null);
        notes.push('only/except:changes depends on the changed-files list');
      } else parts.push(matchChangedFiles(spec.changes, ctx.changedFiles));
    }
    if (spec.unsupported && spec.unsupported.length) {
      parts.push(null);
      notes.push('unsupported only/except keys: ' + spec.unsupported.join(', '));
    }
    if (!parts.length) return false;
    return triAnd(parts);
  }

  function evalLegacy(program, defaults, candidate, ctx, trace) {
    var notes = [];
    var onlyV = legacyHalf(program.only, candidate, ctx, notes, true);
    var exceptV = program.except
      ? legacyHalf(program.except, candidate, ctx, notes, false) : false;
    var included = triAnd([onlyV, triNot(exceptV)]);
    var desc = program.implicit_default
      ? 'implicit default only: [branches, tags] (job has no rules/only/except)'
      : 'only/except';
    if (included === true) {
      var out = ruleOutcome({ when: null }, defaults);
      trace.push({ rule: 0, desc: desc, verdict: 'matched', notes: notes, when: out.when });
      return out;
    }
    if (included === false) {
      trace.push({ rule: 0, desc: desc, verdict: 'no match', notes: notes });
      return { included: false, state: 'not-added', reason: desc + ' did not match' };
    }
    trace.push({ rule: 0, desc: desc, verdict: 'unknown', notes: notes });
    return {
      state: 'conditional', condition: desc, conditionNotes: notes,
      then: ruleOutcome({ when: null }, defaults),
      otherwise: { included: false, state: 'not-added' },
      included: true
    };
  }

  function evaluateJob(jobWhatif, candidate, ctx) {
    var defaults = {
      when: jobWhatif.when || 'on_success',
      allow_failure: jobWhatif.allow_failure,
      start_in: jobWhatif.start_in
    };
    var trace = [];
    var program = jobWhatif.program;
    var outcome;
    if (program.kind === 'rules') {
      outcome = walkRules(program.rules, 0, defaults, ctx, trace);
    } else if (program.kind === 'legacy') {
      outcome = evalLegacy(program, defaults, candidate, ctx, trace);
    } else {
      trace.push({ rule: null, desc: program.reason || 'rules unknown', verdict: 'unknown' });
      outcome = {
        state: 'conditional', condition: program.reason || 'rules not analyzable',
        then: ruleOutcome({ when: null }, defaults),
        otherwise: { included: false, state: 'not-added' },
        included: true
      };
    }
    outcome.trace = trace;
    return outcome;
  }

  /* ---------------- workflow gate ---------------- */

  function evalWorkflow(workflow, ctx) {
    if (!workflow || !workflow.rules || !workflow.rules.length) {
      return { created: true, reason: 'no workflow:rules — every pipeline type is allowed',
               variables: {}, trace: [] };
    }
    var trace = [];
    function walk(idx) {
      if (idx >= workflow.rules.length) {
        trace.push({ rule: null, desc: 'no workflow rule matched', verdict: 'end' });
        return { created: false, reason: 'no workflow rule matched — the pipeline is not created',
                 variables: {} };
      }
      var rule = workflow.rules[idx];
      var cond = evalRuleCondition(rule, ctx);
      var desc = describeCondition(rule, ctx);
      if (cond.v === true) {
        var never = rule.when === 'never';
        trace.push({ rule: idx, desc: desc, verdict: 'matched', notes: cond.notes,
                     when: never ? 'never' : 'always' });
        return {
          created: !never,
          reason: 'workflow rule ' + (idx + 1) + (never ? ' says when: never' : ' allows it')
            + ': ' + desc,
          variables: rule.variables || {}
        };
      }
      if (cond.v === false) {
        trace.push({ rule: idx, desc: desc, verdict: 'no match', notes: cond.notes });
        return walk(idx + 1);
      }
      trace.push({ rule: idx, desc: desc, verdict: 'unknown', notes: cond.notes });
      var rest = walk(idx + 1);
      return {
        created: null,
        reason: 'depends on: ' + desc,
        conditional: { then: rule.when !== 'never', otherwise: rest.created },
        variables: rule.variables || rest.variables || {}
      };
    }
    var result = walk(0);
    result.trace = trace;
    return result;
  }

  /* ---------------- candidates ---------------- */

  function buildCandidates(config, whatif) {
    var out = [];
    var branch = config.branch || whatif.default_branch;
    switch (config.scenario) {
      case 'push_branch':
        out.push({ id: 'branch', source: 'push', refType: 'branch', ref: branch,
                   label: 'Branch pipeline', noPushEvent: false, childOf: null });
        if (config.openMR) {
          out.push({ id: 'mr', source: 'merge_request_event', refType: 'merge_request',
                     ref: branch, target: config.target || whatif.default_branch,
                     label: 'Merge request pipeline', noPushEvent: false, childOf: null });
        }
        break;
      case 'push_tag':
        out.push({ id: 'tag', source: 'push', refType: 'tag',
                   ref: config.tag || 'v1.0.0',
                   label: 'Tag pipeline', noPushEvent: true, childOf: null });
        break;
      case 'mr':
        out.push({ id: 'mr', source: 'merge_request_event', refType: 'merge_request',
                   ref: branch, target: config.target || whatif.default_branch,
                   label: 'Merge request pipeline', noPushEvent: false, childOf: null });
        break;
      case 'schedule':
      case 'web':
      case 'api':
      case 'trigger': {
        var src = config.scenario === 'schedule' ? 'schedule' : config.scenario;
        out.push({ id: src, source: src, refType: 'branch', ref: branch,
                   label: { schedule: 'Scheduled pipeline', web: 'Manual pipeline (web)',
                            api: 'API pipeline', trigger: 'Trigger-token pipeline' }[src],
                   noPushEvent: true, childOf: null });
        break;
      }
      default:
        out.push({ id: 'branch', source: 'push', refType: 'branch', ref: branch,
                   label: 'Branch pipeline', noPushEvent: false, childOf: null });
    }
    return out;
  }

  /* ---------------- job indexing ---------------- */

  function jobIndex(report) {
    var jobs = [];
    (report.nodes || []).forEach(function (n) {
      if (n.kind === 'job' && n.annotations && n.annotations.whatif) {
        jobs.push({ id: n.id, node: n, whatif: n.annotations.whatif });
      }
    });
    return jobs;
  }

  function mightRun(state) {
    return state === 'runs' || state === 'manual' || state === 'delayed'
      || state === 'conditional';
  }

  /* ---------------- artifact / consumption analysis ---------------- */

  function stageIdx(stages, stage) {
    var i = stages.indexOf(stage);
    return i < 0 ? stages.length : i;
  }

  function analyzeArtifacts(candidate, jobs, results, whatif, report) {
    var notes = [];
    var errors = [];
    var byId = {};
    jobs.forEach(function (j) { byId[j.id] = j; });
    var nodeIds = {};
    (report.nodes || []).forEach(function (n) { nodeIds[n.id] = n; });
    var included = jobs.filter(function (j) { return mightRun(results[j.id].state); });

    included.forEach(function (job) {
      var w = job.whatif;
      var jobState = results[job.id].state;
      var consumed = [];   // [{producer, viaDotenv, optional}]

      function checkTarget(name, optional, wantArtifacts, kindLabel) {
        var targetId = (w.child_of ? w.child_of + '::' : '') + name;
        var target = byId[targetId];
        if (!target) {
          var node = nodeIds[targetId] || nodeIds[name];
          if (node && node.kind === 'ghost') {
            notes.push({ job: job.id, kind: 'external',
              message: kindLabel + ' "' + name + '" lives in an unresolved include — '
                + 'not simulated' });
          }
          return;
        }
        var st = results[target.id].state;
        if (!mightRun(st)) {
          if (!optional) {
            errors.push({ job: job.id, target: target.id, kind: kindLabel,
              message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", but "'
                + name + '" is not in this pipeline (' + st + ') — GitLab would '
                + 'probably fail to create the pipeline' });
          }
          return;
        }
        if (st === 'conditional' && !optional && jobState !== 'conditional') {
          notes.push({ job: job.id, kind: 'conditional-need',
            message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", whose '
              + 'inclusion is conditional — if it is dropped, pipeline creation '
              + 'probably fails' });
        }
        if (wantArtifacts && target.whatif.artifacts.dotenv.length) {
          consumed.push({ producer: target.id, viaDotenv: true, optional: optional });
        } else if (wantArtifacts && target.whatif.artifacts.paths.length) {
          consumed.push({ producer: target.id, viaDotenv: false, optional: optional });
        }
      }

      if (w.needs !== null && w.needs !== undefined) {
        w.needs.forEach(function (need) {
          if (need.kind === 'cross_pipeline' || need.kind === 'cross_project') {
            notes.push({ job: job.id, kind: 'cross-pipeline',
              message: '"' + w.name + '" fetches artifacts from another '
                + (need.kind === 'cross_pipeline' ? 'pipeline' : 'project')
                + ' (' + need.ref + ') by ref — when duplicate pipelines run on '
                + 'the same ref, which artifacts it gets is ambiguous' });
            return;
          }
          checkTarget(need.job, need.optional, need.artifacts, 'needs');
        });
      } else if (w.dependencies !== null && w.dependencies !== undefined) {
        w.dependencies.forEach(function (dep) {
          checkTarget(dep, false, true, 'depends on');
        });
      } else {
        // GitLab default: artifacts from every included earlier-stage job
        var myStage = stageIdx(whatif.stages, w.stage);
        included.forEach(function (other) {
          if (other.id === job.id) return;
          if (stageIdx(whatif.stages, other.whatif.stage) < myStage) {
            if (other.whatif.artifacts.dotenv.length) {
              consumed.push({ producer: other.id, viaDotenv: true, optional: true });
            }
          }
        });
      }

      consumed.forEach(function (c) {
        if (c.viaDotenv) {
          notes.push({ job: job.id, kind: 'dotenv', producer: c.producer,
            message: '"' + w.name + '" runtime env is extended by variables from "'
              + byId[c.producer].whatif.name + '"’s dotenv report — dotenv '
              + 'variables can never affect rules (rules evaluate before any '
              + 'job runs)' });
        }
      });
    });

    return { notes: notes, errors: errors,
             producers: included.filter(function (j) {
               return j.whatif.artifacts.paths.length || j.whatif.artifacts.dotenv.length;
             }).map(function (j) { return j.id; }) };
  }

  /* ---------------- event evaluation (entry point) ---------------- */

  function evaluateCandidate(candidate, allJobs, config, whatif, report) {
    var env = buildEnv(candidate, config, whatif);
    var base = Object.assign({}, env, whatif.globals || {});
    var overrides = config.overrides || {};

    var ctx0 = { env: Object.assign({}, base, overrides),
                 changedFiles: config.changedFiles,
                 changesAlwaysTrue: candidate.noPushEvent };
    var wf = candidate.childOf ? null : (whatif.workflow || null);
    var gate = evalWorkflow(wf, ctx0);

    var jobs = allJobs.filter(function (j) {
      return (j.whatif.child_of || null) === (candidate.childOf || null);
    });
    var results = {};
    jobs.forEach(function (job) {
      var jobEnv = Object.assign({}, base, gate.variables || {},
                                 job.whatif.variables || {}, overrides);
      var ctx = { env: jobEnv, changedFiles: config.changedFiles,
                  changesAlwaysTrue: candidate.noPushEvent };
      results[job.id] = evaluateJob(job.whatif, candidate, ctx);
    });

    var anyIncluded = jobs.some(function (j) { return mightRun(results[j.id].state); });
    var created = gate.created;
    var reason = gate.reason;
    if (created !== false && !anyIncluded) {
      created = false;
      reason = 'no jobs were added — GitLab does not create an empty pipeline';
    }

    var artifacts = analyzeArtifacts(candidate, jobs, results, whatif, report);

    // child pipelines spawned by included trigger jobs
    var children = [];
    if (!candidate.childOf) {
      jobs.forEach(function (job) {
        var trig = job.whatif.trigger;
        if (!trig || !mightRun(results[job.id].state) || created === false) return;
        (trig.children || []).forEach(function (childRel) {
          var childCandidate = {
            id: candidate.id + '>' + childRel,
            source: 'parent_pipeline',
            refType: candidate.refType,
            ref: candidate.ref,
            target: candidate.target,
            label: 'Child pipeline: ' + childRel,
            noPushEvent: true,
            childOf: childRel,
            parentJob: job.id,
            parentConditional: results[job.id].state === 'conditional'
          };
          children.push(evaluateCandidate(childCandidate, allJobs, config, whatif, report));
        });
        if (trig.project) {
          artifacts.notes.push({ job: job.id, kind: 'downstream',
            message: '"' + job.whatif.name + '" triggers a pipeline in project "'
              + trig.project + '" — its config is not available offline' });
        }
      });
    }

    return {
      id: candidate.id, label: candidate.label, source: candidate.source,
      ref: candidate.ref, refType: candidate.refType, childOf: candidate.childOf || null,
      parentJob: candidate.parentJob || null,
      parentConditional: candidate.parentConditional || false,
      created: created, reason: reason, workflowTrace: gate.trace || [],
      env: env, jobs: results, jobOrder: jobs.map(function (j) { return j.id; }),
      artifacts: artifacts, children: children
    };
  }

  function evaluateEvent(report, config) {
    var whatif = (report.annotations || {}).whatif;
    if (!whatif) return null;
    var allJobs = jobIndex(report);
    var candidates = buildCandidates(config, whatif)
      .map(function (c) { return evaluateCandidate(c, allJobs, config, whatif, report); });

    // duplicates: a job that might run in >= 2 top-level candidates
    var duplicates = [];
    if (candidates.length > 1) {
      var seen = {};
      candidates.forEach(function (c) {
        if (c.created === false) return;
        c.jobOrder.forEach(function (id) {
          if (mightRun(c.jobs[id].state)) {
            (seen[id] = seen[id] || []).push(c.id);
          }
        });
      });
      Object.keys(seen).forEach(function (id) {
        if (seen[id].length > 1) duplicates.push({ job: id, candidates: seen[id] });
      });
    }

    var crossPipelineArtifacts = duplicates.length > 0 && candidates.some(function (c) {
      return c.artifacts.producers.length > 0;
    });

    return {
      candidates: candidates,
      duplicates: duplicates,
      crossPipelineArtifacts: crossPipelineArtifacts,
      lint: whatif.lint || []
    };
  }

  /* ---------------- helpers for the UI ---------------- */

  function collectAstVars(ast, out) {
    if (!ast || typeof ast !== 'object') return;
    if (ast.t === 'var') { out[ast.name] = true; return; }
    ['left', 'right', 'arg', 'term'].forEach(function (k) {
      if (ast[k]) collectAstVars(ast[k], out);
    });
    (ast.args || []).forEach(function (a) { collectAstVars(a, out); });
  }

  // Every variable name referenced in any rules expression — the UI marks
  // the ones the simulation knows nothing about.
  function collectExpressionVariables(report) {
    var out = {};
    var whatif = (report.annotations || {}).whatif;
    if (!whatif) return [];
    function fromRules(rules) {
      (rules || []).forEach(function (r) { collectAstVars(r.if, out); });
    }
    if (whatif.workflow) fromRules(whatif.workflow.rules);
    (report.nodes || []).forEach(function (n) {
      var w = n.annotations && n.annotations.whatif;
      if (!w) return;
      if (w.program.kind === 'rules') fromRules(w.program.rules);
      if (w.program.kind === 'legacy') {
        [w.program.only, w.program.except].forEach(function (spec) {
          if (spec && spec.variables) {
            spec.variables.forEach(function (ast) { collectAstVars(ast, out); });
          }
        });
      }
    });
    return Object.keys(out).sort();
  }

  return {
    evalExpr: evalExpr,
    globToRegExp: globToRegExp,
    buildEnv: buildEnv,
    buildCandidates: buildCandidates,
    evaluateEvent: evaluateEvent,
    collectExpressionVariables: collectExpressionVariables
  };
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = PipeviewWhatIf;
}
