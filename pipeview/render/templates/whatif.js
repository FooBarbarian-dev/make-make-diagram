/* pipeview what-if evaluator.
 *
 * A DOM-free, tri-state (true / false / unknown=null) interpreter over the
 * programs compiled by pipeview/parsers/gitlab_whatif.py. It never parses
 * GitLab syntax — Python did that at generation time. It is inlined into
 * report.html at generation (offline guarantee) and runnable under plain
 * `node` for the vector test suite.
 *
 * Value model for variables (three different "missing" cases):
 *   - a name in the env            → that string value
 *   - deliberately unset for this pipeline type (CI_COMMIT_BRANCH in an MR
 *     pipeline, CI_MERGE_REQUEST_* in a branch pipeline, …) → unset (null)
 *   - any other CI_ or GITLAB_ name → UNKNOWN: real GitLab sets it at
 *     runtime, the simulation doesn't — verdicts touching it are tri-state
 *     unknown, never a confident guess
 *   - any other custom name        → unset (defined nowhere)
 *
 * Entry point: PipeviewWhatIf.evaluateEvent(report, config)
 *   config = {
 *     scenario: 'push_branch'|'push_tag'|'mr'|'schedule'|'web'|'api'|'trigger',
 *     branch, tag, refKind ('branch'|'tag' for schedule/web/api/trigger),
 *     openMR, target, draft, mrFlavor, mrLabels, tagProtected, newBranch,
 *     changedFiles: null | 'all' | [paths], commitMessage,
 *     overrides: {NAME: value}
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

  /* ---------------- variable lookup ---------------- */

  var UNKNOWN = { runtimeUnknown: true };   // sentinel, never leaks as a string
  var PREDEFINED_RE = /^(CI|GITLAB)_/;

  // controlled = {names: [...], prefixes: [...]} — the variables this
  // candidate's env-builder DECIDED about (set or deliberately unset).
  function lookupVar(name, env, controlled, notes) {
    if (Object.prototype.hasOwnProperty.call(env, name)) return env[name];
    if (controlled && PREDEFINED_RE.test(name)) {
      var decided = controlled.names.indexOf(name) >= 0
        || controlled.prefixes.some(function (p) { return name.indexOf(p) === 0; });
      if (!decided) {
        if (notes) {
          notes.push('$' + name + ' is set by the pipeline at runtime — not '
            + 'simulated; add it in the variables panel to pin a value');
        }
        return UNKNOWN;
      }
    }
    return null;   // deliberately unset, or a custom variable defined nowhere
  }

  function termValue(term, env, controlled, notes) {
    if (term.t === 'var') return lookupVar(term.name, env, controlled, notes);
    if (term.t === 'str') return term.value;
    if (term.t === 'null') return null;
    return null; // regex terms have no scalar value
  }

  /* ---------------- expression evaluation ---------------- */

  function compileRegex(source, flags, notes) {
    var jsFlags = '';
    var dropped = '';
    (flags || '').split('').forEach(function (f) {
      if ('ims'.indexOf(f) >= 0) jsFlags += f;   // RE2-compatible, JS-supported
      else dropped += f;
    });
    if (dropped && notes) notes.push('regex flags "' + dropped + '" are not supported');
    try { return new RegExp(source, jsFlags); } catch (e) { return null; }
  }

  // Right side of =~ / !~: a /regex/ literal, a variable holding /regex/,
  // or (documented-but-discouraged fallback) a plain string → substring test
  // "left is contained in right".
  function matchAgainst(leftVal, right, env, controlled, notes) {
    if (leftVal === UNKNOWN) return null;
    // GitLab scans text.to_s — an unset left side behaves as "" (so a
    // pattern that matches the empty string, like /.*/, DOES match)
    if (leftVal === null) leftVal = '';
    if (right.t === 're') {
      if (right.non_re2) {
        notes.push('/' + right.source + '/ uses lookaround or backreferences — '
          + "GitLab's RE2 engine rejects it and the configuration is invalid");
        return null;
      }
      var rx = compileRegex(right.source, right.flags, notes);
      if (!rx) { notes.push('invalid regex /' + right.source + '/'); return null; }
      return rx.test(leftVal);
    }
    var rv = termValue(right, env, controlled, notes);
    if (rv === UNKNOWN) return null;
    if (rv === null) {
      // GitLab: `return false unless regexp` — a definite no-match
      notes.push('the pattern variable is unset — GitLab returns no-match');
      return false;
    }
    var m = /^\/(.*)\/([a-z]*)$/.exec(rv);
    if (m) {
      var rx2 = compileRegex(m[1], m[2], notes);
      if (!rx2) { notes.push('invalid regex in variable: ' + rv); return null; }
      return rx2.test(leftVal);
    }
    notes.push('right side "' + rv + '" is not /regex/ — GitLab falls back to a '
      + 'substring check (undocumented behavior)');
    return rv.indexOf(leftVal) >= 0;
  }

  function evalExpr(ast, env, notes, controlled) {
    notes = notes || [];
    if (!ast) return true;
    switch (ast.op) {
      case 'opaque':
        notes.push('expression could not be parsed: ' + (ast.src || ''));
        return null;
      case 'invalid':
        notes.push('GitLab rejects this expression (invalid expression syntax): '
          + (ast.src || ''));
        return null;
      case 'and': return triAnd(ast.args.map(function (a) { return evalExpr(a, env, notes, controlled); }));
      case 'or': return triOr(ast.args.map(function (a) { return evalExpr(a, env, notes, controlled); }));
      case 'not': return triNot(evalExpr(ast.arg, env, notes, controlled));
      case 'truthy': {
        var v = termValue(ast.term, env, controlled, notes);
        if (v === UNKNOWN) return null;
        return v !== null && v !== '';   // "false" and "0" are truthy
      }
      case 'cmp': {
        // chained comparisons: the left side may itself be a comparison
        // (left-associative, GitLab's shunting-yard behavior)
        var leftIsExpr = !!ast.left.op;
        var left = leftIsExpr ? evalExpr(ast.left, env, notes, controlled)
                              : termValue(ast.left, env, controlled, notes);
        if (ast.cmp === '==' || ast.cmp === '!=') {
          var rightIsExpr = !!ast.right.op;
          var right = rightIsExpr ? evalExpr(ast.right, env, notes, controlled)
                                  : termValue(ast.right, env, controlled, notes);
          if (leftIsExpr !== rightIsExpr) {
            // a boolean result never equals a string/null, unknown or not
            return ast.cmp === '==' ? false : true;
          }
          if (leftIsExpr) {
            if (left === null || right === null) return null;
            return ast.cmp === '==' ? left === right : left !== right;
          }
          if (left === UNKNOWN || right === UNKNOWN) return null;
          var eq = left === right;       // null == null is true (both unset)
          return ast.cmp === '==' ? eq : !eq;
        }
        if (leftIsExpr) {
          notes.push('matching a boolean result with =~ is not modeled');
          return null;
        }
        var matched = matchAgainst(left, ast.right, env, controlled, notes);
        if (matched === null) return null;
        return ast.cmp === '=~' ? matched : !matched;
      }
      default: return null;
    }
  }

  function collectAstVars(ast, out) {
    if (!ast || typeof ast !== 'object') return;
    if (ast.t === 'var') { out[ast.name] = true; return; }
    ['left', 'right', 'arg', 'term'].forEach(function (k) {
      if (ast[k]) collectAstVars(ast[k], out);
    });
    (ast.args || []).forEach(function (a) { collectAstVars(a, out); });
  }

  /* ---------------- glob matching (mirror of Python _glob_translate) ------ */

  function globTranslate(pattern) {
    var out = '';
    var i = 0, n = pattern.length;
    while (i < n) {
      var c = pattern[i];
      if (c === '\\' && i + 1 < n) { out += escapeRe(pattern[i + 1]); i += 2; }
      else if (c === '*') {
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
          // EXTGLOB: each alternative is itself a glob — recurse
          var alts = pattern.slice(i + 1, k).split(',').map(globTranslate);
          out += '(?:' + alts.join('|') + ')';
          i = k + 1;
        }
      } else { out += escapeRe(c); i += 1; }
    }
    return out;
  }
  function globToRegExp(pattern) {
    try { return new RegExp('^' + globTranslate(pattern) + '$'); }
    catch (e) { return null; }
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
  var FAKE_BEFORE_SHA = '900dcafe900dcafe900dcafe900dcafe900dcafe';
  var ZERO_SHA = '0000000000000000000000000000000000000000';

  function slugify(ref) {
    return ref.toLowerCase().replace(/[^0-9a-z]/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 63);
  }

  function isProtectedRef(ref, refType, whatif, config) {
    if (refType === 'tag') return !!(config && config.tagProtected);
    return (whatif.protected_refs || []).indexOf(ref) >= 0;
  }

  // Names the env-builder decides about even when it leaves them unset —
  // so the evaluator can tell "deliberately unset" from "not simulated".
  var CONTROLLED = {
    names: ['CI_COMMIT_BRANCH', 'CI_COMMIT_TAG', 'CI_COMMIT_TAG_MESSAGE',
            'CI_OPEN_MERGE_REQUESTS', 'CI_PIPELINE_SCHEDULE_DESCRIPTION',
            'CI_PIPELINE_TRIGGERED'],
    prefixes: ['CI_MERGE_REQUEST_']
  };

  function controlledFor(env) {
    var names = Object.keys(env);
    CONTROLLED.names.forEach(function (n) {
      if (names.indexOf(n) < 0) names.push(n);
    });
    return { names: names, prefixes: CONTROLLED.prefixes.slice() };
  }

  // The predefined-variable matrix, per candidate. Facts worth restating:
  // CI_PIPELINE_SOURCE is "push" for BOTH branch and tag pipelines;
  // CI_COMMIT_BRANCH is unset in MR and tag pipelines; in MR pipelines
  // CI_COMMIT_REF_NAME is the SOURCE BRANCH NAME (the git ref path is
  // CI_MERGE_REQUEST_REF_PATH); CI_OPEN_MERGE_REQUESTS is set in EVERY
  // branch pipeline (push, schedule, api, web, trigger) when an open MR
  // uses the branch as source — that's what the documented dedup pattern
  // relies on; CI_COMMIT_BEFORE_SHA is all zeros except ordinary branch
  // pushes (MR/schedule/manual/api/trigger/tag/new-branch → zeros).
  function buildEnv(candidate, config, whatif) {
    var msg = (config.commitMessage || 'Update code');
    var nl = msg.indexOf('\n');
    var isPlainBranchPush = candidate.source === 'push'
      && candidate.refType === 'branch' && !config.newBranch;
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
      CI_COMMIT_REF_PROTECTED:
        isProtectedRef(candidate.ref, candidate.refType, whatif, config) ? 'true' : 'false',
      CI_COMMIT_BEFORE_SHA: isPlainBranchPush ? FAKE_BEFORE_SHA : ZERO_SHA
    };

    if (candidate.refType === 'branch') {
      env.CI_COMMIT_BRANCH = candidate.ref;
      if (config.openMR) env.CI_OPEN_MERGE_REQUESTS = 'group/project!1';
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
        isProtectedRef(candidate.ref, 'branch', whatif, config) ? 'true' : 'false';
      env.CI_MERGE_REQUEST_TARGET_BRANCH_PROTECTED =
        isProtectedRef(target, 'branch', whatif, config) ? 'true' : 'false';
      env.CI_MERGE_REQUEST_TITLE = 'Example merge request';
      // always "true"/"false" in MR pipelines, never unset (source-verified)
      env.CI_MERGE_REQUEST_DRAFT = config.draft ? 'true' : 'false';
      env.CI_MERGE_REQUEST_PROJECT_ID = '1';
      env.CI_MERGE_REQUEST_PROJECT_PATH = 'group/project';
      env.CI_MERGE_REQUEST_SQUASH_ON_MERGE = 'false';
      if (config.mrLabels) env.CI_MERGE_REQUEST_LABELS = config.mrLabels;
      // Empty in detached MR pipelines; real SHAs only in merged results /
      // merge trains. Empty-but-set matters: bare $VAR is falsy on "".
      var merged = flavor === 'merged_result' || flavor === 'merge_train';
      env.CI_MERGE_REQUEST_SOURCE_BRANCH_SHA = merged ? FAKE_SHA : '';
      env.CI_MERGE_REQUEST_TARGET_BRANCH_SHA = merged ? FAKE_SHA : '';
    }

    if (candidate.source === 'schedule') {
      env.CI_PIPELINE_SCHEDULE_DESCRIPTION = 'nightly';
    }
    if (candidate.source === 'trigger') {
      env.CI_PIPELINE_TRIGGERED = 'true';
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

  function displayValue(name, env, controlled) {
    var v = lookupVar(name, env, controlled, null);
    if (v === UNKNOWN) return { name: name, value: null, runtime: true };
    return { name: name, value: v, runtime: false };
  }

  // One rule's condition → {v, notes, vars}
  function evalRuleCondition(rule, ctx) {
    var parts = [];
    var notes = [];
    var vars = [];
    if (rule.if) {
      parts.push(evalExpr(rule.if, ctx.env, notes, ctx.controlled));
      var names = {};
      collectAstVars(rule.if, names);
      Object.keys(names).sort().forEach(function (n) {
        vars.push(displayValue(n, ctx.env, ctx.controlled));
      });
    }
    if (rule.changes) {
      if (rule.changes.compare_to || rule.changes.regexp) {
        parts.push(null);
        notes.push('changes:' + (rule.changes.compare_to ? 'compare_to' : 'regexp')
          + ' is not resolvable offline');
      } else if (ctx.changesAlwaysTrue) {
        parts.push(true);
        notes.push('rules:changes is always true in pipelines with no push event '
          + '(tag, schedule, manual, api/trigger, first push of a new branch)');
      } else if (ctx.changedFiles === 'all') {
        parts.push(true);
        notes.push('assuming every changes: pattern matches');
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
    return { v: parts.length ? triAnd(parts) : true, notes: notes, vars: vars };
  }

  function ruleOutcome(rule, defaults, hasRules) {
    var when = rule.when || defaults.when || 'on_success';
    var out;
    if (when === 'never') {
      out = { included: false, state: 'skipped', when: 'never' };
    } else {
      var allowFailure = rule.allow_failure;
      if (allowFailure === null || allowFailure === undefined) {
        if (defaults.allow_failure !== null && defaults.allow_failure !== undefined) {
          // an explicit job-level allow_failure always applies
          allowFailure = defaults.allow_failure;
        } else if (when === 'manual') {
          // GitLab: a manual job defaults to BLOCKING whenever the job uses
          // rules (manual_action? && !has_rules?); only legacy rule-less
          // manual jobs default to optional (allow_failure: true)
          allowFailure = hasRules ? false : true;
        } else {
          allowFailure = false;
        }
      }
      out = {
        included: true,
        state: when === 'manual' ? 'manual' : when === 'delayed' ? 'delayed' : 'runs',
        when: when,
        allow_failure: allowFailure,
        start_in: rule.start_in || defaults.start_in || null,
        variables: rule.variables || null
      };
    }
    if (rule.needs !== undefined && rule.needs !== null) {
      out.needsOverride = rule.needs;   // rules:needs replaces the job's needs
    }
    return out;
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
      var out = ruleOutcome(rule, defaults, true);
      trace.push({ rule: idx, desc: desc, verdict: 'matched', notes: cond.notes,
                   vars: cond.vars, when: out.when });
      return out;
    }
    if (cond.v === false) {
      trace.push({ rule: idx, desc: desc, verdict: 'no match', notes: cond.notes,
                   vars: cond.vars });
      return walkRules(rules, idx + 1, defaults, ctx, trace);
    }
    trace.push({ rule: idx, desc: desc, verdict: 'unknown', notes: cond.notes,
                 vars: cond.vars });
    var thenOut = ruleOutcome(rule, defaults, true);
    var elseOut = walkRules(rules, idx + 1, defaults, ctx, trace);
    if (!thenOut.included && !elseOut.included && elseOut.state !== 'conditional') {
      // the unknown condition cannot change the answer — stay definite
      return {
        included: false,
        state: thenOut.state === 'skipped' || elseOut.state === 'skipped'
          ? 'skipped' : 'not-added',
        when: thenOut.when,
        collapsed: true,
        reason: 'excluded whichever way the unknown conditions go'
      };
    }
    var conditional = {
      state: 'conditional',
      condition: desc,
      conditionNotes: cond.notes,
      then: thenOut,
      otherwise: elseOut,
      included: thenOut.included || elseOut.included
    };
    if (thenOut.needsOverride !== undefined || elseOut.needsOverride !== undefined
        || elseOut.needsUncertain) {
      conditional.needsUncertain = true;
    }
    return conditional;
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
      } else if (entry.indexOf('@') >= 0) {
        results.push(null);
        notes.push('ref@project form "' + entry + '" is not simulated');
      } else {
        var m = /^\/(.*)\/([a-z]*)$/.exec(entry);
        if (m) {
          var rx = compileRegex(m[1], m[2], notes);
          if (!rx) { results.push(null); notes.push('invalid ref regex ' + entry); }
          else results.push(rx.test(candidate.ref));
        } else {
          results.push(entry === candidate.ref);
        }
      }
    }
    return triOr(results);
  }

  // GitLab's documented combination (verified against source): `only`
  // includes when ALL clause kinds match (AND); `except` excludes when ANY
  // clause kind matches (OR). Inside every array it is OR. An absent `only`
  // defaults to branches+tags.
  function legacyHalf(spec, candidate, ctx, notes, isOnly) {
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
        return evalExpr(ast, ctx.env, notes, ctx.controlled);
      })));
    }
    if (spec.changes) {
      // only:changes with a no-push-event ref is documented "always true"
      // (only → job runs; except → job never runs)
      if (ctx.changesAlwaysTrue || ctx.changedFiles === 'all') parts.push(true);
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
    return isOnly ? triAnd(parts) : triOr(parts);
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
      var out = ruleOutcome({ when: null }, defaults, false);
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
      then: ruleOutcome({ when: null }, defaults, false),
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
        then: ruleOutcome({ when: null }, defaults, true),
        otherwise: { included: false, state: 'not-added' },
        included: true
      };
    }
    outcome.trace = trace;
    return outcome;
  }

  // The honest might-this-job-run test: conditional counts only when some
  // branch of the unknown actually includes the job.
  function mightRun(outcome) {
    if (!outcome) return false;
    if (outcome.state === 'conditional') return !!outcome.included;
    return outcome.state === 'runs' || outcome.state === 'manual'
      || outcome.state === 'delayed';
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
                     vars: cond.vars, when: never ? 'never' : 'always' });
        return {
          created: !never,
          reason: 'workflow rule ' + (idx + 1) + (never ? ' says when: never' : ' allows it')
            + ': ' + desc,
          variables: rule.variables || {}
        };
      }
      if (cond.v === false) {
        trace.push({ rule: idx, desc: desc, verdict: 'no match', notes: cond.notes,
                     vars: cond.vars });
        return walk(idx + 1);
      }
      trace.push({ rule: idx, desc: desc, verdict: 'unknown', notes: cond.notes,
                   vars: cond.vars });
      var rest = walk(idx + 1);
      var uncertainVars = (rule.variables && Object.keys(rule.variables).length)
        ? Object.keys(rule.variables) : [];
      if (rest.variablesUncertain) {
        uncertainVars = uncertainVars.concat(rest.variablesUncertain);
      }
      var thenCreated = rule.when !== 'never';
      if (rest.created === thenCreated) {
        // the unknown rule cannot change whether the pipeline is created
        var collapsed = {
          created: rest.created,
          reason: rest.created
            ? 'the pipeline is created whichever way the unknown rule goes'
            : 'the pipeline is not created whichever way the unknown rule goes',
          variables: rest.variables || {}
        };
        if (uncertainVars.length) collapsed.variablesUncertain = uncertainVars;
        return collapsed;
      }
      var result = {
        created: null,
        reason: 'depends on: ' + desc,
        conditional: { then: thenCreated, otherwise: rest.created },
        // an unknown rule's variables are NOT applied as fact — jobs are
        // evaluated with the definite path's variables plus a caveat
        variables: rest.variables || {}
      };
      if (uncertainVars.length) result.variablesUncertain = uncertainVars;
      return result;
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
        var src = config.scenario;
        var labels = { schedule: 'Scheduled pipeline', web: 'Manual pipeline (web)',
                       api: 'API pipeline', trigger: 'Trigger-token pipeline' };
        if (config.refKind === 'tag') {
          out.push({ id: src, source: src, refType: 'tag',
                     ref: config.tag || 'v1.0.0',
                     label: labels[src] + ' on a tag', noPushEvent: true, childOf: null });
        } else {
          out.push({ id: src, source: src, refType: 'branch', ref: branch,
                     label: labels[src], noPushEvent: true, childOf: null });
        }
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
    var unresolvedIncludes = whatif.unresolved_includes || [];
    var included = jobs.filter(function (j) { return mightRun(results[j.id]); });

    included.forEach(function (job) {
      var w = job.whatif;
      var outcome = results[job.id];
      var dotenvIn = [];   // producers whose dotenv reaches this job

      function checkTarget(name, optional, wantArtifacts, kindLabel) {
        var targetId = (w.child_of ? w.child_of + '::' : '') + name;
        var target = byId[targetId];
        if (!target) {
          var node = nodeIds[targetId] || nodeIds[name];
          if (node && node.kind === 'job') {
            // a template (.job) or otherwise never-runnable definition
            errors.push({ job: job.id, target: name, kind: kindLabel,
              message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", a '
                + 'hidden/template job that is never added to pipelines — '
                + 'GitLab fails to create the pipeline' });
          } else if (unresolvedIncludes.length) {
            notes.push({ job: job.id, kind: 'external',
              message: kindLabel + ' "' + name + '" is not in the local files — '
                + 'probably defined in an unresolved include ('
                + unresolvedIncludes.join(', ') + '), not simulated' });
          } else if (!optional) {
            errors.push({ job: job.id, target: name, kind: kindLabel,
              message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", which '
                + 'is not defined anywhere — GitLab fails to create the pipeline' });
          }
          return;
        }
        var st = results[target.id];
        if (!mightRun(st)) {
          if (!optional) {
            errors.push({ job: job.id, target: target.id, kind: kindLabel,
              message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", but "'
                + name + '" is not in this pipeline (' + st.state + ') — GitLab '
                + 'would probably fail to create the pipeline' });
          }
          return;
        }
        if (kindLabel === 'depends on'
            && stageIdx(whatif.stages, target.whatif.stage)
               >= stageIdx(whatif.stages, w.stage)) {
          errors.push({ job: job.id, target: target.id, kind: kindLabel,
            message: '"' + w.name + '" lists "' + name + '" in dependencies, '
              + 'but it is not in an earlier stage — GitLab rejects this' });
          return;
        }
        if (st.state === 'conditional' && !optional && outcome.state !== 'conditional') {
          notes.push({ job: job.id, kind: 'conditional-need',
            message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", whose '
              + 'inclusion is conditional — if it is dropped, pipeline creation '
              + 'probably fails' });
        }
        if (st.state === 'manual') {
          notes.push({ job: job.id, kind: 'manual-producer',
            message: '"' + w.name + '" consumes artifacts from "' + name + '", '
              + 'a manual job — the artifacts exist only after its gate is run' });
        }
        if (wantArtifacts && target.whatif.artifacts.dotenv.length) {
          dotenvIn.push(target.id);
        }
      }

      var effectiveNeeds = w.needs;
      if (outcome.needsUncertain) {
        notes.push({ job: job.id, kind: 'needs-uncertain',
          message: '"' + w.name + '" has rules:needs on a rule whose match is '
            + 'uncertain — artifact flow is not analyzed for it' });
        effectiveNeeds = undefined;
      } else if (outcome.needsOverride !== undefined) {
        effectiveNeeds = outcome.needsOverride;   // rules:needs replaced them
      }

      if (effectiveNeeds !== null && effectiveNeeds !== undefined) {
        effectiveNeeds.forEach(function (need) {
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
      } else if (outcome.needsUncertain) {
        /* skipped above */
      } else if (w.dependencies !== null && w.dependencies !== undefined) {
        w.dependencies.forEach(function (dep) {
          checkTarget(dep, false, true, 'depends on');
        });
      } else {
        // GitLab default: artifacts from every included earlier-stage job
        var myStage = stageIdx(whatif.stages, w.stage);
        included.forEach(function (other) {
          if (other.id === job.id) return;
          if (stageIdx(whatif.stages, other.whatif.stage) < myStage
              && other.whatif.artifacts.dotenv.length) {
            dotenvIn.push(other.id);
          }
        });
      }

      dotenvIn.forEach(function (producer) {
        notes.push({ job: job.id, kind: 'dotenv', producer: producer,
          message: '"' + w.name + '" runtime env is extended by variables from "'
            + byId[producer].whatif.name + '"’s dotenv report — dotenv '
            + 'variables can never affect rules (rules evaluate before any '
            + 'job runs)' });
      });
    });

    return { notes: notes, errors: errors,
             producers: included.filter(function (j) {
               return j.whatif.artifacts.paths.length || j.whatif.artifacts.dotenv.length;
             }).map(function (j) { return j.id; }) };
  }

  /* ---------------- event evaluation (entry point) ---------------- */

  var MAX_CHILD_DEPTH = 3;

  function evaluateCandidate(candidate, allJobs, config, whatif, report) {
    var env = buildEnv(candidate, config, whatif);
    var controlled = controlledFor(env);
    var overrides = config.overrides || {};
    // Child pipelines: the child file's own globals apply, and the parent's
    // globals are forwarded by trigger:forward (pipeline-variable
    // precedence: forwarded values beat the child's YAML).
    var childGlobals = candidate.childOf
      ? ((whatif.child_globals || {})[candidate.childOf] || {}) : null;
    var base = candidate.childOf
      ? Object.assign({}, env, childGlobals, whatif.globals || {})
      : Object.assign({}, env, whatif.globals || {});
    var changesAlwaysTrue = candidate.noPushEvent
      || !!(config.newBranch && candidate.source === 'push'
            && candidate.refType === 'branch');

    var ctx0 = { env: Object.assign({}, base, overrides),
                 controlled: controlled,
                 changedFiles: config.changedFiles,
                 changesAlwaysTrue: changesAlwaysTrue };
    // the child's own workflow gates the child (never the parent's)
    var wf = candidate.childOf
      ? ((whatif.child_workflows || {})[candidate.childOf] || null)
      : (whatif.workflow || null);
    var gate = evalWorkflow(wf, ctx0);

    var jobs = allJobs.filter(function (j) {
      return (j.whatif.child_of || null) === (candidate.childOf || null);
    });
    var results = {};
    jobs.forEach(function (job) {
      var jobEnv = Object.assign({}, base, gate.variables || {},
                                 job.whatif.variables || {}, overrides);
      var ctx = { env: jobEnv, controlled: controlled,
                  changedFiles: config.changedFiles,
                  changesAlwaysTrue: changesAlwaysTrue };
      results[job.id] = evaluateJob(job.whatif, candidate, ctx);
    });

    var includedJobs = jobs.filter(function (j) { return mightRun(results[j.id]); });
    // .pre/.post alone are not a pipeline (verified GitLab behavior)
    var realJobs = includedJobs.filter(function (j) {
      return j.whatif.stage !== '.pre' && j.whatif.stage !== '.post';
    });
    var created = gate.created;
    var reason = gate.reason;
    if (created !== false && !realJobs.length) {
      created = false;
      reason = includedJobs.length
        ? 'only .pre/.post jobs were added — GitLab does not consider that a '
          + 'complete pipeline and does not create it'
        : 'no jobs were added — GitLab does not create an empty pipeline';
    }

    var artifacts = analyzeArtifacts(candidate, jobs, results, whatif, report);
    // a started environment whose on_stop job is excluded here can never be
    // auto-stopped — the docs tell users to keep both jobs' rules in sync
    jobs.forEach(function (job) {
      var envc = job.whatif.environment;
      if (!envc || !envc.on_stop) return;
      if (envc.action && envc.action !== 'start') return;
      if (!mightRun(results[job.id])) return;
      var stopId = (job.whatif.child_of ? job.whatif.child_of + '::' : '') + envc.on_stop;
      var stop = results[stopId];
      if (stop && !mightRun(stop)) {
        artifacts.notes.push({ job: job.id, kind: 'env-stop',
          message: '"' + job.whatif.name + '" starts environment "' + envc.name
            + '" but its on_stop job "' + envc.on_stop + '" is not in this '
            + 'pipeline (' + stop.state + ') — the environment can never be '
            + 'auto-stopped; keep both jobs’ rules in sync' });
      }
    });
    if (gate.variablesUncertain) {
      artifacts.notes.unshift({ job: null, kind: 'workflow-vars',
        message: 'workflow variables ' + gate.variablesUncertain.join(', ')
          + ' come from a rule whose match is uncertain — they were NOT '
          + 'applied to this evaluation' });
    }

    // child pipelines spawned by included trigger jobs (recursive)
    var children = [];
    var lineage = candidate.lineage || [];
    jobs.forEach(function (job) {
      var trig = job.whatif.trigger;
      if (!trig || !mightRun(results[job.id]) || created === false) return;
      (trig.children || []).forEach(function (childRel) {
        if (childRel === (candidate.childOf || null) || lineage.indexOf(childRel) >= 0) {
          artifacts.notes.push({ job: job.id, kind: 'downstream',
            message: '"' + job.whatif.name + '" re-triggers ' + childRel
              + ' which is already in this chain — cycle not expanded' });
          return;
        }
        if (lineage.length >= MAX_CHILD_DEPTH) {
          artifacts.notes.push({ job: job.id, kind: 'downstream',
            message: 'child pipelines nested deeper than ' + MAX_CHILD_DEPTH
              + ' levels are not expanded' });
          return;
        }
        var childCandidate = {
          id: candidate.id + '>' + job.id + '>' + childRel,
          source: 'parent_pipeline',
          refType: candidate.refType,
          ref: candidate.ref,
          target: candidate.target,
          label: 'Child pipeline: ' + childRel,
          noPushEvent: true,
          childOf: childRel,
          parentJob: job.id,
          parentConditional: results[job.id].state === 'conditional',
          lineage: lineage.concat([candidate.childOf || '(root)'])
        };
        var childResult = evaluateCandidate(childCandidate, allJobs, config, whatif, report);
        children.push(childResult);
        if (childResult.created === false) {
          artifacts.errors.push({ job: job.id, target: childRel, kind: 'trigger',
            message: '"' + job.whatif.name + '"’s child pipeline ' + childRel
              + ' would have no jobs (' + childResult.reason + ') — GitLab '
              + 'fails the trigger job: "downstream pipeline can not be '
              + 'created, the resulting pipeline would have been empty"' });
        }
      });
      (trig.unresolved || []).forEach(function (ref) {
        artifacts.notes.push({ job: job.id, kind: 'downstream',
          message: '"' + job.whatif.name + '" triggers a child pipeline whose '
            + 'config is not available offline (' + ref + ') — not simulated' });
      });
      if (trig.project) {
        artifacts.notes.push({ job: job.id, kind: 'downstream',
          message: '"' + job.whatif.name + '" triggers a pipeline in project "'
            + trig.project + '" — its config is not available offline' });
      }
    });

    // needs/dependencies problems make GitLab refuse to create THIS pipeline
    // (trigger-kind errors fail only the trigger job, not the pipeline)
    var creationFails = created !== false && artifacts.errors.some(function (e) {
      return e.kind !== 'trigger';
    });

    return {
      id: candidate.id, label: candidate.label, source: candidate.source,
      ref: candidate.ref, refType: candidate.refType, childOf: candidate.childOf || null,
      parentJob: candidate.parentJob || null,
      parentConditional: candidate.parentConditional || false,
      created: created, reason: reason, creationFails: creationFails,
      workflowTrace: gate.trace || [],
      workflowVariables: gate.variables || {},
      env: env, controlled: controlled,
      jobs: results, jobOrder: jobs.map(function (j) { return j.id; }),
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
        if (c.created === false || c.creationFails) return;
        c.jobOrder.forEach(function (id) {
          if (mightRun(c.jobs[id])) {
            (seen[id] = seen[id] || []).push(c.id);
          }
        });
      });
      Object.keys(seen).forEach(function (id) {
        if (seen[id].length > 1) duplicates.push({ job: id, candidates: seen[id] });
      });
    }

    var producing = candidates.filter(function (c) {
      return c.created !== false && c.artifacts.producers.length > 0;
    });

    return {
      candidates: candidates,
      duplicates: duplicates,
      crossPipelineArtifacts: producing.length >= 2,
      lint: whatif.lint || [],
      fatal: whatif.fatal || []
    };
  }

  /* ---------------- helpers for the UI ---------------- */

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
    Object.keys(whatif.child_workflows || {}).forEach(function (k) {
      fromRules(whatif.child_workflows[k].rules);
    });
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
    mightRun: mightRun,
    collectExpressionVariables: collectExpressionVariables
  };
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = PipeviewWhatIf;
}
