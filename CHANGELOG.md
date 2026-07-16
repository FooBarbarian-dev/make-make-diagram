# Changelog

## 0.1.0

Initial release.

- GNU Make static parser: targets, prerequisites, pattern rules, order-only
  dependencies, variables (all operators), `include`/`-include` chains,
  `$(MAKE) -C` recursion detection, `ifeq`/`ifdef` conditionals (both
  branches captured), `define` blocks, `##` docstrings.
- GitLab CI parser: stages, jobs, `needs:` DAG, `extends:` chains with
  template inheritance, `include:` (local resolved, `project:`/`remote:`/
  `template:`/`component:` as ghost nodes), `rules:`/`when: manual`,
  `only:`/`except:`, `trigger:`, job and global variables.
- Optional Make enrichment pass (`make -pqn`) for resolved variable values
  and computed default goal, with `--no-enrich` opt-out.
- Single-file HTML report with four interactive views: Dependency Graph
  (dagre layout, pan/zoom, focus mode, edge filters, legend), Task Catalog,
  Variable Explorer (event timelines, clickable `$(VAR)` references), and
  File Map (include tree with diagnostics).
- Export formats: HTML, JSON model, DOT, Mermaid, SVG.
- Fully offline: no network access at generation time, no CDN references in
  output. Enforced by automated test.
- Ghost nodes for unresolvable references, with diagnostics.
- `python -m pipeview` support for running from a checkout.
