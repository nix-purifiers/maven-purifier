# SPDX-License-Identifier: MIT
#
# The definition hash of a Maven build, as `maven-purifier lock` computes it
# (definition_hash in cli/maven_purifier.py): the SHA-256 of a text that
# names the lock goals and each definition file with the SHA-256 of its
# contents. The definition files are every regular file named pom.xml, and
# every regular file under .mvn/ at the build root; the walk skips the
# directories that never belong to a build definition and symbolic links.
#
# `maven-purifier prefetch` appends lines of its own to .mvn/maven.config,
# from the line in `marker` on. They point Maven at the prefetched files of
# this machine and take no part in the build definition, so the hash of
# .mvn/maven.config covers only the lines before them.
#
# `src` must be a path, since the walk reads the directory at evaluation
# time.
{ src, goals }:
let
  marker = import ./maven-config-marker.nix;

  skipped = [
    "target"
    ".git"
    ".maven-purifier"
    ".idea"
    "node_modules"
  ];
  skippedDir = name: builtins.elem name skipped || builtins.substring 0 6 name == "result";

  # The definition files under `dir`, as paths relative to the build root.
  # `rel` is the relative path of `dir`, "" at the root; `inMvn` says whether
  # `dir` is .mvn/ at the build root or below it.
  walk =
    dir: rel: inMvn:
    let
      entries = builtins.readDir dir;
      relOf = name: if rel == "" then name else "${rel}/${name}";
    in
    builtins.concatLists (
      map (
        name:
        let
          type = entries.${name};
        in
        if type == "directory" then
          if skippedDir name then
            [ ]
          else
            walk (dir + "/${name}") (relOf name) (inMvn || (rel == "" && name == ".mvn"))
        else if type == "regular" && (name == "pom.xml" || inMvn) then
          [ (relOf name) ]
        else
          [ ]
      ) (builtins.attrNames entries)
    );

  # The SHA-256 of a definition file. For .mvn/maven.config, the build's own
  # lines joined by newlines: those before the lines of `maven-purifier
  # prefetch`, less trailing empty ones (build_config_lines in
  # cli/maven_purifier.py).
  fileHash =
    rel:
    let
      path = src + "/${rel}";
      lines = builtins.filter builtins.isString (builtins.split "\n" (builtins.readFile path));
      before =
        list:
        if list == [ ] || builtins.head list == marker then
          [ ]
        else
          [ (builtins.head list) ] ++ before (builtins.tail list);
      dropTrailingEmpty =
        list: if list != [ ] && lib.last list == "" then dropTrailingEmpty (lib.init list) else list;
      lib = {
        last = list: builtins.elemAt list (builtins.length list - 1);
        init = list: builtins.genList (builtins.elemAt list) (builtins.length list - 1);
      };
    in
    if rel == ".mvn/maven.config" then
      builtins.hashString "sha256" (builtins.concatStringsSep "\n" (dropTrailingEmpty (before lines)))
    else
      builtins.hashFile "sha256" path;

  # An absent config and one containing only generated prefetch lines
  # describe the same build.
  files = builtins.filter (
    rel: rel != ".mvn/maven.config" || fileHash rel != builtins.hashString "sha256" ""
  ) (builtins.sort builtins.lessThan (walk src "" false));
  lines = [
    "goals ${builtins.concatStringsSep " " goals}"
  ]
  ++ map (rel: "${rel} ${fileHash rel}") files;
in
"sha256:${builtins.hashString "sha256" (builtins.concatStringsSep "\n" lines + "\n")}"
