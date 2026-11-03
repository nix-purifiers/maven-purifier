# SPDX-License-Identifier: MIT
{

  description = "maven-purifier: lock the downloads of a Maven build and build it in the Nix sandbox";

  inputs = {
    caisson.url = "github:nix-caisson/caisson";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # The library comes from the nixpkgs.lib mirror. The package sets
    # take nixpkgs.
    nixpkgs-lib.url = "github:nix-community/nixpkgs.lib";
    flake-parts.url = "github:hercules-ci/flake-parts";
    # The shared library of the nix-purifiers, which fetches the locked
    # downloads and lays them out.
    purifier.url = "github:nix-purifiers/purifier";
  };

  outputs =
    inputs@{ caisson, purifier, ... }:
    let
      lib = caisson.lib.caisson.mkLib {
        inherit (caisson.lib.caisson.pins.flake inputs) sources root;
        name = "maven-purifier";
        systems = [
          "x86_64-linux"
          "aarch64-linux"
          "aarch64-darwin"
        ];
        projects = {
          inherit caisson purifier;
        };

        configs = lib: lib.caisson.mkModules ./configs;

        pkgSets = lib: lib.caisson.nixpkgs.mkConfigurations { };

        libOverlays = lib: lib.caisson.mkLibOverlays ./lib-overlays;
        pkgOverlays = lib: lib.caisson.mkPkgOverlays ./pkg-overlays;
      };
    in
    lib.caisson.flake-parts.mkTopConfiguration { };

}
