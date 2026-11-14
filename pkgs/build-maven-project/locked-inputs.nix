# SPDX-License-Identifier: MIT
# Private dependency preparation shared by the pure builder and local CLI.
# No project runtime is selected or realized here.
{
  lib,
  stdenvNoCC,
  runCommand,
  writeText,
  unzip,
  python3,
  fetchurl,
  purifier,
}:
{
  src,
  lockFile ? src + "/maven-purifier.lock.json",
  checkDefinition ? true,
  fetchers ? { },
  projectDependencies ? purifier.lockedProjectInputs {
    inputs = (builtins.fromJSON (builtins.readFile lockFile)).projectInputs or [ ];
    system = stdenvNoCC.hostPlatform.system;
  },
}:
let
  # The lock in memory (lock.nix), which checkLock fails on when the
  # lockfile is stale, tampered or malformed.
  checked = purifier.checkLock (
    import ../../lib-overlays/default/lock.nix {
      inherit src lockFile checkDefinition;
    }
  );
  lock = checked.maven;
  # The downloads of this system; those only development needs are left
  # out of a sandboxed build.
  entries = purifier.entriesFor stdenvNoCC.hostPlatform.system checked;
  buildEntries = builtins.filter (e: !(e.development or false)) entries;

  # A local repository holds copies: Maven 3.10 treats a file as absent
  # when its real path differs from its path in the repository.
  mkRepository =
    name: entries:
    purifier.tree (
      {
        inherit
          name
          entries
          runCommand
          fetchurl
          ;
        link = false;
      }
      // fetchers
    );
  lockedRepository = mkRepository "maven-repository" buildEntries;
  provided = purifier.jvmProjections {
    repository = null;
    dependencies = projectDependencies;
    inherit runCommand python3;
    name = "maven-provided-dependencies";
  };
  repository =
    if projectDependencies == [ ] then
      lockedRepository
    else
      runCommand "maven-composed-repository" { } ''
        mkdir $out
        cp -r --no-preserve=mode ${lockedRepository}/. $out/
        ${python3}/bin/python ${./project-inputs.py} merge \
          ${purifier.jvmRepositoryTool} ${provided.maven} "$out"
      '';
  lockedDevelopmentRepository = mkRepository "maven-development-repository" entries;
  developmentRepository =
    if projectDependencies == [ ] then
      lockedDevelopmentRepository
    else
      runCommand "maven-composed-development-repository" { } ''
        mkdir $out
        cp -r --no-preserve=mode ${lockedDevelopmentRepository}/. $out/
        ${python3}/bin/python ${./project-inputs.py} merge \
          ${purifier.jvmRepositoryTool} ${provided.maven} "$out"
      '';

  maven = stdenvNoCC.mkDerivation {
    pname = "maven";
    inherit (lock) version;
    src = "${repository}/${lock.distribution}";
    nativeBuildInputs = lib.optional (lib.hasSuffix ".zip" lock.distribution) unzip;
    dontConfigure = true;
    dontBuild = true;
    installPhase = ''
      runHook preInstall
      cp -r . $out
      runHook postInstall
    '';
  };

  # The settings the lock run used, without servers and proxies, so that the
  # build sees the repository and mirror ids of the lock run.
  settings = writeText "settings.xml" (
    if lock.settings or null != null then lock.settings else "<settings/>\n"
  );

  prefetch = runCommand "maven-purifier-local-inputs" { } ''
    mkdir $out
    ln -s ${developmentRepository} $out/repository
    ln -s ${maven} $out/maven
    ln -s ${settings} $out/settings.xml
    ${lib.optionalString (projectDependencies != [ ]) ''
      ${python3}/bin/python ${./project-inputs.py} properties \
        ${provided.maven.catalog} "$out/project-inputs.properties"
    ''}
  '';
in
{
  inherit
    checked
    lock
    entries
    buildEntries
    provided
    repository
    developmentRepository
    maven
    settings
    prefetch
    ;
}
