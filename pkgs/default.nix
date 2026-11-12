# SPDX-License-Identifier: MIT
#
# The `maven-purifier` scope, which the default package overlay adds to the
# package set as `pkgs.maven-purifier`. `callPackage` is the scope's own, so
# `pkgs.maven-purifier.overrideScope` replaces what the packages share.
{ callPackage, lib, ... }:
let
  version = import ./version.nix;
  extension = callPackage ./extension { inherit version; };
in
{
  # The Maven core extension that records what a lock run downloads.
  inherit extension;
  # The `maven-purifier` command, with `lock` and `mvn`.
  cli = callPackage ./cli { inherit version extension; };
  # The builder (nix/lib/build-maven-project.nix), on the purifier library.
  buildMavenProject = callPackage ../nix/lib/build-maven-project.nix {
    inherit (lib) purifier;
    inherit extension;
  };
}
