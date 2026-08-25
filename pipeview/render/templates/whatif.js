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

  // GitLab matches both the plural keywords and the singular sanitized
  // source names; `pipelines`/`pipeline` match multi-project downstreams
  // ONLY — parent_pipeline (child pipelines) does not pluralize to it.
  var LEGACY_KEYWORDS = {
    branches: function (c) { return c.refType === 'branch'; },
    tags: function (c) { return c.refType === 'tag'; },
    merge_requests: function (c) { return c.source === 'merge_request_event'; },
    merge_request: function (c) { return c.source === 'merge_request_event'; },
    pushes: function (c) { return c.source === 'push'; },
    push: function (c) { return c.source === 'push'; },
    schedules: function (c) { return c.source === 'schedule'; },
    schedule: function (c) { return c.source === 'schedule'; },
    triggers: function (c) { return c.source === 'trigger'; },
    trigger: function (c) { return c.source === 'trigger'; },
    api: function (c) { return c.source === 'api'; },
    web: function (c) { return c.source === 'web'; },
    pipelines: function (c) { return c.source === 'pipeline'; },
    pipeline: function (c) { return c.source === 'pipeline'; },
    parent_pipelines: function (c) { return c.source === 'parent_pipeline'; },
    parent_pipeline: function (c) { return c.source === 'parent_pipeline'; },
    chat: function (c) { return c.source === 'chat'; },
    external: function (c) { return c.source === 'external'; },
    external_pull_requests: function (c) { return c.source === 'external_pull_request_event'; },
    external_pull_request: function (c) { return c.source === 'external_pull_request_event'; }
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
      } else if (candidate.refType === 'merge_request') {
        // ref name/regex patterns match branch and tag names only — never
        // merge request pipelines
        results.push(false);
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


  function evaluateJobProgram(jobWhatif, candidate, ctx) {
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

  // parallel:matrix — GitLab expands instances BEFORE rules evaluate, each
  // instance seeing its own axis variables (at job-variable precedence, so
  // forwarded/override values are re-applied on top)
  function evaluateJobMatrix(jobWhatif, candidate, ctx) {
    var par = jobWhatif.parallel;
    if (!par) return evaluateJobProgram(jobWhatif, candidate, ctx);
    if (par.kind === 'count') {
      var single = evaluateJobProgram(jobWhatif, candidate, ctx);
      single.matrixCount = par.count;
      return single;
    }
    var reapply = ctx.reapply || {};
    var per = par.combos.map(function (c) {
      var subCtx = {
        env: Object.assign({}, ctx.env, c.vars, reapply),
        controlled: ctx.controlled,
        changedFiles: ctx.changedFiles,
        changesAlwaysTrue: ctx.changesAlwaysTrue,
        reapply: reapply
      };
      return { name: c.name, outcome: evaluateJobProgram(jobWhatif, candidate, subCtx) };
    });
    if (!per.length) return evaluateJobProgram(jobWhatif, candidate, ctx);
    var ORDER = ['runs', 'delayed', 'manual', 'conditional', 'skipped', 'not-added'];
    var pick = per[0];
    per.forEach(function (p) {
      if (ORDER.indexOf(p.outcome.state) < ORDER.indexOf(pick.outcome.state)) pick = p;
    });
    var base = Object.assign({}, pick.outcome);
    base.matrix = per.map(function (p) {
      return { name: p.name, state: p.outcome.state, when: p.outcome.when || null };
    });
    base.matrixCount = per.length;
    base.matrixPartial = per.some(function (p) {
      return p.outcome.state !== per[0].outcome.state;
    });
    base.included = per.some(function (p) { return mightRun(p.outcome); });
    if (per.some(function (p) { return p.outcome.needsUncertain; })) {
      base.needsUncertain = true;
    }
    return base;
  }

  // include:rules — the file's jobs exist only when the include gate lets
  // the file in (no matching rule → the include is NOT processed)
  function walkIncludeGate(rules, ctx) {
    function walk(idx) {
      if (idx >= rules.length) return false;
      var cond = evalRuleCondition(rules[idx], ctx);
      var thenV = rules[idx].when !== 'never';
      if (cond.v === true) return thenV;
      if (cond.v === false) return walk(idx + 1);
      var rest = walk(idx + 1);
      return rest === thenV ? rest : null;
    }
    return walk(0);
  }

  function evaluateJob(jobWhatif, candidate, ctx) {
    var gateRules = jobWhatif.include_gate;
    if (!gateRules) return evaluateJobMatrix(jobWhatif, candidate, ctx);
    var gv = walkIncludeGate(gateRules, ctx);
    var gdesc = gateRules.map(function (r) { return describeCondition(r, ctx); })
      .join(' | ');
    if (gv === false) {
      return { included: false, state: 'not-added',
        reason: 'its include is not processed for this pipeline',
        trace: [{ rule: null, verdict: 'no match',
          desc: 'from a conditional include gated on: ' + gdesc
            + ' — the include is not processed, the job does not exist here' }] };
    }
    var inner = evaluateJobMatrix(jobWhatif, candidate, ctx);
    if (gv === true) {
      inner.trace.unshift({ rule: null, verdict: 'matched',
        desc: 'from a conditional include (gate matched): ' + gdesc });
      return inner;
    }
    if (!inner.included && inner.state !== 'conditional') {
      inner.trace.unshift({ rule: null, verdict: 'unknown',
        desc: 'include gate unknown (' + gdesc + ') — the job is excluded either way' });
      return inner;
    }
    var wrapped = {
      state: 'conditional',
      condition: 'include gated on: ' + gdesc,
      then: inner.state === 'conditional' ? inner.then : inner,
      otherwise: { included: false, state: 'not-added' },
      included: inner.included,
      trace: inner.trace
    };
    if (inner.needsUncertain || inner.needsOverride !== undefined) {
      wrapped.needsUncertain = true;
    }
    wrapped.trace.unshift({ rule: null, verdict: 'unknown',
      desc: 'from a conditional include gated on: ' + gdesc });
    return wrapped;
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
        // "job: [v1, v2]" addresses one matrix instance by its expanded name
        var lookupName = name;
        var instanceVals = null;
        var mInst = /^(.+?):\s*\[(.+)\]$/.exec(name);
        if (mInst) { lookupName = mInst[1]; instanceVals = mInst[2]; }
        var targetId = (w.child_of ? w.child_of + '::' : '') + lookupName;
        var target = byId[targetId];
        if (!target) {
          var node = nodeIds[targetId] || nodeIds[lookupName];
          if (node && node.kind === 'job') {
            var isTemplate = lookupName.charAt(0) === '.'
              || (node.flags || []).indexOf('template') >= 0;
            errors.push({ job: job.id, target: name, kind: kindLabel,
              message: isTemplate
                ? '"' + w.name + '" ' + kindLabel + ' "' + name + '", a '
                  + 'hidden/template job that is never added to pipelines — '
                  + 'GitLab fails to create the pipeline'
                : '"' + w.name + '" ' + kindLabel + ' "' + name + '", a job in '
                  + 'a different pipeline scope (parent/child) — needs cannot '
                  + 'cross pipelines; GitLab fails to create the pipeline' });
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
        if (instanceVals !== null) {
          var tpar = target.whatif.parallel;
          var fullName = lookupName + ': [' + instanceVals + ']';
          var inst = (st.matrix || []).filter(function (mm) {
            return mm.name === fullName;
          })[0];
          if (!tpar || tpar.kind !== 'matrix' || ((st.matrix || []).length && !inst)) {
            errors.push({ job: job.id, target: target.id, kind: kindLabel,
              message: '"' + w.name + '" ' + kindLabel + ' "' + name + '", which '
                + 'is not a valid matrix instance of "' + lookupName + '" — '
                + 'GitLab fails to create the pipeline' });
            return;
          }
          if (inst) {
            st = { state: inst.state, when: inst.when,
                   included: inst.state === 'conditional' ? true : undefined };
          }
        }
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
        if (st.when === 'on_failure') {
          notes.push({ job: job.id, kind: 'on-failure-chain',
            message: '"' + w.name + '" needs "' + name + '", which runs only '
              + 'after an earlier failure — "' + w.name + '" is effectively '
              + 'skipped in green pipelines' });
        }
        if (wantArtifacts && target.whatif.artifacts.when === 'on_failure') {
          notes.push({ job: job.id, kind: 'artifacts-when',
            message: '"' + w.name + '" consumes artifacts from "' + name + '", '
              + 'which uploads them only when it FAILS (artifacts:when: '
              + 'on_failure) — on success there is nothing to download' });
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
    // yaml defaults visible in this candidate: the child file's own globals
    // for a child pipeline, the main pipeline's globals otherwise
    var yamlView = candidate.childOf
      ? ((whatif.child_globals || {})[candidate.childOf] || {})
      : (whatif.globals || {});
    // trigger:forward-ed variables arrive at pipeline-variable precedence
    // (above the child's own yaml values)
    var pipelineView = candidate.forwardedVars || {};
    var base = Object.assign({}, env, yamlView, pipelineView);
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
      // inherit:variables filters the yaml defaults BEFORE rules evaluate
      var inh = job.whatif.inherit_variables;
      var globalsPart = yamlView;
      if (inh === false) globalsPart = {};
      else if (Array.isArray(inh)) {
        globalsPart = {};
        inh.forEach(function (n) {
          if (Object.prototype.hasOwnProperty.call(yamlView, n)) {
            globalsPart[n] = yamlView[n];
          }
        });
      }
      var jobEnv = Object.assign({}, env, globalsPart, gate.variables || {},
                                 job.whatif.variables || {}, pipelineView, overrides);
      var ctx = { env: jobEnv, controlled: controlled,
                  changedFiles: config.changedFiles,
                  changesAlwaysTrue: changesAlwaysTrue,
                  // matrix axis vars slot in at job-variable precedence;
                  // these layers re-apply on top of them
                  reapply: Object.assign({}, pipelineView, overrides) };
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
        // trigger:forward — by default the trigger job's yaml variables
        // (over the parent's yaml defaults) are forwarded to the child
        var fwd = trig.forward || { yaml_variables: true };
        var forwardedVars = fwd.yaml_variables === false
          ? {}
          : Object.assign({}, yamlView, job.whatif.variables || {});
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
          forwardedVars: forwardedVars,
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
      ref: candidate.ref, refType: candidate.refType, target: candidate.target || null,
      childOf: candidate.childOf || null,
      parentJob: candidate.parentJob || null,
      parentConditional: candidate.parentConditional || false,
      created: created, reason: reason, creationFails: creationFails,
      workflowTrace: gate.trace || [],
      workflowVariables: gate.variables || {},
      forwardedVars: pipelineView,
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
      var jobById = {};
      allJobs.forEach(function (j) { jobById[j.id] = j; });
      Object.keys(seen).forEach(function (id) {
        if (seen[id].length > 1) {
          var entry = { job: id, candidates: seen[id] };
          var c0 = candidates.filter(function (c) { return c.jobs[id]; })[0];
          if (c0 && c0.jobs[id].matrixCount) entry.instances = c0.jobs[id].matrixCount;
          // a shared resource_group serializes the duplicated runs
          if (jobById[id] && jobById[id].whatif.resource_group) {
            entry.resource_group = jobById[id].whatif.resource_group;
          }
          duplicates.push(entry);
        }
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

  /* ---------------- plain-text listing + scenario delta ---------------- */

  var SCENARIO_LABEL = {
    push_branch: 'Push to a branch', push_tag: 'Push a new tag',
    mr: 'MR created / "Run pipeline" on MR', schedule: 'Scheduled run',
    web: 'Manual run (web UI)', api: 'API pipeline', trigger: 'Trigger token'
  };

  // One line that makes a pasted listing self-describing: scenario, ref,
  // MR knobs, changed-files assumption, variable overrides.
  function describeConfig(config) {
    var parts = [SCENARIO_LABEL[config.scenario] || config.scenario];
    var onTag = config.scenario === 'push_tag' || config.refKind === 'tag';
    if (config.scenario === 'mr') {
      parts.push('MR ' + config.branch + ' → ' + (config.target || 'main')
        + (config.draft ? ' (draft)' : ''));
    } else if (onTag) {
      parts.push('tag ' + (config.tag || 'v1.0.0')
        + (config.tagProtected ? ' (protected)' : ''));
    } else {
      parts.push('branch ' + config.branch + (config.newBranch ? ' (new)' : ''));
      if (config.openMR) {
        parts.push('open MR → ' + (config.target || 'main')
          + (config.draft ? ' (draft)' : ''));
      }
    }
    if (config.changedFiles === 'all') {
      parts.push('changed files: every pattern matches');
    } else if (Array.isArray(config.changedFiles)) {
      parts.push(config.changedFiles.length
        ? 'changed files: ' + config.changedFiles.join(', ')
        : 'changed files: none match');
    }
    var ov = Object.keys(config.overrides || {}).sort();
    if (ov.length) {
      parts.push('variables: ' + ov.map(function (n) {
        return n + '="' + config.overrides[n] + '"';
      }).join(', '));
    }
    return parts.join(' — ');
  }

  function outcomeText(out) {
    if (!out) return 'not evaluated';
    var t;
    if (out.state === 'runs') {
      t = out.when === 'on_failure' ? 'runs only after an earlier job fails'
        : out.when === 'always' ? 'runs (when: always)' : 'runs';
    } else if (out.state === 'manual') {
      t = 'manual ' + (out.allow_failure === false ? '(blocking)' : '(optional)');
    } else if (out.state === 'delayed') {
      t = 'delayed' + (out.start_in ? ' ' + out.start_in : '');
    } else if (out.state === 'conditional') {
      t = 'depends: ' + (out.condition || 'unknown condition');
    } else if (out.state === 'skipped') {
      t = 'skipped (when: never)';
    } else {
      t = 'not added';
    }
    if (out.matrixPartial && out.matrix) {
      var r = out.matrix.filter(function (m) {
        return ['runs', 'manual', 'delayed', 'conditional'].indexOf(m.state) >= 0;
      }).length;
      t += ' [' + r + '/' + out.matrix.length + ' matrix instances]';
    } else if (out.matrixCount) {
      t += ' ×' + out.matrixCount;
    }
    return t;
  }

  function jobMeta(report) {
    var names = {}, stages = {};
    (report.nodes || []).forEach(function (n) {
      var w = n.annotations && n.annotations.whatif;
      names[n.id] = w ? w.name : n.name;
      stages[n.id] = w ? w.stage : '';
    });
    return { names: names, stages: stages,
             stageOrder: (((report.annotations || {}).whatif) || {}).stages || [] };
  }

  // Listings read like GitLab presents pipelines: stage order first, YAML
  // definition order within a stage. Unknown stages sort last, stably.
  function stageSorted(ids, meta) {
    var pos = {};
    ids.forEach(function (id, i) { pos[id] = i; });
    var rank = function (id) {
      var i = meta.stageOrder.indexOf(meta.stages[id]);
      return i < 0 ? meta.stageOrder.length : i;
    };
    return ids.slice().sort(function (x, y) {
      return (rank(x) - rank(y)) || (pos[x] - pos[y]);
    });
  }

  function padTo(s, width) {
    while (s.length < width) s += ' ';
    return s;
  }

  function candHeading(cand) {
    var ref = cand.refType === 'merge_request' && cand.target
      ? cand.ref + ' → ' + cand.target : cand.ref;
    return cand.label + ' (' + cand.source + ' on ' + ref + ')';
  }

  // The copy/paste answer to "which jobs would run for this trigger?" —
  // one section per candidate pipeline, only the jobs that might run.
  function textSummary(report, result, config) {
    var meta = jobMeta(report);
    var lines = ['what-if: ' + describeConfig(config)];
    if (result.fatal && result.fatal.length) {
      lines.push('');
      lines.push('invalid configuration — GitLab refuses to create any pipeline:');
      result.fatal.forEach(function (f) { lines.push('  ' + f.message); });
      return lines.join('\n');
    }
    function section(cand, depth) {
      var pad = padTo('', depth * 2);
      lines.push('');
      var head = pad + (depth ? 'child pipeline: ' : '') + candHeading(cand);
      if (cand.created === false) {
        lines.push(head + ' — not created ('
          + (cand.reason || 'no reason recorded') + ')');
        return;
      }
      if (cand.creationFails) {
        head += ' — creation FAILS (needs/dependencies problem, see report)';
      } else if (cand.created === null) {
        head += ' — creation uncertain: ' + (cand.reason || 'depends');
      }
      lines.push(head);
      var ids = stageSorted(
        cand.jobOrder.filter(function (id) { return mightRun(cand.jobs[id]); }), meta);
      if (!ids.length) {
        lines.push(pad + '  (no jobs would run)');
      } else {
        var nameW = 0, stageW = 0;
        ids.forEach(function (id) {
          nameW = Math.max(nameW, Math.min((meta.names[id] || id).length, 40));
          stageW = Math.max(stageW, (meta.stages[id] || '').length + 2);
        });
        ids.forEach(function (id) {
          lines.push(pad + '  ' + padTo(meta.names[id] || id, nameW)
            + '  ' + padTo('[' + (meta.stages[id] || '') + ']', stageW)
            + '  ' + outcomeText(cand.jobs[id]));
        });
      }
      (cand.children || []).forEach(function (child) { section(child, depth + 1); });
    }
    result.candidates.forEach(function (cand) { section(cand, 0); });
    if (result.duplicates && result.duplicates.length) {
      lines.push('');
      lines.push('duplicate jobs (may run in more than one pipeline for this '
        + 'single event): ' + result.duplicates.map(function (d) {
          return meta.names[d.job] || d.job;
        }).join(', '));
    }
    return lines.join('\n');
  }

  // Candidate matching across two evaluations: an event spawns at most one
  // MR-type and one non-MR top-level candidate (see buildCandidates), so
  // MR matches MR and non-MR matches non-MR — a branch pipeline compares
  // against a tag or scheduled pipeline, which IS the trigger delta.
  // Children match by (parent pair, trigger job, child file).
  function candMatchKey(cand, parentKey) {
    if (!parentKey) return cand.source === 'merge_request_event' ? 'mr' : 'main';
    return parentKey + ' > ' + (cand.parentJob || '?') + ' > ' + (cand.childOf || '?');
  }

  function flattenCandidates(result) {
    var out = [];
    function walk(list, parentKey) {
      (list || []).forEach(function (c) {
        var key = candMatchKey(c, parentKey);
        out.push({ key: key, cand: c });
        walk(c.children, key);
      });
    }
    walk(result ? result.candidates : [], null);
    return out;
  }

  var STATE_RANK = { runs: 4, delayed: 3, manual: 2, conditional: 1 };

  // Event-level view: for each job, its strongest outcome across every
  // pipeline this event actually creates (children included).
  function effectiveOutcomes(result) {
    var eff = {};
    if (!result || (result.fatal && result.fatal.length)) return eff;
    flattenCandidates(result).forEach(function (entry) {
      var c = entry.cand;
      if (c.created === false || c.creationFails) return;
      c.jobOrder.forEach(function (id) {
        var out = c.jobs[id];
        if (!mightRun(out)) return;
        var cur = eff[id];
        if (!cur) {
          eff[id] = { state: out.state, outcome: out, pipes: [c.label] };
        } else {
          cur.pipes.push(c.label);
          if ((STATE_RANK[out.state] || 0) > (STATE_RANK[cur.state] || 0)) {
            cur.state = out.state;
            cur.outcome = out;
          }
        }
      });
    });
    return eff;
  }

  function liveIds(cand) {
    if (!cand || cand.created === false || cand.creationFails) return [];
    return cand.jobOrder.filter(function (id) { return mightRun(cand.jobs[id]); });
  }

  // Structured diff of two evaluateEvent results (A = baseline, B = current).
  // Job-level: added / removed / changed / same over effective outcomes —
  // "changed" also covers the duplicate case (same state, but the job now
  // runs in a different NUMBER of pipelines). Pair-level: matched candidate
  // pairs with the union of might-run ids and a per-id delta, for graphs.
  function diffEvents(resultA, resultB) {
    var effA = effectiveOutcomes(resultA);
    var effB = effectiveOutcomes(resultB);
    var order = [];
    function pushOrder(result) {
      flattenCandidates(result).forEach(function (entry) {
        entry.cand.jobOrder.forEach(function (id) {
          if ((effA[id] || effB[id]) && order.indexOf(id) < 0) order.push(id);
        });
      });
    }
    pushOrder(resultB);
    pushOrder(resultA);

    var jobs = {};
    var counts = { added: 0, removed: 0, changed: 0, same: 0 };
    order.forEach(function (id) {
      var a = effA[id] || null, b = effB[id] || null;
      var delta = !a ? 'added' : !b ? 'removed'
        : (outcomeText(a.outcome) === outcomeText(b.outcome)
           && a.pipes.length === b.pipes.length) ? 'same' : 'changed';
      counts[delta] += 1;
      jobs[id] = { delta: delta, a: a, b: b };
    });

    var byKeyA = {}, byKeyB = {};
    flattenCandidates(resultA).forEach(function (e) { byKeyA[e.key] = e.cand; });
    flattenCandidates(resultB).forEach(function (e) { byKeyB[e.key] = e.cand; });
    var keys = Object.keys(byKeyB);
    Object.keys(byKeyA).forEach(function (k) {
      if (keys.indexOf(k) < 0) keys.push(k);
    });
    var pairs = keys.map(function (k) {
      var a = byKeyA[k] || null, b = byKeyB[k] || null;
      var aIds = liveIds(a), bIds = liveIds(b);
      var ids = bIds.slice();
      aIds.forEach(function (id) { if (ids.indexOf(id) < 0) ids.push(id); });
      var deltas = {};
      ids.forEach(function (id) {
        var inA = aIds.indexOf(id) >= 0, inB = bIds.indexOf(id) >= 0;
        deltas[id] = !inA ? 'added' : !inB ? 'removed'
          : outcomeText(a.jobs[id]) === outcomeText(b.jobs[id]) ? 'same' : 'changed';
      });
      return { key: k, a: a, b: b, ids: ids, deltas: deltas };
    });

    return { jobs: jobs, order: order, counts: counts, pairs: pairs };
  }

  function pipesNote(eff) {
    if (!eff || !eff.pipes.length) return '';
    return '  [' + eff.pipes.join(', ') + ']';
  }

  // One line per candidate pair when anything about the PIPELINES (not the
  // jobs) differs: one-sided pairs, differing labels, or a creation problem.
  // "4 removed" is misleading if the real story is "the tag pipeline would
  // fail creation" — the pasted text must carry that.
  function pipelineStatusLines(diff) {
    var status = function (cand, side) {
      if (!cand) return null;
      if (cand.creationFails) return side + ': creation FAILS';
      if (cand.created === false) {
        return side + ': not created (' + (cand.reason || 'suppressed') + ')';
      }
      if (cand.created === null) {
        return side + ': creation uncertain';
      }
      return null;
    };
    var lines = [];
    var noteworthy = false;
    diff.pairs.forEach(function (p) {
      var name;
      if (p.a && p.b) {
        name = p.a.label === p.b.label ? p.b.label
          : p.a.label + ' → ' + p.b.label;
        if (p.a.label !== p.b.label) noteworthy = true;
      } else if (p.b) {
        name = p.b.label + ' — current only';
        noteworthy = true;
      } else {
        name = p.a.label + ' — baseline only';
        noteworthy = true;
      }
      var flags = [status(p.a, 'baseline'), status(p.b, 'current')]
        .filter(function (f) { return f; });
      if (flags.length) noteworthy = true;
      lines.push('    ' + name + (flags.length ? ' — ' + flags.join('; ') : ''));
    });
    return noteworthy ? ['  pipelines:'].concat(lines) : [];
  }

  // The copy/paste delta: +/-/~/= per job, event-level.
  function textDiff(report, diff, labelA, labelB) {
    var meta = jobMeta(report);
    var c = diff.counts;
    var lines = [
      'what-if delta',
      '  baseline: ' + labelA,
      '  current:  ' + labelB,
      '  ' + c.added + ' added, ' + c.removed + ' removed, '
        + c.changed + ' changed, ' + c.same + ' unchanged'
    ].concat(pipelineStatusLines(diff));
    var nameW = 0;
    diff.order.forEach(function (id) {
      nameW = Math.max(nameW, Math.min((meta.names[id] || id).length, 40));
    });
    var order = stageSorted(diff.order, meta);
    ['added', 'removed', 'changed', 'same'].forEach(function (kind) {
      var block = order.filter(function (id) { return diff.jobs[id].delta === kind; });
      if (!block.length) return;
      lines.push('');
      block.forEach(function (id) {
        var j = diff.jobs[id];
        var name = padTo(meta.names[id] || id, nameW);
        if (kind === 'added') {
          lines.push('+ ' + name + '  ' + outcomeText(j.b.outcome) + pipesNote(j.b));
        } else if (kind === 'removed') {
          lines.push('- ' + name + '  was: ' + outcomeText(j.a.outcome) + pipesNote(j.a));
        } else if (kind === 'changed') {
          var ta = outcomeText(j.a.outcome), tb = outcomeText(j.b.outcome);
          lines.push('~ ' + name + '  ' + (ta === tb
            ? tb + '  [in ' + j.a.pipes.length + ' pipeline'
              + (j.a.pipes.length > 1 ? 's' : '') + ' → '
              + j.b.pipes.length + ' pipeline' + (j.b.pipes.length > 1 ? 's' : '') + ']'
            : ta + ' → ' + tb));
        } else {
          lines.push('= ' + name + '  ' + outcomeText(j.b.outcome));
        }
      });
    });
    if (!diff.order.length) {
      lines.push('');
      lines.push('(no jobs would run in either configuration)');
    }
    return lines.join('\n');
  }

  return {
    evalExpr: evalExpr,
    globToRegExp: globToRegExp,
    buildEnv: buildEnv,
    buildCandidates: buildCandidates,
    evaluateEvent: evaluateEvent,
    mightRun: mightRun,
    collectExpressionVariables: collectExpressionVariables,
    describeConfig: describeConfig,
    outcomeText: outcomeText,
    textSummary: textSummary,
    diffEvents: diffEvents,
    textDiff: textDiff
  };
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = PipeviewWhatIf;
}
