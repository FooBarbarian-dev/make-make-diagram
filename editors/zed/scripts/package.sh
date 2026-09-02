#!/usr/bin/env bash
# Package the Zed extension the way Zed installs it.
#
#   scripts/package.sh [--no-build] [--expect-version X.Y.Z] [--out DIR]
#
# Zed has no notion of a bare .wasm file. What it loads — from the
# extension registry and from "Install Dev Extension" alike — is a
# directory whose root holds extension.toml next to the compiled module
# named exactly extension.wasm. This script stages that directory and
# archives it twice (a .zip for Windows hands, a .tar.gz for everyone
# else), then checks the result has the layout Zed expects.
#
# Deliberately NOT in the archive: Cargo.toml and src/. A Cargo.toml makes
# Zed recompile the extension with the user's Rust toolchain on install;
# without it the prebuilt extension.wasm is used as is, so the archive
# installs with no toolchain on Windows, macOS, Linux, and WSL.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

build=1
expect_version=""
out="dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) build=0 ;;
    --expect-version) expect_version="$2"; shift ;;
    --out) out="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

target=wasm32-wasip2

# --- manifest checks (tomllib is stdlib from Python 3.11) -------------------
read -r ext_id ext_version crate_name crate_version < <(python3 - <<'PY'
import tomllib
m = tomllib.load(open("extension.toml", "rb"))
for key in ("id", "name", "version", "schema_version"):
    if key not in m:
        raise SystemExit(f"extension.toml: missing required field {key!r}")
c = tomllib.load(open("Cargo.toml", "rb"))["package"]
print(m["id"], m["version"], c["name"], c["version"])
PY
)
if [ "$ext_version" != "$crate_version" ]; then
  echo "version mismatch: extension.toml $ext_version vs Cargo.toml $crate_version" >&2
  exit 1
fi
if [ -n "$expect_version" ] && [ "$ext_version" != "$expect_version" ]; then
  echo "version mismatch: extension.toml $ext_version vs expected $expect_version" >&2
  exit 1
fi

# --- build ------------------------------------------------------------------
if [ "$build" = 1 ]; then
  cargo build --release --target "$target"
fi
# cargo names the artifact after the crate, dashes normalized to underscores
wasm="target/$target/release/${crate_name//-/_}.wasm"
if [ ! -f "$wasm" ]; then
  echo "built wasm not found at $wasm" >&2
  exit 1
fi

# --- stage + archive --------------------------------------------------------
name="pipeview-zed-v${ext_version}"
stage="$out/stage"
rm -rf "$stage"
mkdir -p "$stage/$ext_id" "$out"
cp extension.toml LICENSE README.md CHANGELOG.md "$stage/$ext_id/"
cp "$wasm" "$stage/$ext_id/extension.wasm"
rm -f "$out/$name.zip" "$out/$name.tar.gz"
tar -czf "$out/$name.tar.gz" -C "$stage" "$ext_id"
python3 - "$stage" "$ext_id" "$out/$name.zip" <<'PY'
import os, sys, zipfile
stage, ext_id, dest = sys.argv[1:]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _dirs, files in os.walk(os.path.join(stage, ext_id)):
        for f in sorted(files):
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, stage))
PY
rm -rf "$stage"

# --- verify the layout Zed expects ------------------------------------------
python3 - "$ext_id" "$out/$name.tar.gz" "$out/$name.zip" <<'PY'
import sys, tarfile, zipfile
ext_id, tgz, zp = sys.argv[1:]
with tarfile.open(tgz) as t:
    tar_names = {m.name.removeprefix("./") for m in t.getmembers()}
with zipfile.ZipFile(zp) as z:
    zip_names = set(z.namelist())
for label, names in (("tar.gz", tar_names), ("zip", zip_names)):
    for required in ("extension.toml", "extension.wasm"):
        if f"{ext_id}/{required}" not in names:
            raise SystemExit(f"{label}: missing {ext_id}/{required}")
    if any(n.endswith("Cargo.toml") for n in names):
        raise SystemExit(f"{label}: must not carry Cargo.toml (forces a rebuild)")
    print(f"{label}: {sorted(n for n in names if not n.endswith('/'))}")
PY
echo "packaged: $out/$name.zip $out/$name.tar.gz"
