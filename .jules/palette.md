## 2024-06-25 - Search Autocomplete Accessibility
**Learning:** Purely visual toggle of active combobox items creates barriers for screen readers; they need explicit state tracking (e.g., `aria-activedescendant` or `aria-selected` bindings) matched with `aria-controls`.
**Action:** When creating or maintaining autocomplete dropdowns, always ensure standard ARIA attributes (`role="combobox"`, `aria-expanded`, `aria-controls`, `aria-activedescendant`, `aria-selected`) dynamically mirror the active DOM state.

## 2024-06-25 - Dynamic Form Labels
**Learning:** Headings placed near inputs visually associate the form field, but are completely missed by screen readers which rely on `for`/`id` bindings.
**Action:** Always replace descriptive headings directly above inputs with semantic `<label for="id">` elements, or add explicit `aria-label` attributes when layout prohibits standard labels.
