#!/usr/bin/env bash
# Install the two pinned scanners into one folder.
#
#   bash scripts/install_scanners.sh [DEST]
#
# DEST defaults to $RUNNER_TEMP/scanners in GitHub Actions, else ./.scanners.
# Cisco skill-scanner gets its own venv, installed from a hash-locked list in
# scanners/. Betterleaks comes from its GitHub release and is checked against
# the SHA-256 pinned below. In Actions the binary paths go to $GITHUB_ENV;
# elsewhere they are printed as export lines.
#
# Supported: Linux x86_64 (the Actions runner) and macOS arm64 (local runs).
# Needs python3 >= 3.12 (or set PYTHON), curl and tar.

set -euo pipefail

SKILL_SCANNER_VERSION="2.3.1"
BETTERLEAKS_VERSION="1.9.0"
# From https://github.com/betterleaks/betterleaks/releases/download/v1.9.0/checksums.txt
BETTERLEAKS_SHA256_LINUX_X64="f8b185a39ffcece2a1ca82bf3a4e7435cd81963ffd16b7a9128daf75f35f6de7"
BETTERLEAKS_SHA256_DARWIN_ARM64="83dd7eaab13d44a1e8e347a17064cdef153a1478db473276d41b38d55613d3f2"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:-${RUNNER_TEMP:+$RUNNER_TEMP/scanners}}"
dest="${dest:-$repo_root/.scanners}"
python="${PYTHON:-python3}"

"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || { echo "Python 3.12 or newer is required (set PYTHON)" >&2; exit 1; }

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) bl_asset="linux_x64"; bl_sha="$BETTERLEAKS_SHA256_LINUX_X64" ;;
  Darwin-arm64) bl_asset="darwin_arm64"; bl_sha="$BETTERLEAKS_SHA256_DARWIN_ARM64" ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$dest"

install_venv() {  # $1 = venv name, $2 = lock file
  "$python" -m venv "$dest/$1"
  "$dest/$1/bin/python" -m pip install --quiet --disable-pip-version-check --no-input \
    --require-hashes --no-deps -r "$repo_root/scanners/$2"
}

echo "Installing Cisco skill-scanner $SKILL_SCANNER_VERSION"
install_venv skill-scanner cisco-skill-scanner.txt

echo "Installing Betterleaks $BETTERLEAKS_VERSION ($bl_asset)"
tarball="$dest/betterleaks_${BETTERLEAKS_VERSION}_${bl_asset}.tar.gz"
curl -fsSL --retry 3 --proto '=https' -o "$tarball" \
  "https://github.com/betterleaks/betterleaks/releases/download/v${BETTERLEAKS_VERSION}/betterleaks_${BETTERLEAKS_VERSION}_${bl_asset}.tar.gz"
actual_sha="$("$python" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$tarball")"
if [ "$actual_sha" != "$bl_sha" ]; then
  echo "Betterleaks checksum mismatch: got $actual_sha" >&2
  exit 1
fi
mkdir -p "$dest/betterleaks"
tar -xzf "$tarball" -C "$dest/betterleaks" betterleaks
rm -f "$tarball"

skill_scanner_bin="$dest/skill-scanner/bin/skill-scanner"
betterleaks_bin="$dest/betterleaks/betterleaks"

# Fail now, not mid-scan, if a pin did not take.
check_version() {  # $1 = label, $2 = expected text, $3 = actual output
  case "$3" in
    *"$2"*) ;;
    *) echo "$1 version check failed: expected $2, got $3" >&2; exit 1 ;;
  esac
}
check_version skill-scanner "$SKILL_SCANNER_VERSION" "$("$skill_scanner_bin" --version)"
check_version Betterleaks "$BETTERLEAKS_VERSION" "$("$betterleaks_bin" version)"

if [ -n "${GITHUB_ENV:-}" ]; then
  {
    echo "SKILL_SCANNER_BIN=$skill_scanner_bin"
    echo "BETTERLEAKS_BIN=$betterleaks_bin"
  } >> "$GITHUB_ENV"
else
  echo "export SKILL_SCANNER_BIN='$skill_scanner_bin'"
  echo "export BETTERLEAKS_BIN='$betterleaks_bin'"
fi
echo "Scanners ready in $dest"
