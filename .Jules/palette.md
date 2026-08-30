## 2025-02-12 - Added ARIA labels to dynamically generated variable rows
**Learning:** Found dynamically generated inputs and icon-only buttons ("×") in `report.html` lacking proper semantic association or textual description. Adding `aria-label` directly into the string concatenation ensures accessibility for inputs mapped over arrays without needing complex ID-linking (`aria-labelledby`).
**Action:** Always verify dynamically generated interactive elements (especially in lists/loops) have self-contained accessible names since static labels are harder to correctly associate with dynamic IDs.
