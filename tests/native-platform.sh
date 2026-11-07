#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
uname -a
nix --version
case "$RUNNER_OS/$RUNNER_ARCH" in
  Linux/X64) expected=x86_64-linux ;;
  Linux/ARM64) expected=aarch64-linux ;;
  macOS/ARM64) expected=aarch64-darwin ;;
  *) echo "Unexpected runner $RUNNER_OS/$RUNNER_ARCH" >&2; exit 1 ;;
esac
actual=$(nix eval --impure --raw --expr builtins.currentSystem)
echo "Native Nix system: $actual"
test "$actual" = "$expected"
sandbox=$(nix config show sandbox)
echo "Nix sandbox: $sandbox"
test "$sandbox" = true
