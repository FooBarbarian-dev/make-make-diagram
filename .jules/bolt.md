## 2026-08-30 - Python's PyYAML parser speedup using CSafeLoader
**Learning:** PyYAML parser uses the pure-Python `SafeLoader` by default when parsing YAML, which can be over 10 times slower than the C-based implementation (`CSafeLoader`), causing a bottleneck during parsing in heavily-relied-upon sections of code.
**Action:** Always check if `CSafeLoader` is available and use it in favor of `SafeLoader` for parsing when doing so does not compromise safety.
