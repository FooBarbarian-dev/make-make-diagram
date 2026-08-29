/* pipeview What-If evaluator for GitHub Actions — the GitHub sibling of
 * whatif.js. A dumb tri-state interpreter over the compiled program
 * pipeview/parsers/github_whatif.py embeds in the model
 * (annotations.whatif with provider "github"). It never parses GitHub
 * workflow syntax.
 *
 * PARITY CONTRACT: pipeview/parsers/github_whatif_eval.py is a literal
 * Python twin of the evaluation half of this file — same functions, same
 * output keys, same note strings. The two are pinned together by
 * tests/github_whatif_vectors.json and tests/test_github_whatif_parity.py.
 * Change evaluation behavior in BOTH places or the parity suite fails.
 *
 * Three-valued logic throughout: true / false / null (= unknown). A value
 * the simulator cannot know (secrets, vars, runner state, repository
 * identity) evaluates to unknown, surfaces as "depends", and is never
 * guessed.
 *
 * The result shape matches the GitLab evaluator's, so the report UI
 * renders both engines with the same code. Candidates are (workflow ×
 * fired event) — one push firing push AND pull_request gives the same
 * workflow two candidate runs, which is exactly GitHub's duplicate-run
 * problem.
 */

var PipeviewWhatIfGH = (function () {
  'use strict';

  /* ---------------- tri-state logic ---------------- */

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

  /* ---------------- value model ---------------- */

  var UNKNOWN = { unknown: true };   // sentinel; never serialized

  var FAKE_SHA = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0';
  var PR_NUMBER = '1234';

  function truthy(value) {
    if (value === UNKNOWN) return null;
    if (value === null || value === undefined || value === false) return false;
    if (typeof value === 'string') return value !== '';
    if (typeof value === 'number') return value !== 0;
    return true;
  }

  function toNumber(value) {
    if (value === null || value === undefined) return 0;
    if (value === true) return 1;
    if (value === false) return 0;
    if (typeof value === 'number') return value;
    if (typeof value === 'string') {
      var s = value.trim();
      if (s === '') return 0;
      var n = Number(s);
      return n;   // Number('12abc') is NaN, Number('0x1a') is 26
    }
    return NaN;
  }

  function looseEqual(a, b) {
    if (typeof a === 'string' && typeof b === 'string') {
      return a.toLowerCase() === b.toLowerCase();
    }
    if (typeof a === 'boolean' && typeof b === 'boolean') return a === b;
    if ((a === null || a === undefined) && (b === null || b === undefined)) {
      return true;
    }
    if (typeof a === 'number' && typeof b === 'number') return a === b;
    var na = toNumber(a), nb = toNumber(b);
    if (na !== na || nb !== nb) return false;
    return na === nb;
  }

  function toStr(v) {
    if (v === null || v === undefined) return '';
    if (v === true) return 'true';
    if (v === false) return 'false';
    if (typeof v === 'number' && isFinite(v) && Math.floor(v) === v) {
      return String(v);
    }
    return String(v);
  }

  /* ---------------- context lookup ---------------- */

  var RUNTIME_PREFIXES = ['runner.', 'steps.', 'job.', 'strategy.'];

  var MISSING = { missing: true };

  // Case-insensitive lookup (GitHub context property names are
  // case-insensitive; the compiler lowercases AST paths). Exact hit
  // first, then a scan. Returns MISSING when absent.
  function ciGet(mapping, key) {
    if (!mapping) return MISSING;
    if (Object.prototype.hasOwnProperty.call(mapping, key)) {
      return mapping[key];
    }
    var low = key.toLowerCase();
    var keys = Object.keys(mapping);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].toLowerCase() === low) return mapping[keys[i]];
    }
    return MISSING;
  }

  function lookupCtx(path, ctx, notes) {
    var overrides = ctx.overrides || {};
    var v = ciGet(overrides, path);
    if (v !== MISSING) return v;
    v = ciGet(ctx.contexts, path);
    if (v !== MISSING) return v;
    if (path.indexOf('env.') === 0) {
      var name = path.slice(4);
      var envChain = ctx.envChain || {};
      v = ciGet(overrides, name);
      if (v !== MISSING) return v;
      v = ciGet(envChain, name);
      if (v !== MISSING) {
        if (typeof v === 'string' && v.indexOf('${{') >= 0) {
          if (notes) {
            notes.push('env.' + name + ' is built from an expression — '
              + 'not simulated');
          }
          return UNKNOWN;
        }
        return v;
      }
      return null;
    }
    if (path.indexOf('vars.') === 0) {
      if (notes) {
        notes.push(path + ' is a repository/organization variable — not '
          + 'visible in the workflow files; add it in the variables panel '
          + 'to pin a value');
      }
      return UNKNOWN;
    }
    if (path.indexOf('secrets.') === 0) {
      if (notes) {
        notes.push(path + ' is a secret — values are never visible; '
          + 'treated as unknown');
      }
      return UNKNOWN;
    }
    if (path.indexOf('inputs.') === 0
        || path.indexOf('github.event.inputs.') === 0) {
      if (ctx.hasInputs) return null;
      if (notes) {
        notes.push(path + ' — this run was not started by a dispatch or a '
          + 'reusable-workflow call, so inputs are empty');
      }
      return null;
    }
    if (path.indexOf('needs.') === 0) {
      var parts = path.split('.');
      if (parts.length >= 3 && parts[2] === 'result') {
        var nr = ciGet(ctx.needResults || {}, parts[1]);
        if (nr !== MISSING) return nr;
        return UNKNOWN;
      }
      if (notes) notes.push(path + ' is produced at run time — not simulated');
      return UNKNOWN;
    }
    if (path.indexOf('matrix.') === 0) {
      var matrixVars = ctx.matrixVars;
      if (matrixVars !== null && matrixVars !== undefined) {
        var mv = ciGet(matrixVars, path.slice(7));
        return mv === MISSING ? null : mv;
      }
      return null;
    }
    for (var i = 0; i < RUNTIME_PREFIXES.length; i++) {
      if (path.indexOf(RUNTIME_PREFIXES[i]) === 0) {
        if (notes) {
          notes.push(path + ' is known only at run time — not simulated');
        }
        return UNKNOWN;
      }
    }
    if (path === 'github.token') return UNKNOWN;
    if (path.indexOf('github.') === 0) {
      var controlled = ctx.controlled || { names: [], prefixes: [] };
      var decided = controlled.names.indexOf(path) >= 0
        || controlled.prefixes.some(function (p) {
          return path.indexOf(p) === 0;
        });
      if (decided) return null;
      if (notes) {
        notes.push(path + ' is set by GitHub at run time — not simulated; '
          + 'add it in the variables panel to pin a value');
      }
      return UNKNOWN;
    }
    return null;
  }

  /* ---------------- expression evaluation ---------------- */

  function evalValue(ast, ctx, notes) {
    if (notes === undefined) notes = [];
    if (!ast) return true;
    if (ast.t === 'lit') return ast.value;
    if (ast.t === 'ctx') {
      if (ast.dynamic) {
        notes.push('dynamic index/filter in ' + ast.path + ' — not simulated');
        return UNKNOWN;
      }
      return lookupCtx(ast.path, ctx, notes);
    }
    var op = ast.op;
    if (op === 'opaque') {
      notes.push('expression could not be analyzed: ' + (ast.src || ''));
      return UNKNOWN;
    }
    if (op === 'invalid') {
      notes.push('GitHub rejects this expression: ' + (ast.src || ''));
      return UNKNOWN;
    }
    var i, t;
    if (op === 'and') {
      // && returns operand values, but a definitely-falsy operand decides
      // the outcome even past an unknown one — evaluate them all
      var andVals = ast.args.map(function (a) {
        return evalValue(a, ctx, notes);
      });
      var andTs = andVals.map(truthy);
      var andAgg = triAnd(andTs);
      if (andAgg === true) {
        return andVals.length ? andVals[andVals.length - 1] : true;
      }
      if (andAgg === false) {
        for (i = 0; i < andVals.length; i++) {
          if (andTs[i] === false) return andVals[i];
        }
        return false;
      }
      return UNKNOWN;
    }
    if (op === 'or') {
      var orVals = ast.args.map(function (a) {
        return evalValue(a, ctx, notes);
      });
      var orTs = orVals.map(truthy);
      var orAgg = triOr(orTs);
      if (orAgg === true) {
        for (i = 0; i < orVals.length; i++) {
          if (orTs[i] === true) return orVals[i];
        }
        return true;
      }
      if (orAgg === false) {
        return orVals.length ? orVals[orVals.length - 1] : false;
      }
      return UNKNOWN;
    }
    if (op === 'not') {
      t = truthy(evalValue(ast.arg, ctx, notes));
      return t === null ? UNKNOWN : !t;
    }
    if (op === 'cmp') {
      var left = evalValue(ast.left, ctx, notes);
      var right = evalValue(ast.right, ctx, notes);
      if (left === UNKNOWN || right === UNKNOWN) return UNKNOWN;
      var c = ast.cmp;
      if (c === '==') return looseEqual(left, right);
      if (c === '!=') return !looseEqual(left, right);
      var na = toNumber(left), nb = toNumber(right);
      if (na !== na || nb !== nb) return false;
      if (c === '<') return na < nb;
      if (c === '<=') return na <= nb;
      if (c === '>') return na > nb;
      if (c === '>=') return na >= nb;
      return UNKNOWN;
    }
    if (op === 'call') return evalCall(ast, ctx, notes);
    return UNKNOWN;
  }

  function evalCall(ast, ctx, notes) {
    var fn = ast.fn;
    if (fn === 'success' || fn === 'always' || fn === 'failure'
        || fn === 'cancelled') {
      var needsState = ctx.needsState === undefined ? true : ctx.needsState;
      if (fn === 'always') return true;
      if (fn === 'cancelled') return false;
      if (fn === 'success') {
        if (needsState === null) return UNKNOWN;
        if (needsState === false) return false;
        return true;
      }
      notes.push('failure() — the simulation assumes no dependency fails, '
        + 'so this is false; the job runs only in real failure scenarios');
      return false;
    }
    var args = (ast.args || []).map(function (a) {
      return evalValue(a, ctx, notes);
    });
    for (var i = 0; i < args.length; i++) {
      if (args[i] === UNKNOWN) return UNKNOWN;
    }
    if (fn === 'contains') {
      if (args.length < 2) return false;
      var hay = args[0], needle = args[1];
      if (Array.isArray(hay)) {
        return hay.some(function (h) { return looseEqual(h, needle); });
      }
      return toStr(hay).toLowerCase()
        .indexOf(toStr(needle).toLowerCase()) >= 0;
    }
    if (fn === 'startswith') {
      if (args.length < 2) return false;
      return toStr(args[0]).toLowerCase()
        .indexOf(toStr(args[1]).toLowerCase()) === 0;
    }
    if (fn === 'endswith') {
      if (args.length < 2) return false;
      var s0 = toStr(args[0]).toLowerCase(), s1 = toStr(args[1]).toLowerCase();
      return s1 === '' || s0.slice(-s1.length) === s1;
    }
    if (fn === 'format') {
      if (!args.length) return '';
      var out = toStr(args[0]).split('{{').join('\u0000')
        .split('}}').join('\u0001');
      for (var j = 1; j < args.length; j++) {
        out = out.split('{' + (j - 1) + '}').join(toStr(args[j]));
      }
      return out.split('\u0000').join('{').split('\u0001').join('}');
    }
    if (fn === 'join') {
      if (!args.length) return '';
      var sep = args.length > 1 ? toStr(args[1]) : ',';
      if (Array.isArray(args[0])) return args[0].map(toStr).join(sep);
      return toStr(args[0]);
    }
    if (fn === 'tojson') {
      return JSON.stringify(args.length ? args[0] : null);
    }
    if (fn === 'fromjson') {
      if (!args.length) return UNKNOWN;
      try {
        return JSON.parse(toStr(args[0]));
      } catch (e) {
        notes.push('fromJSON() argument is not valid JSON');
        return UNKNOWN;
      }
    }
    if (fn === 'hashfiles') {
      notes.push('hashFiles() depends on workspace content — not simulated');
      return UNKNOWN;
    }
    return UNKNOWN;
  }

  function evalCondition(ast, ctx, notes) {
    return truthy(evalValue(ast, ctx, notes));
  }

  function collectAstPaths(ast, out) {
    if (!ast || typeof ast !== 'object') return;
    if (ast.t === 'ctx') { out[ast.path] = true; return; }
    ['left', 'right', 'arg'].forEach(function (k) {
      if (ast[k]) collectAstPaths(ast[k], out);
    });
    (ast.args || []).forEach(function (a) { collectAstPaths(a, out); });
  }

  function displayValue(path, ctx) {
    var v = lookupCtx(path, ctx, null);
    if (v === UNKNOWN) return { name: path, value: null, runtime: true };
    return { name: path, value: v === null || v === undefined
             ? null : toStr(v), runtime: false };
  }

  function usesStatusFn(ast) {
    if (!ast || typeof ast !== 'object') return false;
    if (ast.op === 'call' && ['success', 'always', 'failure', 'cancelled']
        .indexOf(ast.fn) >= 0) {
      return true;
    }
    var keys = ['left', 'right', 'arg'];
    for (var i = 0; i < keys.length; i++) {
      if (ast[keys[i]] && usesStatusFn(ast[keys[i]])) return true;
    }
    return (ast.args || []).some(usesStatusFn);
  }

  /* ---------------- filter-pattern matching ---------------- */
  // GitHub's dialect: * (not /), ** (anything), ? and + quantify the
  // PRECEDING atom, [] classes, \ escapes, leading ! negates.

  function escapeRe(c) {
    return c.replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&');
  }

  function patternTranslate(pattern) {
    var atoms = [];
    var i = 0, n = pattern.length;
    while (i < n) {
      var c = pattern[i];
      if (c === '\\' && i + 1 < n) {
        atoms.push(escapeRe(pattern[i + 1]));
        i += 2;
      } else if (c === '*') {
        if (pattern.slice(i, i + 2) === '**') {
          atoms.push('.*');
          i += 2;
        } else {
          atoms.push('[^/]*');
          i += 1;
        }
      } else if (c === '?') {
        if (atoms.length) {
          atoms[atoms.length - 1] = '(?:' + atoms[atoms.length - 1] + ')?';
        }
        i += 1;
      } else if (c === '+') {
        if (atoms.length) {
          atoms[atoms.length - 1] = '(?:' + atoms[atoms.length - 1] + ')+';
        }
        i += 1;
      } else if (c === '[') {
        var j = pattern.indexOf(']', i + 1);
        if (j < 0) {
          atoms.push(escapeRe(c));
          i += 1;
        } else {
          atoms.push('[' + pattern.slice(i + 1, j) + ']');
          i = j + 1;
        }
      } else {
        atoms.push(escapeRe(c));
        i += 1;
      }
    }
    return atoms.join('');
  }

  function patternToRegExp(pattern) {
    try {
      return new RegExp('^' + patternTranslate(pattern) + '$');
    } catch (e) {
      return null;
    }
  }

  function matchPatternList(value, patterns) {
    var include = false, matchedPositive = false, unknown = false;
    for (var i = 0; i < patterns.length; i++) {
      var raw = patterns[i];
      var neg = raw.indexOf('!') === 0;
      var rx = patternToRegExp(neg ? raw.slice(1) : raw);
      if (rx === null) { unknown = true; continue; }
      if (rx.test(value)) {
        include = !neg;
        if (!neg) matchedPositive = true;
      }
    }
    if (unknown && !include) return null;
    return include && matchedPositive;
  }

  function matchPaths(patterns, changedFiles) {
    var unknown = false;
    for (var i = 0; i < changedFiles.length; i++) {
      var m = matchPatternList(changedFiles[i], patterns);
      if (m === true) return true;
      if (m === null) unknown = true;
    }
    return unknown ? null : false;
  }

  function matchPathsIgnore(patterns, changedFiles) {
    var unknown = false;
    for (var i = 0; i < changedFiles.length; i++) {
      var hit = false;
      for (var j = 0; j < patterns.length; j++) {
        var rx = patternToRegExp(patterns[j]);
        if (rx === null) { unknown = true; continue; }
        if (rx.test(changedFiles[i])) { hit = true; break; }
      }
      if (!hit) return true;
    }
    return unknown ? null : false;
  }

  /* ---------------- trigger (on:) evaluation ---------------- */

  var DEFAULT_PR_TYPES = ['opened', 'synchronize', 'reopened'];

  function evalTrigger(wf, event, candidate, config, trace) {
    var on = wf.on || {};
    var cfg = on[event.name];
    if (cfg === undefined || cfg === null) {
      trace.push({ rule: null, desc: 'on: has no ' + event.name + ' trigger',
                   verdict: 'no match' });
      return false;
    }
    var parts = [];

    function add(desc, verdictV, notes) {
      var verdict = verdictV === true ? 'matched'
        : verdictV === false ? 'no match' : 'unknown';
      trace.push({ rule: trace.length, desc: desc, verdict: verdict,
                   notes: notes || [] });
      parts.push(verdictV);
    }

    var b, bi, m;
    if (event.name === 'push') {
      var isTag = candidate.refType === 'tag';
      b = cfg.branches; bi = cfg.branches_ignore;
      var tg = cfg.tags, ti = cfg.tags_ignore;
      if (isTag) {
        if (tg !== undefined && tg !== null) {
          add('tags: ' + tg.join(', '),
              matchPatternList(candidate.ref, tg));
        } else if (ti !== undefined && ti !== null) {
          m = matchPatternList(candidate.ref, ti);
          add('tags-ignore: ' + ti.join(', '), triNot(m));
        } else if ((b !== undefined && b !== null)
                   || (bi !== undefined && bi !== null)) {
          add('push filters only branches — tag pushes do not trigger it',
              false);
        }
      } else {
        if (b !== undefined && b !== null) {
          add('branches: ' + b.join(', '),
              matchPatternList(candidate.ref, b));
        } else if (bi !== undefined && bi !== null) {
          m = matchPatternList(candidate.ref, bi);
          add('branches-ignore: ' + bi.join(', '), triNot(m));
        } else if ((tg !== undefined && tg !== null)
                   || (ti !== undefined && ti !== null)) {
          add('push filters only tags — branch pushes do not trigger it',
              false);
        }
      }
    } else if (event.name === 'pull_request'
               || event.name === 'pull_request_target') {
      var types = (cfg.types && cfg.types.length) ? cfg.types
        : DEFAULT_PR_TYPES;
      var action = event.action || 'synchronize';
      add('types: ' + types.join(', ')
          + (cfg.types && cfg.types.length ? '' : ' (default)'),
          types.indexOf(action) >= 0);
      var target = candidate.target || '';
      b = cfg.branches; bi = cfg.branches_ignore;
      if (b !== undefined && b !== null) {
        add("branches (the PR's BASE branch): " + b.join(', '),
            matchPatternList(target, b));
      } else if (bi !== undefined && bi !== null) {
        add("branches-ignore (the PR's BASE branch): " + bi.join(', '),
            triNot(matchPatternList(target, bi)));
      }
    } else if (event.name === 'release') {
      if (cfg.types && cfg.types.length) {
        add('types: ' + cfg.types.join(', '),
            cfg.types.indexOf(event.action || 'published') >= 0);
      }
    } else if (event.name === 'schedule') {
      var crons = cfg.crons || [];
      add('schedule: ' + (crons.length ? crons.join(', ') : '(no cron)'),
          crons.length > 0,
          crons.length
            ? ['each cron fires on its own schedule — shown together here']
            : []);
    } else if (event.name === 'workflow_dispatch') {
      add('workflow_dispatch — started manually for a chosen ref', true);
    }

    if (['push', 'pull_request', 'pull_request_target']
        .indexOf(event.name) >= 0) {
      var p = cfg.paths, pi = cfg.paths_ignore;
      var changed = config.changedFiles;
      if (p !== undefined && p !== null) {
        if (candidate.refType === 'tag') {
          add('paths: ' + p.join(', '), true,
              ['paths filters do not apply to tag pushes']);
        } else if (changed === 'all') {
          add('paths: ' + p.join(', '), true,
              ['assuming every paths: pattern matches']);
        } else if (changed === null || changed === undefined) {
          add('paths: ' + p.join(', '), null,
              ['depends on which files changed — fill in the changed-files '
               + 'list']);
        } else {
          add('paths: ' + p.join(', '), matchPaths(p, changed));
        }
      } else if (pi !== undefined && pi !== null) {
        if (candidate.refType === 'tag') {
          add('paths-ignore: ' + pi.join(', '), true,
              ['paths filters do not apply to tag pushes']);
        } else if (changed === 'all') {
          add('paths-ignore: ' + pi.join(', '), true,
              ['assuming some changed file escapes the ignore list']);
        } else if (changed === null || changed === undefined) {
          add('paths-ignore: ' + pi.join(', '), null,
              ['depends on which files changed — fill in the changed-files '
               + 'list']);
        } else {
          add('paths-ignore: ' + pi.join(', '),
              matchPathsIgnore(pi, changed));
        }
      }
    }

    if (!parts.length) {
      trace.push({ rule: null, desc: 'on: ' + event.name + ' (no filters)',
                   verdict: 'matched' });
      return true;
    }
    return triAnd(parts);
  }

  /* ---------------- world construction ---------------- */

  var CONTROLLED = {
    names: ['github.base_ref', 'github.head_ref', 'github.event.action',
            'github.event.created', 'github.event.deleted',
            'github.event.forced'],
    prefixes: ['github.event.pull_request.', 'github.event.release.',
               'github.event.head_commit.', 'github.event.inputs.',
               'inputs.']
  };

  function controlledFor(contexts) {
    var names = Object.keys(contexts);
    CONTROLLED.names.forEach(function (n) {
      if (names.indexOf(n) < 0) names.push(n);
    });
    return { names: names, prefixes: CONTROLLED.prefixes.slice() };
  }

  function isProtected(ref, refType, whatif, config) {
    if (refType === 'tag') return !!(config && config.tagProtected);
    return (whatif.protected_refs || []).indexOf(ref) >= 0;
  }

  function buildWorld(candidate, config, whatif, wf) {
    var msg = config.commitMessage || 'Update code';
    var source = candidate.source;
    var refType = candidate.refType;
    var ref = candidate.ref;
    var defaultBranch = whatif.default_branch;
    var wfName = wf.name || wf.file || candidate.workflow;

    var fullRef, refName, gitRefType, protectedRef;
    if (refType === 'pull_request') {
      fullRef = 'refs/pull/' + PR_NUMBER + '/merge';
      refName = PR_NUMBER + '/merge';
      gitRefType = 'branch';
      protectedRef = false;
    } else if (source === 'pull_request_target') {
      fullRef = 'refs/heads/' + (candidate.target || defaultBranch);
      refName = candidate.target || defaultBranch;
      gitRefType = 'branch';
      protectedRef = isProtected(refName, 'branch', whatif, config);
    } else if (refType === 'tag') {
      fullRef = 'refs/tags/' + ref;
      refName = ref;
      gitRefType = 'tag';
      protectedRef = isProtected(ref, 'tag', whatif, config);
    } else {
      fullRef = 'refs/heads/' + ref;
      refName = ref;
      gitRefType = 'branch';
      protectedRef = isProtected(ref, 'branch', whatif, config);
    }

    var contexts = {
      'github.event_name': source,
      'github.ref': fullRef,
      'github.ref_name': refName,
      'github.ref_type': gitRefType,
      'github.ref_protected': protectedRef,
      'github.sha': FAKE_SHA,
      'github.workflow': wfName,
      'github.default_branch': defaultBranch
    };
    var env = {
      CI: 'true',
      GITHUB_ACTIONS: 'true',
      GITHUB_EVENT_NAME: source,
      GITHUB_REF: fullRef,
      GITHUB_REF_NAME: refName,
      GITHUB_REF_TYPE: gitRefType,
      GITHUB_REF_PROTECTED: protectedRef ? 'true' : 'false',
      GITHUB_SHA: FAKE_SHA,
      GITHUB_WORKFLOW: wfName || ''
    };

    if (source === 'pull_request' || source === 'pull_request_target') {
      var target = candidate.target || defaultBranch;
      var head = candidate.headBranch || config.branch || 'feature/widget';
      contexts['github.base_ref'] = target;
      contexts['github.head_ref'] = head;
      contexts['github.event.action'] = candidate.action || 'synchronize';
      contexts['github.event.pull_request.number'] = Number(PR_NUMBER);
      contexts['github.event.pull_request.draft'] = !!config.draft;
      contexts['github.event.pull_request.base.ref'] = target;
      contexts['github.event.pull_request.head.ref'] = head;
      contexts['github.event.pull_request.title'] = 'Example pull request';
      contexts['github.event.pull_request.merged'] = false;
      env.GITHUB_BASE_REF = target;
      env.GITHUB_HEAD_REF = head;
    } else if (source === 'push') {
      contexts['github.event.created'] = !!config.newBranch
        && refType === 'branch';
      contexts['github.event.deleted'] = false;
      contexts['github.event.forced'] = false;
      var nl = msg.indexOf('\n');
      contexts['github.event.head_commit.message'] = msg;
      env.GITHUB_EVENT_NAME = 'push';
      contexts['github.event.head_commit.title'] =
        nl >= 0 ? msg.slice(0, nl) : msg;
    } else if (source === 'release') {
      contexts['github.event.action'] = candidate.action || 'published';
      contexts['github.event.release.tag_name'] = ref;
      contexts['github.event.release.draft'] = false;
      contexts['github.event.release.prerelease'] = false;
    } else if (source === 'workflow_dispatch') {
      contexts['github.event.action'] = null;
    }

    return { contexts: contexts, env: env };
  }

  function candidateInputs(wf, config, candidate) {
    var notes = [];
    var trig = candidate.childOf ? 'workflow_call' : 'workflow_dispatch';
    var declared = ((wf.on || {})[trig] || {}).inputs || {};
    var supplied = candidate.childOf ? (candidate.inputs || {})
      : (config.inputs || {});
    var out = {};
    Object.keys(declared).forEach(function (name) {
      var spec = declared[name];
      var raw;
      if (Object.prototype.hasOwnProperty.call(supplied, name)) {
        raw = supplied[name];
      } else {
        raw = spec['default'];
        if ((raw === null || raw === undefined) && spec.required) {
          notes.push("required input '" + name + "' has no value — GitHub "
            + 'refuses the ' + (candidate.childOf ? 'call' : 'dispatch')
            + ' without it');
        }
      }
      if (raw === null || raw === undefined) {
        out[name] = null;
      } else if (spec.type === 'boolean') {
        out[name] = typeof raw === 'boolean' ? raw
          : toStr(raw).toLowerCase() === 'true';
      } else if (spec.type === 'number') {
        out[name] = toNumber(raw);
      } else {
        out[name] = toStr(raw);
      }
    });
    Object.keys(supplied).forEach(function (name) {
      if (!Object.prototype.hasOwnProperty.call(out, name)) {
        out[name] = toStr(supplied[name]);
        notes.push("input '" + name + "' is not declared by the workflow — "
          + 'GitHub rejects it');
      }
    });
    return { map: out, notes: notes };
  }

  /* ---------------- job evaluation ---------------- */

  function mightRun(outcome) {
    if (!outcome) return false;
    if (outcome.state === 'conditional') return !!outcome.included;
    return ['runs', 'manual', 'delayed'].indexOf(outcome.state) >= 0;
  }

  function jobOutcomeRuns(jobWhatif) {
    return { included: true, state: 'runs', when: 'on_success',
             allow_failure: !!jobWhatif.continue_on_error,
             start_in: null, variables: null };
  }

  function evaluateJobOnce(jobWhatif, ctx, trace) {
    var needsState = ctx.needsState === undefined ? true : ctx.needsState;
    var ast = jobWhatif['if'];
    var hasStatus = ast ? usesStatusFn(ast) : false;

    var needsDesc = null;
    if (jobWhatif.needs && jobWhatif.needs.length) {
      needsDesc = 'needs: ' + jobWhatif.needs.map(function (n) {
        return n.job;
      }).join(', ');
    }

    if (ast === null || ast === undefined) {
      var cond0 = needsState;
      if (needsDesc) {
        trace.push({
          rule: 0, desc: needsDesc,
          verdict: cond0 === true ? 'matched'
            : cond0 === false ? 'no match' : 'unknown',
          notes: cond0 === true ? []
            : cond0 === false
              ? ['a needed job is ' + (ctx.needsBlockedBy || 'skipped')
                 + ' — this job is skipped too']
              : ['whether the needed jobs run is uncertain']
        });
      } else {
        trace.push({ rule: null, desc: 'no if: condition',
                     verdict: 'matched' });
      }
      if (cond0 === true) return jobOutcomeRuns(jobWhatif);
      if (cond0 === false) {
        return { included: false, state: 'skipped',
                 reason: 'a needed job is '
                 + (ctx.needsBlockedBy || 'skipped') };
      }
      return {
        state: 'conditional',
        condition: needsDesc || 'needs uncertain',
        conditionNotes: ['whether the needed jobs run is uncertain'],
        then: jobOutcomeRuns(jobWhatif),
        otherwise: { included: false, state: 'skipped' },
        included: true
      };
    }

    var notes = [];
    var desc = 'if: ' + (jobWhatif.raw_if || '');
    var v = evalCondition(ast, ctx, notes);
    var paths = {};
    collectAstPaths(ast, paths);
    var vars = Object.keys(paths).sort().map(function (p) {
      return displayValue(p, ctx);
    });

    var gateParts = [v];
    var gateNote = null;
    if (!hasStatus && jobWhatif.needs && jobWhatif.needs.length) {
      gateParts.push(needsState);
      if (needsState === false) {
        gateNote = 'a needed job is ' + (ctx.needsBlockedBy || 'skipped')
          + ' — without always(), this job is skipped too';
      } else if (needsState === null) {
        gateNote = 'whether the needed jobs run is uncertain';
      }
    }
    var cond = triAnd(gateParts);
    if (gateNote) notes.push(gateNote);

    trace.push({
      rule: 0, desc: desc,
      verdict: cond === true ? 'matched'
        : cond === false ? 'no match' : 'unknown',
      notes: notes, vars: vars
    });
    if (cond === true) return jobOutcomeRuns(jobWhatif);
    if (cond === false) {
      var reason = v === false ? 'if: condition is false'
        : 'a needed job is ' + (ctx.needsBlockedBy || 'skipped');
      return { included: false, state: 'skipped', reason: reason };
    }
    return {
      state: 'conditional',
      condition: desc,
      conditionNotes: notes,
      then: jobOutcomeRuns(jobWhatif),
      otherwise: { included: false, state: 'skipped' },
      included: true
    };
  }

  var MATRIX_ORDER = ['runs', 'conditional', 'skipped', 'not-added'];

  function evaluateJob(jobWhatif, ctx) {
    var trace = [];
    var par = jobWhatif.parallel;
    var out;
    if (!par) {
      out = evaluateJobOnce(jobWhatif, ctx, trace);
      out.trace = trace;
      return out;
    }
    if (par.kind === 'dynamic') {
      out = evaluateJobOnce(jobWhatif, ctx, trace);
      out.trace = trace;
      out.matrixCount = null;
      out.matrixDynamic = true;
      return out;
    }
    var per = [];
    (par.combos || []).forEach(function (c) {
      var subCtx = {};
      Object.keys(ctx).forEach(function (k) { subCtx[k] = ctx[k]; });
      subCtx.matrixVars = c.vars;
      var subTrace = [];
      per.push({ name: c.name,
                 outcome: evaluateJobOnce(jobWhatif, subCtx, subTrace),
                 trace: subTrace });
    });
    if (!per.length) {
      out = evaluateJobOnce(jobWhatif, ctx, trace);
      out.trace = trace;
      return out;
    }
    function order(state) {
      var i = MATRIX_ORDER.indexOf(state);
      return i < 0 ? MATRIX_ORDER.length : i;
    }
    var pick = per[0];
    per.forEach(function (p) {
      if (order(p.outcome.state) < order(pick.outcome.state)) pick = p;
    });
    var base = {};
    Object.keys(pick.outcome).forEach(function (k) {
      base[k] = pick.outcome[k];
    });
    base.trace = pick.trace;
    base.matrix = per.map(function (p) {
      return { name: p.name, state: p.outcome.state,
               when: p.outcome.when === undefined ? null : p.outcome.when };
    });
    base.matrixCount = per.length;
    base.matrixPartial = per.some(function (p) {
      return p.outcome.state !== per[0].outcome.state;
    });
    base.included = per.some(function (p) { return mightRun(p.outcome); });
    return base;
  }

  function topoOrder(jobs) {
    var byName = {};
    jobs.forEach(function (j) { byName[j.whatif.name] = j; });
    var done = {};
    var out = [];
    var remaining = jobs.slice();
    while (remaining.length) {
      var progressed = false;
      for (var i = 0; i < remaining.length; i++) {
        var j = remaining[i];
        var needs = j.whatif.needs || [];
        var ready = needs.every(function (n) {
          return done[n.job] || !byName[n.job];
        });
        if (ready) {
          out.push(j);
          done[j.whatif.name] = true;
          remaining.splice(i, 1);
          progressed = true;
          i -= 1;
        }
      }
      if (!progressed) {   // cycle — parser already flagged it
        out = out.concat(remaining);
        break;
      }
    }
    return out;
  }

  /* ---------------- candidate evaluation ---------------- */

  var MAX_CALL_DEPTH = 4;

  function jobIndex(report) {
    var jobs = [];
    (report.nodes || []).forEach(function (n) {
      var w = (n.annotations || {}).whatif;
      if (n.kind === 'job' && w) {
        jobs.push({ id: n.id, node: n, whatif: w });
      }
    });
    return jobs;
  }

  function wfLabel(wfKey, whatif) {
    var wf = (whatif.workflows || {})[wfKey] || {};
    return wf.name || wfKey;
  }

  function evaluateCandidate(candidate, allJobs, config, whatif, report) {
    var wfKey = candidate.workflow;
    var wf = (whatif.workflows || {})[wfKey] || {};
    var world = buildWorld(candidate, config, whatif, wf);
    var contexts = world.contexts, env = world.env;
    var inputsMap = {};
    var inputNotes = [];
    var isInputRun = !!candidate.childOf
      || candidate.source === 'workflow_dispatch';
    if (isInputRun) {
      var ci = candidateInputs(wf, config, candidate);
      inputsMap = ci.map;
      inputNotes = ci.notes;
      Object.keys(inputsMap).forEach(function (name) {
        contexts['inputs.' + name] = inputsMap[name];
        contexts['github.event.inputs.' + name] =
          inputsMap[name] === null ? null : toStr(inputsMap[name]);
      });
    }
    var controlled = controlledFor(contexts);

    var trace = [];
    var invalid = wf.invalid || [];
    var created, reason;
    if (invalid.length) {
      created = false;
      reason = 'invalid workflow file — GitHub refuses to run it: '
        + invalid.join('; ');
      trace.push({ rule: null, desc: reason, verdict: 'no match' });
    } else if (candidate.source === 'workflow_run'
               && candidate.triggerReason) {
      created = candidate.triggerVerdict === undefined
        ? true : candidate.triggerVerdict;
      reason = candidate.triggerReason || 'workflow_run trigger';
      trace.push({ rule: null, desc: reason,
                   verdict: created === true ? 'matched'
                     : created === null ? 'unknown' : 'no match' });
    } else if (candidate.childOf) {
      created = true;
      reason = "called by '" + (candidate.parentJob || '?')
        + "' — a reusable workflow runs whenever its caller does";
      trace.push({ rule: null, desc: reason, verdict: 'matched' });
    } else {
      var event = { name: candidate.source, action: candidate.action };
      created = evalTrigger(wf, event, candidate, config, trace);
      if (created === true) {
        reason = 'on: ' + candidate.source + ' matches this event';
      } else if (created === false) {
        reason = 'on: ' + candidate.source + ' filters exclude this event';
      } else {
        reason = 'whether on: ' + candidate.source
          + ' matches depends on unknown facts';
      }
    }

    var baseCtx = {
      contexts: contexts,
      envChain: wf.env || {},
      overrides: config.overrides || {},
      controlled: controlled,
      hasInputs: isInputRun
    };

    var jobs = allJobs.filter(function (j) {
      return j.whatif.workflow === wfKey;
    });
    var ordered = topoOrder(jobs);
    var results = {};
    var outcomesByName = {};
    ordered.forEach(function (job) {
      var jw = job.whatif;
      var needs = jw.needs || [];
      var needStates = [];
      var needResults = {};
      var blockedBy = null;
      needs.forEach(function (n) {
        var no = outcomesByName[n.job];
        if (!no) { needStates.push(null); return; }
        if (no.state === 'conditional') {
          needStates.push(null);
        } else if (mightRun(no)) {
          needStates.push(true);
          needResults[n.job] = 'success';
        } else {
          needStates.push(false);
          needResults[n.job] = 'skipped';
          blockedBy = blockedBy || ("skipped ('" + n.job + "')");
        }
      });
      var ctx = {};
      Object.keys(baseCtx).forEach(function (k) { ctx[k] = baseCtx[k]; });
      var chain = {};
      Object.keys(wf.env || {}).forEach(function (k) {
        chain[k] = (wf.env || {})[k];
      });
      Object.keys(jw.env || {}).forEach(function (k) {
        chain[k] = (jw.env || {})[k];
      });
      ctx.envChain = chain;
      ctx.needsState = needStates.length ? triAnd(needStates) : true;
      ctx.needsBlockedBy = blockedBy;
      ctx.needResults = needResults;
      var outcome = evaluateJob(jw, ctx);
      results[job.id] = outcome;
      outcomesByName[jw.name] = outcome;
    });

    var artifacts = { notes: [], errors: [], producers: [] };
    inputNotes.forEach(function (note) {
      artifacts.notes.push({ job: null, kind: 'inputs', message: note });
    });
    var conc = wf.concurrency;
    if (conc && conc.cancel_in_progress) {
      artifacts.notes.push({
        job: null, kind: 'concurrency',
        message: "concurrency group '" + (conc.group || '')
          + "' with cancel-in-progress — a newer run of this workflow "
          + 'cancels this one' });
    }
    ordered.forEach(function (job) {
      var jw = job.whatif;
      var envc = jw.environment;
      if (envc && envc.name && mightRun(results[job.id])) {
        artifacts.notes.push({
          job: job.id, kind: 'environment',
          message: '"' + jw.name + '" deploys to environment "' + envc.name
            + '" — protection rules there (required reviewers, wait '
            + 'timers) can hold it for approval; configured in repository '
            + 'settings, not visible here' });
      }
    });

    var children = [];
    var lineage = candidate.lineage || [];
    ordered.forEach(function (job) {
      var jw = job.whatif;
      var uses = jw.uses;
      if (!uses || !mightRun(results[job.id]) || created === false) return;
      if (uses.kind === 'local'
          && (whatif.workflows || {})[uses.workflow]) {
        var target = uses.workflow;
        if (target === wfKey || lineage.indexOf(target) >= 0) {
          artifacts.notes.push({
            job: job.id, kind: 'downstream',
            message: '"' + jw.name + '" re-calls ' + target
              + ' which is already in this chain — cycle not expanded' });
          return;
        }
        if (lineage.length >= MAX_CALL_DEPTH) {
          artifacts.notes.push({
            job: job.id, kind: 'downstream',
            message: 'reusable workflows nested deeper than '
              + MAX_CALL_DEPTH + " levels are not expanded (GitHub's own "
              + 'limit)' });
          return;
        }
        var childCandidate = {
          id: candidate.id + '>' + job.id + '>' + target,
          source: candidate.source,
          refType: candidate.refType,
          ref: candidate.ref,
          target: candidate.target === undefined ? null : candidate.target,
          action: candidate.action,
          headBranch: candidate.headBranch,
          label: 'Called workflow: ' + target,
          workflow: target,
          childOf: target,
          parentJob: job.id,
          parentConditional: results[job.id].state === 'conditional',
          inputs: uses['with'] || {},
          lineage: lineage.concat([wfKey])
        };
        var childResult = evaluateCandidate(childCandidate, allJobs, config,
                                            whatif, report);
        children.push(childResult);
        if (childResult.created === false) {
          artifacts.errors.push({
            job: job.id, target: target, kind: 'trigger',
            message: '"' + jw.name + '" calls ' + target
              + ' which cannot run (' + childResult.reason
              + ') — GitHub fails the caller job' });
        }
      } else if (uses.kind === 'remote') {
        artifacts.notes.push({
          job: job.id, kind: 'downstream',
          message: '"' + jw.name + '" calls the reusable workflow '
            + uses.raw + ' in another repository — its config is not '
            + 'available offline' });
      } else if (uses.kind === 'local') {
        artifacts.notes.push({
          job: job.id, kind: 'downstream',
          message: '"' + jw.name + '" calls ' + uses.raw
            + ' which is not in this report' });
      }
    });

    var creationFails = created !== false
      && artifacts.errors.some(function (e) { return e.kind !== 'trigger'; });

    var forwardedVars = {};
    Object.keys(inputsMap).forEach(function (k) {
      forwardedVars[k] = inputsMap[k] === null ? null : toStr(inputsMap[k]);
    });

    return {
      id: candidate.id, label: candidate.label,
      source: candidate.source, ref: candidate.ref,
      refType: candidate.refType,
      target: candidate.target === undefined ? null : candidate.target,
      workflow: wfKey,
      workflowName: wf.name === undefined ? null : wf.name,
      childOf: candidate.childOf === undefined ? null : candidate.childOf,
      parentJob: candidate.parentJob === undefined
        ? null : candidate.parentJob,
      parentConditional: candidate.parentConditional || false,
      created: created, reason: reason, creationFails: creationFails,
      workflowTrace: trace,
      workflowVariables: {},
      forwardedVars: forwardedVars,
      env: env, controlled: controlled,
      jobs: results, jobOrder: ordered.map(function (j) { return j.id; }),
      artifacts: artifacts, children: children,
      separate: candidate.separate || false
    };
  }

  /* ---------------- candidate enumeration ---------------- */

  function buildCandidates(config, whatif) {
    var scenario = config.scenario;
    var branch = config.branch || whatif.default_branch;
    var tag = config.tag || 'v1.0.0';
    var target = config.target || whatif.default_branch;
    var workflows = whatif.workflows || {};

    var events = [];
    if (scenario === 'push_branch') {
      events.push({ name: 'push', refType: 'branch', ref: branch });
      if (config.openPR) {
        var action = config.prAction || 'synchronize';
        events.push({ name: 'pull_request', refType: 'pull_request',
                      ref: branch, target: target, action: action,
                      headBranch: branch });
        events.push({ name: 'pull_request_target', refType: 'branch',
                      ref: branch, target: target, action: action,
                      headBranch: branch });
      }
    } else if (scenario === 'push_tag') {
      events.push({ name: 'push', refType: 'tag', ref: tag });
    } else if (scenario === 'pr') {
      var prAction = config.prAction || 'opened';
      events.push({ name: 'pull_request', refType: 'pull_request',
                    ref: branch, target: target, action: prAction,
                    headBranch: branch });
      events.push({ name: 'pull_request_target', refType: 'branch',
                    ref: branch, target: target, action: prAction,
                    headBranch: branch });
    } else if (scenario === 'schedule') {
      events.push({ name: 'schedule', refType: 'branch',
                    ref: whatif.default_branch });
    } else if (scenario === 'workflow_dispatch') {
      var isTag = config.refKind === 'tag';
      events.push({ name: 'workflow_dispatch',
                    refType: isTag ? 'tag' : 'branch',
                    ref: isTag ? tag : branch, separate: true });
    } else if (scenario === 'release') {
      events.push({ name: 'release', refType: 'tag', ref: tag,
                    action: config.releaseAction || 'published' });
    } else {
      events.push({ name: 'push', refType: 'branch', ref: branch });
    }

    var out = [];
    events.forEach(function (event) {
      Object.keys(workflows).forEach(function (wfKey) {
        var wf = workflows[wfKey];
        var on = wf.on || {};
        if (!(event.name in on)) return;
        if (scenario === 'workflow_dispatch' && config.dispatchWorkflow
            && wfKey !== config.dispatchWorkflow) {
          return;
        }
        var label = (wf.name || wfKey) + ' — ' + event.name;
        if (event.action) label += ' (' + event.action + ')';
        out.push({
          id: wfKey + ':' + event.name,
          workflow: wfKey,
          source: event.name,
          refType: event.refType,
          ref: event.ref,
          target: event.target === undefined ? null : event.target,
          action: event.action,
          headBranch: event.headBranch,
          label: label,
          childOf: null,
          separate: !!event.separate
        });
      });
    });
    return out;
  }

  function workflowRunCascade(candidates, allJobs, config, whatif, report,
                              depth) {
    if (depth === undefined) depth = 0;
    if (depth >= 3) return;
    var workflows = whatif.workflows || {};
    candidates.forEach(function (cand) {
      if (cand.created === false) return;
      var triggerName = cand.workflowName || wfLabel(cand.workflow, whatif);
      Object.keys(workflows).forEach(function (wfKey) {
        var wf = workflows[wfKey];
        var wr = (wf.on || {}).workflow_run;
        if (wr === undefined || wr === null || wfKey === cand.workflow) {
          return;
        }
        var names = wr.workflows || [];
        if (names.indexOf(triggerName) < 0
            && names.indexOf(cand.workflow) < 0) {
          return;
        }
        var types = (wr.types && wr.types.length) ? wr.types : ['completed'];
        if (types.indexOf('completed') < 0 && types.indexOf('requested') < 0
            && types.indexOf('in_progress') < 0) {
          return;
        }
        var verdict = true;
        var note = "runs when workflow '" + triggerName + "' completes; "
          + 'always uses the workflow version on the default branch';
        var b = wr.branches, bi = wr.branches_ignore;
        if (cand.refType === 'branch' && b !== undefined && b !== null) {
          verdict = matchPatternList(cand.ref, b);
        } else if (cand.refType === 'branch' && bi !== undefined
                   && bi !== null) {
          verdict = triNot(matchPatternList(cand.ref, bi));
        } else if (cand.refType !== 'branch'
                   && ((b !== undefined && b !== null)
                       || (bi !== undefined && bi !== null))) {
          verdict = null;
        }
        var child = {
          id: cand.id + '>workflow_run>' + wfKey,
          workflow: wfKey,
          source: 'workflow_run',
          refType: 'branch',
          ref: whatif.default_branch,
          target: null,
          label: wfLabel(wfKey, whatif) + ' — workflow_run',
          childOf: wfKey,
          parentJob: null,
          parentConditional: cand.created === null,
          triggerVerdict: verdict,
          triggerReason: note,
          lineage: [cand.workflow]
        };
        var result = evaluateCandidate(child, allJobs, config, whatif,
                                       report);
        cand.children.push(result);
        workflowRunCascade([result], allJobs, config, whatif, report,
                           depth + 1);
      });
    });
  }

  /* ---------------- event evaluation (entry point) ---------------- */

  var WHATIF_VERSION = 1;

  function evaluateEvent(report, config) {
    var whatif = (report.annotations || {}).whatif;
    if (!whatif || whatif.provider !== 'github') return null;
    if (whatif.version !== WHATIF_VERSION) {
      throw new Error('report carries what-if program version '
        + whatif.version + '; this evaluator speaks version '
        + WHATIF_VERSION + ' — regenerate the report with this pipeview');
    }
    var allJobs = jobIndex(report);
    var candidates = buildCandidates(config, whatif).map(function (c) {
      return evaluateCandidate(c, allJobs, config, whatif, report);
    });
    workflowRunCascade(candidates, allJobs, config, whatif, report);

    var duplicates = [];
    var simultaneous = candidates.filter(function (c) {
      return !c.separate;
    });
    if (simultaneous.length > 1) {
      var seen = {};
      simultaneous.forEach(function (c) {
        if (c.created === false || c.creationFails) return;
        c.jobOrder.forEach(function (jobId) {
          if (mightRun(c.jobs[jobId])) {
            (seen[jobId] = seen[jobId] || []).push(c.id);
          }
        });
      });
      Object.keys(seen).forEach(function (jobId) {
        if (seen[jobId].length > 1) {
          var entry = { job: jobId, candidates: seen[jobId] };
          var c0 = null;
          for (var i = 0; i < simultaneous.length; i++) {
            if (simultaneous[i].jobs[jobId]) { c0 = simultaneous[i]; break; }
          }
          if (c0 && c0.jobs[jobId].matrixCount) {
            entry.instances = c0.jobs[jobId].matrixCount;
          }
          duplicates.push(entry);
        }
      });
    }

    return {
      candidates: candidates,
      duplicates: duplicates,
      crossPipelineArtifacts: false,
      lint: whatif.lint || [],
      fatal: whatif.fatal || []
    };
  }

  /* ---------------- static expression-variable scan ---------------- */

  // Context paths every job if: references, plus their GITHUB_* env twins
  // — feeds the Variables tab's "referenced in conditions" ranking.
  function collectExpressionVariables(report) {
    var found = {};
    function fromAst(ast) {
      var paths = {};
      collectAstPaths(ast, paths);
      Object.keys(paths).forEach(function (p) {
        found[p] = true;
        var m = /^github\.([a-z_]+)$/.exec(p);
        if (m) found['GITHUB_' + m[1].toUpperCase()] = true;
        if (p.indexOf('env.') === 0) found[p.slice(4)] = true;
        if (p.indexOf('matrix.') === 0) found[p.slice(7)] = true;
      });
    }
    (report.nodes || []).forEach(function (n) {
      var w = n.annotations && n.annotations.whatif;
      if (w && w['if']) fromAst(w['if']);
    });
    return Object.keys(found);
  }

  /* ---------------- output: describe / listing / diff ---------------- */

  var SCENARIO_LABEL = {
    push_branch: 'Push to a branch', push_tag: 'Push a new tag',
    pr: 'Pull request', schedule: 'Scheduled run',
    workflow_dispatch: 'Manual dispatch (workflow_dispatch)',
    release: 'Release'
  };

  function describeConfig(config) {
    var parts = [SCENARIO_LABEL[config.scenario] || config.scenario];
    var onTag = config.scenario === 'push_tag'
      || config.scenario === 'release'
      || (config.scenario === 'workflow_dispatch'
          && config.refKind === 'tag');
    if (config.scenario === 'pr') {
      parts.push('PR ' + config.branch + ' → ' + (config.target || 'main')
        + (config.draft ? ' (draft)' : '')
        + (config.prAction && config.prAction !== 'opened'
           ? ' [' + config.prAction + ']' : ''));
    } else if (onTag) {
      parts.push('tag ' + (config.tag || 'v1.0.0')
        + (config.scenario === 'release'
           ? ' [' + (config.releaseAction || 'published') + ']' : ''));
    } else if (config.scenario === 'schedule') {
      parts.push('on the default branch');
    } else {
      parts.push('branch ' + config.branch
        + (config.newBranch ? ' (new)' : ''));
      if (config.openPR) {
        parts.push('an open PR uses this branch as source (also fires '
          + 'pull_request)');
      }
    }
    if (config.scenario === 'workflow_dispatch') {
      if (config.dispatchWorkflow) {
        parts.push('workflow ' + config.dispatchWorkflow);
      }
      var inp = config.inputs || {};
      var inpKeys = Object.keys(inp);
      if (inpKeys.length) {
        parts.push('inputs: ' + inpKeys.sort().map(function (n) {
          return n + '=' + inp[n];
        }).join(', '));
      }
    }
    if (config.changedFiles === 'all') {
      parts.push('assume all paths changed');
    } else if (Array.isArray(config.changedFiles)) {
      parts.push('changed: ' + config.changedFiles.join(', '));
    }
    var overrides = config.overrides || {};
    var names = Object.keys(overrides).sort();
    if (names.length) {
      parts.push('variables: ' + names.map(function (n) {
        return n + '="' + overrides[n] + '"';
      }).join(', '));
    }
    return parts.join(' — ');
  }

  function outcomeText(out) {
    if (!out) return 'not evaluated';
    var t;
    if (out.state === 'runs') {
      t = out.allow_failure ? 'runs (continue-on-error)' : 'runs';
    } else if (out.state === 'conditional') {
      t = 'depends: ' + (out.condition || 'unknown condition');
    } else if (out.state === 'skipped') {
      t = 'skipped' + (out.reason ? ' (' + out.reason + ')' : '');
    } else {
      t = 'not added';
    }
    if (out.matrixDynamic) {
      t += ' [matrix from an expression — instance count unknown]';
    } else if (out.matrixPartial && out.matrix) {
      var r = out.matrix.filter(function (m) {
        return ['runs', 'conditional'].indexOf(m.state) >= 0;
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
      stages[n.id] = w ? (w.stage || '') : '';
    });
    return { names: names, stages: stages, stageOrder: [] };
  }

  // No stages in GitHub Actions: listings keep evaluation order (needs
  // topology, then definition order) — jobOrder already carries it.
  function padTo(s, width) {
    while (s.length < width) s += ' ';
    return s;
  }

  function candHeading(cand) {
    var ref = cand.refType === 'pull_request' && cand.target
      ? cand.ref + ' → ' + cand.target : cand.ref;
    return cand.label + ' (' + cand.source + ' on ' + ref + ')';
  }

  function textSummary(report, result, config) {
    var meta = jobMeta(report);
    var lines = ['what-if: ' + describeConfig(config)];
    if (!result.candidates.length) {
      lines.push('');
      lines.push('(no workflow subscribes to this event)');
      return lines.join('\n');
    }
    var separate = result.candidates.length > 1
      && result.candidates.every(function (c) { return c.separate; });
    if (separate) {
      lines.push('');
      lines.push('each workflow below is dispatched individually — one run '
        + 'per manual dispatch, never simultaneous');
    }
    function section(cand, depth) {
      var pad = padTo('', depth * 2);
      lines.push('');
      var head = pad + (depth ? 'called workflow: ' : '') + candHeading(cand);
      if (cand.created === false) {
        lines.push(head + ' — does not run ('
          + (cand.reason || 'no reason recorded') + ')');
        return;
      }
      if (cand.creationFails) {
        head += ' — FAILS to start (see report)';
      } else if (cand.created === null) {
        head += ' — run uncertain: ' + (cand.reason || 'depends');
      }
      lines.push(head);
      var ids = cand.jobOrder.filter(function (id) {
        return mightRun(cand.jobs[id]);
      });
      if (!ids.length) {
        lines.push(pad + '  (no jobs would run)');
      } else {
        var nameW = 0;
        ids.forEach(function (id) {
          nameW = Math.max(nameW,
                           Math.min((meta.names[id] || id).length, 40));
        });
        ids.forEach(function (id) {
          lines.push(pad + '  ' + padTo(meta.names[id] || id, nameW)
            + '  ' + outcomeText(cand.jobs[id]));
        });
      }
      (cand.children || []).forEach(function (child) {
        section(child, depth + 1);
      });
    }
    result.candidates.forEach(function (cand) { section(cand, 0); });
    if (result.duplicates && result.duplicates.length) {
      lines.push('');
      lines.push('duplicate jobs (run in more than one workflow run for '
        + 'this single event): ' + result.duplicates.map(function (d) {
          return (meta.stages[d.job] ? meta.stages[d.job] + ': ' : '')
            + (meta.names[d.job] || d.job);
        }).join(', '));
    }
    return lines.join('\n');
  }

  /* ---------------- delta ---------------- */

  // Top-level candidates pair by workflow plus event family: push /
  // schedule / dispatch / release runs of one workflow compare against
  // each other (that IS the trigger delta), pull_request-family runs pair
  // separately. Children pair by (parent, caller job, called workflow).
  function candMatchKey(cand, parentKey) {
    if (!parentKey) {
      var fam = (cand.source === 'pull_request'
                 || cand.source === 'pull_request_target')
        ? cand.source : 'main';
      return cand.workflow + '#' + fam;
    }
    return parentKey + ' > ' + (cand.parentJob || '?') + ' > '
      + (cand.childOf || '?');
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

  var STATE_RANK = { runs: 4, conditional: 1 };

  function effectiveOutcomes(result) {
    var eff = {};
    if (!result) return eff;
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
    return cand.jobOrder.filter(function (id) {
      return mightRun(cand.jobs[id]);
    });
  }

  function diffEvents(resultA, resultB) {
    var effA = effectiveOutcomes(resultA);
    var effB = effectiveOutcomes(resultB);
    var order = [];
    function pushOrder(result) {
      flattenCandidates(result).forEach(function (entry) {
        entry.cand.jobOrder.forEach(function (id) {
          if ((effA[id] || effB[id]) && order.indexOf(id) < 0) {
            order.push(id);
          }
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
    flattenCandidates(resultA).forEach(function (e) {
      byKeyA[e.key] = e.cand;
    });
    flattenCandidates(resultB).forEach(function (e) {
      byKeyB[e.key] = e.cand;
    });
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
          : outcomeText(a.jobs[id]) === outcomeText(b.jobs[id])
            ? 'same' : 'changed';
      });
      return { key: k, a: a, b: b, ids: ids, deltas: deltas };
    });

    return { jobs: jobs, order: order, counts: counts, pairs: pairs };
  }

  function pipesNote(eff) {
    if (!eff || !eff.pipes.length) return '';
    return '  [' + eff.pipes.join(', ') + ']';
  }

  function pipelineStatusLines(diff) {
    var status = function (cand, side) {
      if (!cand) return null;
      if (cand.creationFails) return side + ': FAILS to start';
      if (cand.created === false) {
        return side + ': does not run (' + (cand.reason || 'suppressed')
          + ')';
      }
      if (cand.created === null) return side + ': run uncertain';
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
      lines.push('    ' + name
        + (flags.length ? ' — ' + flags.join('; ') : ''));
    });
    return noteworthy ? ['  workflow runs:'].concat(lines) : [];
  }

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
    ['added', 'removed', 'changed', 'same'].forEach(function (kind) {
      var block = diff.order.filter(function (id) {
        return diff.jobs[id].delta === kind;
      });
      if (!block.length) return;
      lines.push('');
      block.forEach(function (id) {
        var j = diff.jobs[id];
        var name = padTo(meta.names[id] || id, nameW);
        if (kind === 'added') {
          lines.push('+ ' + name + '  ' + outcomeText(j.b.outcome)
            + pipesNote(j.b));
        } else if (kind === 'removed') {
          lines.push('- ' + name + '  was: ' + outcomeText(j.a.outcome)
            + pipesNote(j.a));
        } else if (kind === 'changed') {
          var ta = outcomeText(j.a.outcome), tb = outcomeText(j.b.outcome);
          lines.push('~ ' + name + '  ' + (ta === tb
            ? tb + '  [in ' + j.a.pipes.length + ' run'
              + (j.a.pipes.length > 1 ? 's' : '') + ' → '
              + j.b.pipes.length + ' run'
              + (j.b.pipes.length > 1 ? 's' : '') + ']'
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

  /* ---------------- markdown listing + delta ---------------- */

  function mdCell(s) {
    return String(s).replace(/\n/g, ' ').replace(/\|/g, '\\|');
  }

  function mdCode(s) {
    s = String(s);
    return s.indexOf('`') >= 0 ? '`` ' + s + ' ``' : '`' + s + '`';
  }

  function markdownSummary(report, result, config) {
    var meta = jobMeta(report);
    var lines = ['**What-if:** ' + mdCell(describeConfig(config))];
    function section(cand, depth) {
      lines.push('');
      lines.push('### ' + (depth ? 'Called workflow: ' : '')
        + candHeading(cand));
      if (cand.created === false) {
        lines.push('');
        lines.push('*Does not run — '
          + mdCell(cand.reason || 'no reason recorded') + '.*');
        return;
      }
      if (cand.creationFails) {
        lines.push('');
        lines.push('> ⚠ FAILS to start (see the report)');
      } else if (cand.created === null) {
        lines.push('');
        lines.push('> ⚠ run uncertain: ' + mdCell(cand.reason || 'depends'));
      }
      lines.push('');
      var ids = cand.jobOrder.filter(function (id) {
        return mightRun(cand.jobs[id]);
      });
      if (!ids.length) {
        lines.push('*(no jobs would run)*');
      } else {
        lines.push('| Job | Workflow | Verdict |');
        lines.push('|---|---|---|');
        ids.forEach(function (id) {
          lines.push('| ' + mdCell(mdCode(meta.names[id] || id))
            + ' | ' + mdCell(meta.stages[id] || '')
            + ' | ' + mdCell(outcomeText(cand.jobs[id])) + ' |');
        });
      }
      (cand.children || []).forEach(function (child) {
        section(child, depth + 1);
      });
    }
    result.candidates.forEach(function (cand) { section(cand, 0); });
    if (result.duplicates && result.duplicates.length) {
      lines.push('');
      lines.push('**Duplicates** (run in more than one workflow run for '
        + 'this single event): '
        + result.duplicates.map(function (d) {
          return mdCell(mdCode(meta.names[d.job] || d.job));
        }).join(', '));
    }
    return lines.join('\n');
  }

  function markdownDiff(report, diff, labelA, labelB) {
    var meta = jobMeta(report);
    var c = diff.counts;
    var lines = [
      '**What-if delta** — ' + c.added + ' added, ' + c.removed
        + ' removed, ' + c.changed + ' changed, ' + c.same + ' unchanged',
      '',
      '- baseline: ' + mdCell(labelA),
      '- current: ' + mdCell(labelB)
    ];
    var status = pipelineStatusLines(diff);
    if (status.length) {
      lines.push('');
      lines.push('**Workflow runs:**');
      status.slice(1).forEach(function (l) {
        lines.push('- ' + mdCell(l.replace(/^\s+/, '')));
      });
    }
    if (!diff.order.length) {
      lines.push('');
      lines.push('*(no jobs would run in either configuration)*');
      return lines.join('\n');
    }
    lines.push('');
    lines.push('| Δ | Job | Verdict |');
    lines.push('|---|---|---|');
    var symbol = { added: '+', removed: '-', changed: '~', same: '=' };
    ['added', 'removed', 'changed', 'same'].forEach(function (kind) {
      diff.order.forEach(function (id) {
        var j = diff.jobs[id];
        if (j.delta !== kind) return;
        var verdict;
        if (kind === 'added') {
          verdict = outcomeText(j.b.outcome) + pipesNote(j.b);
        } else if (kind === 'removed') {
          verdict = 'was: ' + outcomeText(j.a.outcome) + pipesNote(j.a);
        } else if (kind === 'changed') {
          var ta = outcomeText(j.a.outcome), tb = outcomeText(j.b.outcome);
          verdict = ta === tb
            ? tb + '  [in ' + j.a.pipes.length + ' run'
              + (j.a.pipes.length > 1 ? 's' : '') + ' → '
              + j.b.pipes.length + ' run'
              + (j.b.pipes.length > 1 ? 's' : '') + ']'
            : ta + ' → ' + tb;
        } else {
          verdict = outcomeText(j.b.outcome);
        }
        lines.push('| ' + symbol[kind] + ' | '
          + mdCell(mdCode(meta.names[id] || id)) + ' | '
          + mdCell(verdict) + ' |');
      });
    });
    return lines.join('\n');
  }

  /* ---------------- scenario export (YAML) ---------------- */

  function yamlScalar(v) {
    if (v === true) return 'true';
    if (v === false) return 'false';
    var s = String(v);
    if (s.indexOf('\n') >= 0) {
      return '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
        .replace(/\n/g, '\\n') + '"';
    }
    if (/^[A-Za-z_][A-Za-z0-9_.\/-]*$/.test(s)
        && !/^(true|false|null|yes|no|on|off)$/i.test(s)) {
      return s;
    }
    return "'" + s.replace(/'/g, "''") + "'";
  }

  function yamlFlowMap(obj) {
    var keys = Object.keys(obj).sort();
    if (!keys.length) return '{}';
    return '{ ' + keys.map(function (k) {
      return yamlScalar(k) + ': ' + yamlScalar(obj[k]);
    }).join(', ') + ' }';
  }

  function scenarioId(config) {
    var base;
    switch (config.scenario) {
      case 'push_branch': base = 'push-' + (config.branch || 'branch'); break;
      case 'push_tag': base = 'tag-' + (config.tag || 'v1.0.0'); break;
      case 'pr': base = 'pr-' + (config.branch || 'source'); break;
      case 'release': base = 'release-' + (config.tag || 'v1.0.0'); break;
      case 'workflow_dispatch':
        base = 'dispatch-' + (config.dispatchWorkflow || 'all'); break;
      default:
        base = config.scenario;
    }
    var id = base.toLowerCase().replace(/[^a-z0-9-]+/g, '-')
      .replace(/-{2,}/g, '-').replace(/^-+|-+$/g, '');
    return id || 'scenario';
  }

  function scenarioYaml(config) {
    var s = config.scenario || 'push_branch';
    var lines = [
      '# pipeview trigger-docs scenario — exported from the What-If tab.',
      '# Paste the `- id:` block into your scenarios file, or keep this '
        + 'as one.',
      'version: 1',
      'scenarios:',
      '  - id: ' + scenarioId(config) + '   # rename to taste',
      '    event: ' + s
    ];
    function add(key, value) { lines.push('    ' + key + ': ' + value); }
    if (s === 'push_tag' || s === 'release'
        || (s === 'workflow_dispatch' && config.refKind === 'tag')) {
      if (s === 'workflow_dispatch') add('ref_kind', 'tag');
      add('tag', yamlScalar(config.tag || 'v1.0.0'));
    } else if (config.branch && s !== 'schedule') {
      add('branch', yamlScalar(config.branch));
    }
    if (s === 'push_branch' && config.newBranch) add('new_branch', 'true');
    if (s === 'push_branch' && config.openPR) {
      var pr = {};
      if (config.target) pr.target = config.target;
      if (config.draft) pr.draft = true;
      if (config.prAction && config.prAction !== 'synchronize') {
        pr.action = config.prAction;
      }
      add('open_pr', yamlFlowMap(pr));
    }
    if (s === 'pr') {
      if (config.target) add('target', yamlScalar(config.target));
      if (config.draft) add('draft', 'true');
      if (config.prAction && config.prAction !== 'opened') {
        add('pr_action', config.prAction);
      }
    }
    if (s === 'workflow_dispatch') {
      if (config.dispatchWorkflow) {
        add('workflow', yamlScalar(config.dispatchWorkflow));
      }
      var inp = config.inputs || {};
      if (Object.keys(inp).length) add('inputs', yamlFlowMap(inp));
    }
    if (s === 'release' && config.releaseAction
        && config.releaseAction !== 'published') {
      add('release_action', config.releaseAction);
    }
    var changed = config.changedFiles;
    if (changed === 'all') {
      add('changed_files', 'all');
    } else if (Array.isArray(changed)) {
      add('changed_files', '[' + changed.map(yamlScalar).join(', ') + ']');
    }
    if (config.commitMessage && config.commitMessage !== 'Update code') {
      add('commit_message', yamlScalar(config.commitMessage));
    }
    var overrides = config.overrides || {};
    if (Object.keys(overrides).length) {
      add('variables', yamlFlowMap(overrides));
    }
    return lines.join('\n') + '\n';
  }

  return {
    evalExpr: evalValue,
    evalCondition: evalCondition,
    patternToRegExp: patternToRegExp,
    matchPatternList: matchPatternList,
    buildCandidates: buildCandidates,
    evaluateEvent: evaluateEvent,
    mightRun: mightRun,
    collectExpressionVariables: collectExpressionVariables,
    describeConfig: describeConfig,
    outcomeText: outcomeText,
    textSummary: textSummary,
    diffEvents: diffEvents,
    textDiff: textDiff,
    markdownSummary: markdownSummary,
    markdownDiff: markdownDiff,
    scenarioYaml: scenarioYaml
  };
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = PipeviewWhatIfGH;
}
