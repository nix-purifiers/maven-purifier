#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Locks every fixture again with the maven-purifier of this checkout. Run it
# from the repository root; it needs the network. The `sources` fixture
# takes a SNAPSHOT from tests/snapshot-repository, which this script serves
# on localhost:18080 while it locks.
set -euo pipefail
root=$(pwd)
# Maven Central's files come through Google's copy of it, which spares
# Central's rate limit; the lock still records the repo1.maven.org URLs.
export MAVEN_PURIFIER_CENTRAL_MIRROR=${MAVEN_PURIFIER_CENTRAL_MIRROR-https://maven-central.storage-download.googleapis.com/maven2/}
cli=$(nix build --no-link --print-out-paths "$root#cli")
python=$(nix build --no-link --print-out-paths 'nixpkgs#python3')

"$python/bin/python3" -m http.server 18080 --bind 127.0.0.1 \
  --directory "$root/tests/snapshot-repository" >/dev/null 2>&1 &
server=$!
trap 'kill $server' EXIT

lock() {
  local fixture=$1
  shift
  echo "locking $fixture" >&2
  (cd "$root/tests/fixtures/$fixture" && "$cli/bin/maven-purifier" lock --force "$@")
}

lock multi clean deploy
lock wrapper
lock bundle
lock sources --settings settings.xml clean install -Pextra
# The external fixture keeps its lockfile outside its source.
lock external/src --lockfile ../maven-purifier.lock.json
