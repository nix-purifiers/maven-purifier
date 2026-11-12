# SPDX-License-Identifier: MIT
# Development versions identify immutable source contents, including local edits.
# Release packaging supplies its selected version explicitly through callPackage.
let
  source = builtins.path {
    path = ../.;
    name = "maven-purifier-source";
    filter = path: _: builtins.baseNameOf path != ".git";
  };
in
"0.0.0-dev.${builtins.substring 0 12 (builtins.baseNameOf source)}"
