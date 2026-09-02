## 2026-08-30 - Python's PyYAML parser speedup using CSafeLoader
**Learning:** PyYAML parser uses the pure-Python `SafeLoader` by default when parsing YAML, which can be over 10 times slower than the C-based implementation (`CSafeLoader`), causing a bottleneck during parsing in heavily-relied-upon sections of code.
**Action:** Always check if `CSafeLoader` is available and use it in favor of `SafeLoader` for parsing when doing so does not compromise safety.

## 2026-08-31 - Always use `yaml.load(text, Loader=SafeLoader)` for PyYAML parsing
**Learning:** `yaml.safe_load(text)` always uses the pure-Python `SafeLoader`, even if you try to alias `CSafeLoader as SafeLoader` in imports. To actually use the fast C-based implementation (`CSafeLoader`), you must use `yaml.load(text, Loader=SafeLoader)` after attempting to import `CSafeLoader as SafeLoader`.
**Action:** When migrating to `CSafeLoader`, replacing `yaml.safe_load(text)` with `yaml.load(text, Loader=SafeLoader)` is the only way to actually engage the C-level performance improvements.
