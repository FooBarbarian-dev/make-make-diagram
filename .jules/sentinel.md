## 2025-02-28 - Missing Single Quote Escape in HTML Escaping Function
**Vulnerability:** The `esc(s)` utility function in `pipeview/render/templates/report.html` and `pipeview/render/templates/rollup.html` failed to escape single quotes `'`. This could lead to XSS vulnerabilities when values are interpolated into single-quoted HTML attributes.
**Learning:** Manual HTML escaping implementations sometimes miss single quotes.
**Prevention:** Always ensure `esc` functions or equivalents include `.replace(/'/g, '&#39;')` alongside other typical HTML character escapes like `<`, `>`, `"`, and `&`.
