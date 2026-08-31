## 2025-02-27 - [Improve HTML escaping to prevent XSS]
**Vulnerability:** The HTML escaping function `_escape_html` replaced `&`, `<`, and `>`, but did not escape double quotes (`"`) and single quotes (`'`).
**Learning:** This is a common pattern in custom escaping functions. Failing to escape quotes can lead to XSS vulnerabilities if user-controlled input is placed within HTML attributes.
**Prevention:** Always use comprehensive escaping functions that handle all HTML special characters (`&`, `<`, `>`, `"`, `'`).
