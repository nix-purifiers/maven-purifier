# SPDX-License-Identifier: MIT
#
# buildMavenProject: the sandboxed build of a Maven build, from the lockfile
# that `maven-purifier lock` writes.
#
# The file is a package for callPackage: the first argument set is what it
# takes from the package set, and the result is the builder. The
# maven-purifier overlay (`overlays.default`) adds it to the scope
# `pkgs.maven-purifier`, so a package written for callPackage takes the
# scope by name:
#
#   { maven-purifier }:
#   (maven-purifier.buildMavenProject { src = ./.; }).packages.app
#
# `maven-purifier.buildMavenProject.override` replaces what the builder
# takes, and `maven-purifier.overrideScope` replaces what the scope shares.
# The builder returns
#
#   packages.<id>   the artifacts of the module with artifactId <id>, in
#                   Maven layout; `artifacts.<key>` names each file, where
#                   <key> is <artifactId>[-<classifier>].<extension>
#   tests           the build with its tests, whose output holds the test
#                   reports
#   release         every artifact the build installs, in Maven layout,
#                   built with the tests skipped; `release.publish` is the
#                   script that `maven-purifier publish` runs to deploy it
#   fetches         the download entries the build fetches
#   prefetch        what `maven-purifier prefetch` keeps alive: the local
#                   repository of every locked download, development
#                   ones included, the Maven distribution, the JDK and the
#                   recorded settings
#   repository      the local repository of the downloads the build needs
#   maven           the Maven distribution the lock run used
#   jdk             the JDK that runs Maven
#   lock            the lock in memory, as readLock (lock.nix) reads it
arguments@{
  lib,
  maven,
  jdk_headless,
  stdenvNoCC,
  runCommand,
  writeText,
  writeShellScriptBin,
  unzip,
  python3,
  # The fetcher of the locked downloads; `fetchers.fetchurl` replaces it
  # for one build.
  fetchurl,
  # The JDKs a build can run on; the default JDK of a build is the headless
  # one of the Java major version its lock was made with.
  javaPackages,
  # The purifier library, which the overlay passes.
  purifier,
  extension,
}:
{
  # The root of the Maven build.
  src,
  # The fetchers that replace nixpkgs' own, passed to the purifier library
  # unchanged; a company mirror or credentials go into `fetchers.fetchurl`.
  fetchers ? { },
  # Nix-built JVM dependencies; their typed repository projections supply
  # artifacts and the actual native versions without download-lock entries.
  projectDependencies ? purifier.lockedProjectInputs {
    inputs = (builtins.fromJSON (builtins.readFile lockFile)).projectInputs or [ ];
    system = stdenvNoCC.hostPlatform.system;
  },
  # The lockfile. It may lie outside `src`, for a build whose source does
  # not hold one; a lockfile given here replaces any copy in `src`.
  lockFile ? src + "/maven-purifier.lock.json",
  # The JDK that runs Maven. The default is nixpkgs' headless JDK of the
  # Java major version the lock was made with.
  jdk ? null,
  # Further arguments for Maven.
  mavenFlags ? [ ],
  # Filters the build root before it is copied into the store, as the
  # `filter` of builtins.path. The default leaves out build outputs, the
  # state of `maven-purifier prefetch` and editor state.
  srcFilter ? null,
  # Compares the build definition with the one the lock was made from, and
  # fails at evaluation time when they differ. The comparison reads `src`
  # at evaluation time, so it applies only when `src` is a path; a `src`
  # that a derivation produces is not compared.
  checkDefinition ? true,
}:
let
  inputs =
    (import ./locked-inputs.nix {
      inherit
        lib
        stdenvNoCC
        runCommand
        writeText
        unzip
        python3
        fetchurl
        purifier
        ;
    })
      {
        inherit
          src
          lockFile
          checkDefinition
          fetchers
          projectDependencies
          ;
      };
  inherit (inputs)
    checked
    lock
    entries
    buildEntries
    provided
    repository
    developmentRepository
    maven
    settings
    ;

  defaultSrcFilter =
    path: type:
    let
      name = baseNameOf path;
    in
    !(
      (type == "directory" && (name == "target" || name == ".maven-purifier"))
      || name == ".maven-purifier-pom.xml"
      || name == ".git"
      || name == ".idea"
      || (type == "symlink" && dirOf path == toString src && lib.hasPrefix "result" name)
    );

  filteredSrc = builtins.path {
    path = src;
    name = "maven-source";
    filter = if srcFilter != null then srcFilter else defaultSrcFilter;
  };

  javaMajor = lock.javaMajor;
  defaultJdk =
    javaPackages.compiler."openjdk${javaMajor}".headless
      or (throw "maven-purifier: javaPackages has no openjdk${javaMajor} for the Java ${lock.javaMajor} the lock was made with; pass `jdk`.");
  jdk' = if jdk != null then jdk else defaultJdk;

  # The goals of a build: the locked goals, with `deploy` replaced by
  # `install`. Deploying is the job of `release.publish`, which deploys what
  # the build made.
  buildGoals = map (goal: if goal == "deploy" then "install" else goal) lock.goals;

  # A minimal Java 8 plugin, separate from the Maven 3/4 core adapters, can
  # read the evaluated reactor on Maven 2 as well as current Maven releases.
  runtimeRecorder =
    runCommand "maven-runtime-recorder"
      {
        nativeBuildInputs = [ arguments.jdk_headless ];
      }
      ''
        mkdir -p classes/META-INF/maven
        cp ${../../extension/src/io/github/nixpurifiers/maven/LegacyRecorderMojo.java} LegacyRecorderMojo.java
        javac --release 8 -Xlint:-options -d classes -cp "${arguments.maven}/maven/lib/*" LegacyRecorderMojo.java
        cp ${../../extension/resources/META-INF/maven/plugin.xml} classes/META-INF/maven/plugin.xml
        # Maven 4 populates the legacy project artifact view on demand.
        sed -i '/<requiresProject>/a\      <requiresDependencyResolution>runtime</requiresDependencyResolution>' \
          classes/META-INF/maven/plugin.xml
        directory=$out/io/github/nix-purifiers/maven-purifier-legacy-recorder/1
        mkdir -p "$directory"
        jar --create --file "$directory/maven-purifier-legacy-recorder-1.jar" \
          --date=1980-01-01T00:00:02Z -C classes .
        # Legacy plugin containers otherwise inject an undeclared
        # plexus-utils:1.1 download. Use the library from our pinned SDK.
        utils=( ${arguments.maven}/maven/lib/plexus-utils-*.jar )
        test "''${#utils[@]}" -eq 1
        utilsVersion=$(basename "''${utils[0]}" .jar)
        utilsVersion=''${utilsVersion#plexus-utils-}
        utilsDirectory=$out/org/codehaus/plexus/plexus-utils/$utilsVersion
        mkdir -p "$utilsDirectory"
        cp "''${utils[0]}" "$utilsDirectory/plexus-utils-$utilsVersion.jar"
        cat > "$utilsDirectory/plexus-utils-$utilsVersion.pom" <<POM
        <project><modelVersion>4.0.0</modelVersion><groupId>org.codehaus.plexus</groupId>
        <artifactId>plexus-utils</artifactId><version>$utilsVersion</version></project>
        POM
        cat > "$directory/maven-purifier-legacy-recorder-1.pom" <<POM
        <project><modelVersion>4.0.0</modelVersion><groupId>io.github.nix-purifiers</groupId>
        <artifactId>maven-purifier-legacy-recorder</artifactId><version>1</version><packaging>maven-plugin</packaging>
        <dependencies><dependency><groupId>org.codehaus.plexus</groupId><artifactId>plexus-utils</artifactId>
        <version>$utilsVersion</version></dependency></dependencies></project>
        POM
      '';

  marker = import ../../lib-overlays/default/maven-config-marker.nix;

  # Runs Maven offline in the build root, on a writable copy of the
  # repository. The jars get a fixed timestamp, so that a rebuild gives the
  # same bytes.
  runMaven = flags: ''
    export HOME="$TMPDIR/home"
    export JAVA_HOME=${jdk'.home}
    mkdir -p "$HOME"
    cp -r --no-preserve=mode ${repository}/. "$TMPDIR/repository"
    # The lines `maven-purifier prefetch` writes into .mvn/maven.config
    # point at files of the machine that ran it.
    if [ -f .mvn/maven.config ]; then
      sed -i ${lib.escapeShellArg "/^${marker}$/,$d"} .mvn/maven.config
    fi
    ${lib.optionalString (projectDependencies != [ ]) ''
      ${python3}/bin/python ${purifier.jvmRepositoryTool} \
        --rewrite-sources . --catalog ${provided.maven.catalog}
    ''}
    cp -r --no-preserve=mode ${runtimeRecorder}/. "$TMPDIR/repository"
    export MAVEN_PURIFIER_RECORD="$TMPDIR/runtime-record.jsonl"
    export MAVEN_PURIFIER_CAPTURE_RUNTIME=1
    maven_failed() {
      echo "maven-purifier: Maven failed offline. If it reports an artifact that has not been downloaded, the lock does not hold everything this build needs: run \`maven-purifier lock --force\` at the build root, with the goals and -P options you build with. If it reports a compile or test failure, the build itself failed." >&2
      exit 1
    }
    ${maven}/bin/mvn -o -B \
      ${lib.optionalString (lib.versionAtLeast lock.version "3.1") "-Dmaven.ext.class.path=${extension}/share/java/maven-purifier-extension.jar -Dmaven-purifier.replay=true"} \
      -s ${settings} \
      -Dmaven.repo.local="$TMPDIR/repository" \
      -Duser.home="$HOME" \
      -Daether.remoteRepositoryFilter.prefixes=false \
      -Dproject.build.outputTimestamp=1980-01-01T00:00:02Z \
      ${
        lib.escapeShellArgs (
          flags
          ++ mavenFlags
          ++ buildGoals
          ++ [ "io.github.nix-purifiers:maven-purifier-legacy-recorder:1:record" ]
        )
      } </dev/null || maven_failed
  '';

  releaseBuild = stdenvNoCC.mkDerivation {
    name = "maven-release";
    src = filteredSrc;
    nativeBuildInputs = [ jdk' ];
    dontConfigure = true;
    # The outputs are jars and POMs; fixup has nothing to patch, and it
    # must not rewrite them.
    dontFixup = true;
    buildPhase = ''
      runHook preBuild
      ${runMaven [ "-DskipTests" ]}
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      for path in ${
        lib.escapeShellArgs (lib.concatMap (p: map (a: a.path) p.artifacts) lock.projects)
      }; do
        if [ ! -f "$TMPDIR/repository/$path" ]; then
          echo "maven-purifier: the lock names $path, which this build did not install; run \`maven-purifier lock --force\`" >&2
          exit 1
        fi
        mkdir -p "$out/$(dirname "$path")"
        cp "$TMPDIR/repository/$path" "$out/$path"
      done
      ${python3}/bin/python ${./export-runtime.py} ${purifier.projectInputsTool} \
        "$TMPDIR/runtime-record.jsonl" "$TMPDIR/repository" ${provided.maven.catalog} "$out/nix-support/runtime" \
        ${writeText "maven-published-projects.json" (builtins.toJSON lock.projects)}
      runHook postInstall
    '';
  };

  tests = stdenvNoCC.mkDerivation {
    name = "maven-tests";
    src = filteredSrc;
    nativeBuildInputs = [ jdk' ];
    dontConfigure = true;
    dontFixup = true;
    buildPhase = ''
      runHook preBuild
      ${runMaven [ ]}
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      find . -type d \( -name surefire-reports -o -name failsafe-reports \) -path '*/target/*' | while read -r reports; do
        mkdir -p "$out/$(dirname "$reports")"
        cp -r "$reports" "$out/$reports"
      done
      runHook postInstall
    '';
  };

  mkPackage =
    project:
    let
      package =
        runCommand project.artifactId
          {
            passthru = {
              mavenProject = project;
              projections = purifier.jvmProjections {
                repository = releaseBuild;
                producer = releaseBuild;
                runtimeRepository = "${releaseBuild}/nix-support/runtime";
                dependencies = projectDependencies;
                inherit runCommand python3;
                name = "maven-publications";
              };
              inherit (project) groupId artifactId version;
              # The files of the package by key, such as
              # artifacts."core-sources.jar". They are known from the lock
              # alone, before the package is built.
              artifacts = builtins.listToAttrs (
                map (a: {
                  name = a.key;
                  value = "${package}/${a.path}";
                }) project.artifacts
              );
            };
            meta.description = "${project.groupId}:${project.artifactId}:${project.version}, built by maven-purifier";
          }
          ''
            ${lib.concatMapStrings (a: ''
              mkdir -p "$out/${dirOf a.path}"
              cp ${releaseBuild}/${a.path} "$out/${a.path}"
            '') project.artifacts}
          '';
    in
    package;

  # The maven-deploy-plugin version the lock holds, when its goals deployed;
  # otherwise Maven downloads this version when the publish script runs.
  deployPluginVersion =
    let
      versions = lib.concatMap (
        d:
        let
          m = builtins.match "org/apache/maven/plugins/maven-deploy-plugin/([^/]+)/maven-deploy-plugin-[^/]+\\.jar" d.path;
        in
        lib.optional (m != null) (builtins.head m)
      ) (builtins.filter (d: !(d.development or false)) entries);
    in
    if versions != [ ] then
      lib.last (lib.naturalSort versions)
    else if lib.versionOlder lock.version "3.6.3" then
      "2.8.2"
    else
      "3.1.4";

  rootProjections = purifier.jvmProjections {
    repository = releaseBuild;
    runtimeRepository = "${releaseBuild}/nix-support/runtime";
    dependencies = projectDependencies;
    inherit runCommand python3;
    name = "maven-publications";
  };

  # Deploys the artifacts of the release, module by module, with
  # deploy:deploy-file of the locked Maven:
  #
  #   maven-purifier-publish --repository-id ID --url URL [MAVEN ARG...]
  #
  # Maven reads ~/.m2/settings.xml as usual, which is where the credentials
  # of the repository ID belong. The local repository is a copy of the
  # locked downloads, so the deploy plugin comes from the lock when the
  # lock run deployed.
  publish = writeShellScriptBin "maven-purifier-publish" ''
    set -euo pipefail
    repository_id=
    url=
    args=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --repository-id) repository_id=$2; shift 2 ;;
        --url) url=$2; shift 2 ;;
        *) args+=("$1"); shift ;;
      esac
    done
    if [ -z "$repository_id" ] || [ -z "$url" ]; then
      echo "usage: maven-purifier-publish --repository-id ID --url URL [MAVEN ARG...]" >&2
      exit 2
    fi
    export JAVA_HOME=''${JAVA_HOME:-${jdk'.home}}
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    cp -r --no-preserve=mode ${repository}/. "$work/repository"
    cd "$work"
    ${python3}/bin/python ${./project-inputs.py} merge \
      ${purifier.jvmRepositoryTool} ${rootProjections.maven} "$work/repository"
    ${python3}/bin/python ${./publish-projections.py} \
      --maven ${maven}/bin/mvn --repository ${rootProjections.maven} \
      --catalog ${rootProjections.maven.catalog} --local-repository "$work/repository" \
      --plugin-version ${deployPluginVersion} --repository-id "$repository_id" \
      --url "$url" -- "''${args[@]}"
  '';

  prefetch = runCommand "maven-purifier-prefetch" { } ''
    mkdir $out
    ln -s ${developmentRepository} $out/repository
    ln -s ${maven} $out/maven
    ln -s ${jdk'.home} $out/jdk
    ln -s ${settings} $out/settings.xml
    ${lib.optionalString (projectDependencies != [ ]) ''
      ${python3}/bin/python ${./project-inputs.py} properties \
        ${provided.maven.catalog} "$out/project-inputs.properties"
    ''}
  '';
in
{
  packages = builtins.mapAttrs (_: mkPackage) checked.outputs;
  inherit tests;
  projections = rootProjections;
  release = releaseBuild // {
    inherit publish;
  };
  fetches = checked.entries;
  inherit
    prefetch
    repository
    maven
    ;
  lock = checked;
  jdk = jdk';
}
