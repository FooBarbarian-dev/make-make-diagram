## 2024-05-18 - Input Labeling in Dynamic Forms
**Learning:** HTML templates generating dynamic forms need careful tracking of implicit label-input relationships. Forms appending new sections via JS concatenation frequently miss explicit `<label>` element links (`for` and `id`) and `aria-label`s.
**Action:** Always verify `aria-label` or explicitly linked `for` and `id` tags in `input` and `select` fields rendered via Javascript HTML string generation.
