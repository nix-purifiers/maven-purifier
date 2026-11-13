# SPDX-License-Identifier: MIT
#
# The `maven-purifier` library namespace. `readLock` reads a maven-purifier
# lockfile into the lock in memory of the purifier contract
# (lib-overlays/default/lock.nix).
{ ... }:
{

  overlay = final: prev: {
    maven-purifier = (prev.maven-purifier or { }) // {
      readLock = import ./lock.nix;
    };
  };

}
