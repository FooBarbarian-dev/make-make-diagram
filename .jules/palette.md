## 2024-05-18 - Input Labeling in Dynamic Forms
**Learning:** HTML templates generating dynamic forms need careful tracking of implicit label-input relationships. Forms appending new sections via JS concatenation frequently miss explicit `<label>` element links (`for` and `id`) and `aria-label`s.
**Action:** Always verify `aria-label` or explicitly linked `for` and `id` tags in `input` and `select` fields rendered via Javascript HTML string generation.

## 2026-09-02 - Keyboard shortcut hints in inputs
**Learning:** Adding a keyboard shortcut hint inside a search input (like `( / )` inside a placeholder) can be confusing and visually non-distinct. It's better to use a dedicated `<kbd>` element styled as a distinct badge and positioned absolutely within the input wrapper to make it clear it's a keyboard shortcut.
**Action:** When adding keyboard shortcut hints to text inputs, use a dedicated `<kbd>` element, position it absolutely within the input wrapper, and ensure the input has enough padding to prevent text from overlapping the hint.
